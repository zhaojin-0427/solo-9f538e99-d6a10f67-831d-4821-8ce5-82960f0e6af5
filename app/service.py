"""学籍更正清单业务逻辑。

关键不变量
==========

1. 转学单号固定对应一名学生；草稿批次内可改生效日与转入校，不可改学生。
2. 整批提交后，任一学生同一生效日最多一条有效转学单。
3. 提交成功时，为“本批学生”冻结一份复查依据（本批之前的有效单 + 本批单据），
   同时记录“归属可能发生变化”的学校索引。清单查询只读这份冻结依据，
   因此后续补录批次或服务重启都不会改变旧批清单。
4. 清单中的“归属”= 截至统计日（含当日）生效日最晚的一条单所对应的学校；
   无单即未登记（None）。
5. “补报”= 本批后归属变为该校，而本批前不是该校；
   “撤回”= 本批前归属是该校，而本批后不再是该校；归属未变不列入。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Database
from .errors import DomainError
from .models import (
    Batch,
    BasisRecord,
    BasisStudentSchool,
    Counter,
    EffectiveSlip,
    Slip,
)

COMMIT_SEQ_COUNTER = "commit_seq"


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlipView:
    """用于归属计算的只读单据视图。"""

    slip_no: str
    student_no: str
    school_id: str
    effective_date: date
    origin_batch_no: str
    origin_commit_seq: int = 0
    origin_batch_id: int | None = None


@dataclass(frozen=True)
class Attribution:
    school_id: str
    slip_no: str
    effective_date: date
    origin_batch_no: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def latest_as_of(slips, statistics_date: date) -> Attribution | None:
    """取生效日不晚于统计日的最后一单；同日多单时按单号稳定决断。"""
    best: Attribution | None = None
    for slip in slips:
        if slip.effective_date > statistics_date:
            continue
        candidate = Attribution(
            school_id=slip.school_id,
            slip_no=slip.slip_no,
            effective_date=slip.effective_date,
            origin_batch_no=slip.origin_batch_no,
        )
        if best is None or (candidate.effective_date, candidate.slip_no) > (
            best.effective_date,
            best.slip_no,
        ):
            best = candidate
    return best


def changed_schools(prior, after) -> set[str]:
    """返回在某个统计日上“归属前后不同”所涉及的学校集合。

    归属只在单据生效日处发生变化，因此只需检查前后两侧生效日的并集，
    不逐日展开。若学生归属从未变化，返回空集（不会被任何学校清单读取）。
    """
    dates = {slip.effective_date for slip in prior}
    dates.update(slip.effective_date for slip in after)
    involved: set[str] = set()
    for point in sorted(dates):
        before = latest_as_of(prior, point)
        afterwards = latest_as_of(after, point)
        before_school = before.school_id if before else None
        after_school = afterwards.school_id if afterwards else None
        if before_school == after_school:
            continue
        if before_school is not None:
            involved.add(before_school)
        if after_school is not None:
            involved.add(after_school)
    return involved


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def initialize(database: Database) -> None:
    database.create_all()
    with database.session() as session:
        try:
            exists = session.execute(
                select(Counter).where(Counter.name == COMMIT_SEQ_COUNTER)
            ).scalar_one_or_none()
            if exists is None:
                session.add(Counter(name=COMMIT_SEQ_COUNTER, value=0))
                session.commit()
        except Exception:  # pragma: no cover - 多进程同时建库时容忍竞争
            session.rollback()


# ---------------------------------------------------------------------------
# 批次
# ---------------------------------------------------------------------------


def create_batch(database: Database, batch_no: str) -> Batch:
    with database.session() as session:
        existing = session.execute(
            select(Batch).where(Batch.batch_no == batch_no)
        ).scalar_one_or_none()
        if existing is not None:
            raise DomainError(
                409,
                "batch_exists",
                f"批次 {batch_no} 已存在",
                {"batch_no": batch_no, "status": existing.status},
            )
        batch = Batch(batch_no=batch_no, status="draft")
        session.add(batch)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise DomainError(
                409, "batch_exists", f"批次 {batch_no} 已存在", {"batch_no": batch_no}
            ) from exc
        except Exception:
            session.rollback()
            raise
        return batch


def get_batch(database: Database, batch_no: str) -> Batch:
    with database.session() as session:
        batch = session.execute(
            select(Batch).where(Batch.batch_no == batch_no)
        ).scalar_one_or_none()
        if batch is None:
            raise DomainError(404, "batch_not_found", f"批次 {batch_no} 不存在")
        return batch


def list_batches(database: Database, status: str | None = None) -> list[Batch]:
    with database.session() as session:
        stmt = select(Batch).order_by(Batch.id)
        if status is not None:
            stmt = stmt.where(Batch.status == status)
        return list(session.scalars(stmt))


def _load_draft_batch(session: Session, batch_no: str) -> Batch:
    batch = session.execute(
        select(Batch).where(Batch.batch_no == batch_no)
    ).scalar_one_or_none()
    if batch is None:
        raise DomainError(404, "batch_not_found", f"批次 {batch_no} 不存在")
    if batch.status != "draft":
        raise DomainError(
            409,
            "batch_not_editable",
            f"批次 {batch_no} 已生效，不能再登记或更正转学单",
            {"batch_no": batch_no, "status": batch.status},
        )
    return batch


def upsert_slip(
    database: Database,
    batch_no: str,
    *,
    slip_no: str,
    student_no: str,
    school_id: str,
    effective_date: date,
) -> tuple[str, Slip]:
    """在草稿批次内登记或更正一张转学单（按单号 upsert）。"""
    with database.session() as session:
        try:
            batch = _load_draft_batch(session, batch_no)

            # 单号与学生的绑定在全局范围内固定（已生效或任意草稿批次）。
            effective = session.get(EffectiveSlip, slip_no)
            bound_student = effective.student_no if effective is not None else None
            if bound_student is None:
                bound_student = session.execute(
                    select(Slip.student_no)
                    .where(Slip.slip_no == slip_no)
                    .order_by(Slip.id)
                    .limit(1)
                ).scalar_one_or_none()
            if bound_student is not None and bound_student != student_no:
                raise DomainError(
                    422,
                    "slip_student_immutable",
                    f"转学单 {slip_no} 已绑定学生 {bound_student}，"
                    f"不能改为 {student_no}",
                    {
                        "slip_no": slip_no,
                        "expected_student_no": bound_student,
                        "given_student_no": student_no,
                    },
                )

            slip = session.execute(
                select(Slip).where(
                    Slip.batch_id == batch.id, Slip.slip_no == slip_no
                )
            ).scalar_one_or_none()

            if slip is not None:
                if slip.student_no != student_no:
                    raise DomainError(
                        422,
                        "slip_student_immutable",
                        f"转学单 {slip_no} 在本批中已绑定学生 {slip.student_no}，"
                        f"不能改为 {student_no}",
                        {
                            "slip_no": slip_no,
                            "expected_student_no": slip.student_no,
                            "given_student_no": student_no,
                        },
                    )
                slip.school_id = school_id
                slip.effective_date = effective_date
                slip.updated_at = utcnow()
                action = "updated"
            else:
                slip = Slip(
                    batch_id=batch.id,
                    batch=batch,
                    slip_no=slip_no,
                    student_no=student_no,
                    school_id=school_id,
                    effective_date=effective_date,
                )
                session.add(slip)
                action = "created"

            session.commit()
            return action, slip
        except Exception:
            session.rollback()
            raise


def delete_slip(database: Database, batch_no: str, slip_no: str) -> None:
    with database.session() as session:
        try:
            batch = _load_draft_batch(session, batch_no)
            slip = session.execute(
                select(Slip).where(
                    Slip.batch_id == batch.id, Slip.slip_no == slip_no
                )
            ).scalar_one_or_none()
            if slip is None:
                raise DomainError(
                    404,
                    "slip_not_found",
                    f"批次 {batch_no} 中不存在转学单 {slip_no}",
                )
            session.delete(slip)
            session.commit()
        except Exception:
            session.rollback()
            raise


def list_slips(database: Database, batch_no: str) -> list[Slip]:
    with database.session() as session:
        batch = session.execute(
            select(Batch).where(Batch.batch_no == batch_no)
        ).scalar_one_or_none()
        if batch is None:
            raise DomainError(404, "batch_not_found", f"批次 {batch_no} 不存在")
        return list(
            session.scalars(
                select(Slip).where(Slip.batch_id == batch.id).order_by(Slip.id)
            )
        )


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------


def commit_batch(database: Database, batch_no: str) -> Batch:
    """整批生效。

    先在事务内取得提交序号（同时获得 SQLite 写锁），再读取/校验/写入，
    任一校验失败则整体回滚：学籍不变、不产生依据、不产生清单。
    """
    with database.session() as session:
        try:
            seq = _next_commit_seq(session)

            batch = session.execute(
                select(Batch).where(Batch.batch_no == batch_no)
            ).scalar_one_or_none()
            if batch is None:
                raise DomainError(404, "batch_not_found", f"批次 {batch_no} 不存在")
            if batch.status != "draft":
                raise DomainError(
                    409,
                    "batch_not_committed",
                    f"批次 {batch_no} 不是草稿状态，无法提交",
                    {"batch_no": batch_no, "status": batch.status},
                )

            slips = list(
                session.scalars(
                    select(Slip).where(Slip.batch_id == batch.id).order_by(Slip.id)
                )
            )
            if not slips:
                raise DomainError(
                    409, "batch_empty", f"批次 {batch_no} 没有登记任何转学单"
                )

            student_nos = sorted({slip.student_no for slip in slips})
            slip_nos = sorted({slip.slip_no for slip in slips})

            # 单号全局绑定的二次校验（依赖已生效学籍）。
            effective_by_no = {
                row.slip_no: row
                for row in session.scalars(
                    select(EffectiveSlip).where(EffectiveSlip.slip_no.in_(slip_nos))
                )
            }
            prior_by_student: dict[str, list[EffectiveSlip]] = defaultdict(list)
            for row in session.scalars(
                select(EffectiveSlip)
                .where(EffectiveSlip.student_no.in_(student_nos))
                .order_by(EffectiveSlip.effective_date, EffectiveSlip.slip_no)
            ):
                prior_by_student[row.student_no].append(row)

            for slip in slips:
                bound = effective_by_no.get(slip.slip_no)
                if bound is not None and bound.student_no != slip.student_no:
                    raise DomainError(
                        422,
                        "slip_student_immutable",
                        f"转学单 {slip.slip_no} 已绑定学生 {bound.student_no}，"
                        f"不能改为 {slip.student_no}",
                        {
                            "slip_no": slip.slip_no,
                            "expected_student_no": bound.student_no,
                            "given_student_no": slip.student_no,
                        },
                    )

            # 计算合并后的有效学籍视图（本批之前 + 本批）。
            prior_views: dict[str, list[SlipView]] = {}
            after_views: dict[str, list[SlipView]] = {}
            for student_no in student_nos:
                prior = [
                    SlipView(
                        slip_no=row.slip_no,
                        student_no=row.student_no,
                        school_id=row.school_id,
                        effective_date=row.effective_date,
                        origin_batch_no=row.source_batch_no,
                        origin_commit_seq=row.source_commit_seq,
                        origin_batch_id=row.source_batch_id,
                    )
                    for row in prior_by_student.get(student_no, [])
                ]
                merged = {view.slip_no: view for view in prior}
                for slip in slips:
                    if slip.student_no != student_no:
                        continue
                    merged[slip.slip_no] = SlipView(
                        slip_no=slip.slip_no,
                        student_no=student_no,
                        school_id=slip.school_id,
                        effective_date=slip.effective_date,
                        origin_batch_no=batch.batch_no,
                        origin_commit_seq=seq,
                        origin_batch_id=batch.id,
                    )
                prior_views[student_no] = prior
                after_views[student_no] = list(merged.values())

            # 同一学生同一生效日最多一单。
            for student_no in student_nos:
                seen: dict[date, str] = {}
                for view in after_views[student_no]:
                    other = seen.get(view.effective_date)
                    if other is not None:
                        raise DomainError(
                            409,
                            "student_day_conflict",
                            f"学生 {student_no} 在 {view.effective_date.isoformat()} "
                            "存在多张转学单",
                            {
                                "student_no": student_no,
                                "effective_date": view.effective_date.isoformat(),
                                "slip_nos": sorted([other, view.slip_no]),
                            },
                        )
                    seen[view.effective_date] = view.slip_no

            # 计算归属变化涉及的学校；归属从未变化的学生不建索引。
            involved_by_student: dict[str, set[str]] = {}
            for student_no in student_nos:
                involved_by_student[student_no] = changed_schools(
                    prior_views[student_no], after_views[student_no]
                )

            # 应用有效学籍：先整体删除受影响学生，再写入合并视图，避免换日冲突。
            session.execute(
                delete(EffectiveSlip).where(
                    EffectiveSlip.student_no.in_(student_nos)
                )
            )
            session.flush()
            for student_no in student_nos:
                for view in after_views[student_no]:
                    session.add(
                        EffectiveSlip(
                            slip_no=view.slip_no,
                            student_no=student_no,
                            school_id=view.school_id,
                            effective_date=view.effective_date,
                            source_batch_id=view.origin_batch_id or batch.id,
                            source_batch_no=view.origin_batch_no,
                            source_commit_seq=view.origin_commit_seq,
                        )
                    )
            session.flush()

            # 冻结复查依据：仅为本批中“归属确实会变化”的学生留存，内容为
            # 本批之前的有效单 + 本批单据。归属从未变化的学生既不会被任何
            # 学校清单读取，也无需留存依据。
            prior_count = 0
            batch_count = 0
            for student_no in student_nos:
                if not involved_by_student[student_no]:
                    continue
                for view in prior_views[student_no]:
                    session.add(
                        BasisRecord(
                            batch_id=batch.id,
                            student_no=student_no,
                            slip_no=view.slip_no,
                            school_id=view.school_id,
                            effective_date=view.effective_date,
                            origin_batch_no=view.origin_batch_no,
                            is_batch_slip=False,
                        )
                    )
                    prior_count += 1
                for slip in slips:
                    if slip.student_no != student_no:
                        continue
                    session.add(
                        BasisRecord(
                            batch_id=batch.id,
                            student_no=student_no,
                            slip_no=slip.slip_no,
                            school_id=slip.school_id,
                            effective_date=slip.effective_date,
                            origin_batch_no=batch.batch_no,
                            is_batch_slip=True,
                        )
                    )
                    batch_count += 1

            for student_no, schools in involved_by_student.items():
                for school_id in sorted(schools):
                    session.add(
                        BasisStudentSchool(
                            batch_id=batch.id,
                            student_no=student_no,
                            school_id=school_id,
                        )
                    )

            batch.status = "committed"
            batch.committed_at = utcnow()
            batch.commit_seq = seq
            batch.slip_count = len(slips)
            batch.basis_record_count = prior_count + batch_count
            batch.basis_prior_record_count = prior_count
            batch.basis_batch_record_count = batch_count
            batch.affected_student_count = len(student_nos)
            batch.changed_student_count = sum(
                1 for schools in involved_by_student.values() if schools
            )

            session.commit()
            return batch
        except Exception:
            session.rollback()
            raise


def _next_commit_seq(session: Session) -> int:
    """在写事务内递增提交序号。

    该 UPDATE 会立即取得 SQLite 写锁，从而把并发的批次提交串行化，避免
    “先读后写”造成的归属计算竞争。未使用 RETURNING，以兼容较旧的 SQLite。
    """
    result = session.execute(
        update(Counter)
        .where(Counter.name == COMMIT_SEQ_COUNTER)
        .values(value=Counter.value + 1)
    )
    if result.rowcount == 0:
        session.add(Counter(name=COMMIT_SEQ_COUNTER, value=1))
        session.flush()
        return 1
    value = session.execute(
        select(Counter.value).where(Counter.name == COMMIT_SEQ_COUNTER)
    ).scalar_one()
    return int(value)


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


@dataclass
class CorrectionList:
    batch_no: str
    school_id: str
    statistics_date: date
    additions: list[dict]
    withdrawals: list[dict]
    candidate_student_count: int
    listed_student_count: int
    unmatched_student_count: int
    basis_records_read: int


def correction_list(
    database: Database,
    batch_no: str,
    school_id: str,
    statistics_date: date,
) -> CorrectionList:
    with database.session() as session:
        batch = session.execute(
            select(Batch).where(Batch.batch_no == batch_no)
        ).scalar_one_or_none()
        if batch is None:
            raise DomainError(404, "batch_not_found", f"批次 {batch_no} 不存在")
        if batch.status != "committed":
            raise DomainError(
                409,
                "batch_not_committed",
                f"批次 {batch_no} 尚未成功提交，不产生清单",
                {"batch_no": batch_no, "status": batch.status},
            )

        candidates = list(
            session.scalars(
                select(BasisStudentSchool.student_no)
                .where(
                    BasisStudentSchool.batch_id == batch.id,
                    BasisStudentSchool.school_id == school_id,
                )
                .order_by(BasisStudentSchool.student_no)
            )
        )

        rows: list[BasisRecord] = []
        if candidates:
            rows = list(
                session.scalars(
                    select(BasisRecord)
                    .where(
                        BasisRecord.batch_id == batch.id,
                        BasisRecord.student_no.in_(candidates),
                    )
                    .order_by(BasisRecord.id)
                )
            )

        grouped: dict[str, list[BasisRecord]] = defaultdict(list)
        for row in rows:
            grouped[row.student_no].append(row)

        additions: list[dict] = []
        withdrawals: list[dict] = []
        listed_students: set[str] = set()

        for student_no in candidates:
            student_rows = grouped.get(student_no, [])
            prior = [
                SlipView(
                    slip_no=row.slip_no,
                    student_no=row.student_no,
                    school_id=row.school_id,
                    effective_date=row.effective_date,
                    origin_batch_no=row.origin_batch_no,
                )
                for row in student_rows
                if not row.is_batch_slip
            ]
            merged = {view.slip_no: view for view in prior}
            for row in student_rows:
                if row.is_batch_slip:
                    merged[row.slip_no] = SlipView(
                        slip_no=row.slip_no,
                        student_no=row.student_no,
                        school_id=row.school_id,
                        effective_date=row.effective_date,
                        origin_batch_no=row.origin_batch_no,
                    )

            before = latest_as_of(prior, statistics_date)
            after = latest_as_of(list(merged.values()), statistics_date)
            before_school = before.school_id if before else None
            after_school = after.school_id if after else None

            if before_school == after_school:
                continue

            if after_school == school_id:
                listed_students.add(student_no)
                additions.append(
                    {
                        "student_no": student_no,
                        "school_id": school_id,
                        "slip_no": after.slip_no,
                        "effective_date": after.effective_date,
                        "previous_school_id": before_school,
                        "previous_slip_no": before.slip_no if before else None,
                    }
                )
            elif before_school == school_id:
                listed_students.add(student_no)
                withdrawals.append(
                    {
                        "student_no": student_no,
                        "school_id": school_id,
                        "slip_no": before.slip_no,
                        "effective_date": before.effective_date,
                        "next_school_id": after_school,
                        "next_slip_no": after.slip_no if after else None,
                    }
                )

        additions.sort(key=lambda item: (item["student_no"], item["slip_no"]))
        withdrawals.sort(key=lambda item: (item["student_no"], item["slip_no"]))

        return CorrectionList(
            batch_no=batch_no,
            school_id=school_id,
            statistics_date=statistics_date,
            additions=additions,
            withdrawals=withdrawals,
            candidate_student_count=len(candidates),
            listed_student_count=len(listed_students),
            unmatched_student_count=len(candidates) - len(listed_students),
            basis_records_read=len(rows),
        )


# ---------------------------------------------------------------------------
# 其他
# ---------------------------------------------------------------------------


def statistics(database: Database) -> dict:
    with database.session() as session:
        return {
            "batches": session.scalar(select(func.count()).select_from(Batch)) or 0,
            "committed_batches": session.scalar(
                select(func.count()).select_from(Batch).where(Batch.status == "committed")
            )
            or 0,
            "effective_slips": session.scalar(
                select(func.count()).select_from(EffectiveSlip)
            )
            or 0,
            "basis_records": session.scalar(
                select(func.count()).select_from(BasisRecord)
            )
            or 0,
        }
