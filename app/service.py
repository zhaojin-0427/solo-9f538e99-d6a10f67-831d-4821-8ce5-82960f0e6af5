"""学籍更正清单业务逻辑。

关键不变量
==========

1. 转学单号固定对应一名学生；草稿批次内可改生效日与转入校，不可改学生。
2. 整批提交后，任一学生同一生效日最多一条有效转学单。
3. 提交成功时，为“本批学生”冻结一份复查依据（本批之前的有效单 + 本批单据），
   同时在同一事务内写好清单查询数据：
   * ``basis_student_schools``：本批归属会变化的学校索引（候选计数）；
   * ``correction_segments``：按生效日边界保存的补报/撤回成立区间
     （不按统计日逐日展开）。
   任一步失败整体回滚：学籍不变、不产生依据与查询数据、批次仍是草稿，
   查不到该批清单。
4. 清单查询只读取冻结依据与本批查询数据，不回放历史批次；因此后续补录批次
   或服务重启都不会改变旧批清单。
5. 清单中的“归属”= 截至统计日（含当日）生效日最晚的一条单所对应的学校；
   无单即未登记（None）。
6. “补报”= 本批后归属变为该校，而本批前不是该校；
   “撤回”= 本批前归属是该校，而本批后不再是该校；归属未变不列入。

读取预算
========

清单查询的记录读取条数有硬上限：

* 辅助记录（``correction_segments``）基础预算 :data:`AUXILIARY_BASE_BUDGET`
  条；每命中一名学生再放宽 :data:`BASIS_BUDGET_PER_HIT` 条；
* 复查依据（``basis_records``）每名命中学生最多读取
  :data:`BASIS_BUDGET_PER_HIT` 条（实现上只做两次 ``LIMIT 1`` 索引取数，
  实际至多 2 条）。

预算被突破时直接抛出 ``read_budget_exceeded``，不会静默超量读取。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Database
from .errors import DomainError
from .models import (
    BasisRecord,
    BasisStudentSchool,
    Batch,
    CorrectionSegment,
    Counter,
    EffectiveSlip,
    Slip,
)

COMMIT_SEQ_COUNTER = "commit_seq"

# 辅助记录基础读取预算：未命中任何学生时最多读取这么多条。
AUXILIARY_BASE_BUDGET = 64
# 每命中一名学生：辅助预算放宽量，同时也是该生复查依据的读取上限。
BASIS_BUDGET_PER_HIT = 4
# 辅助记录分页大小，与基础预算一致。
AUXILIARY_PAGE_SIZE = AUXILIARY_BASE_BUDGET

# 旧版 SQLite 卷没有为依据取数建立的覆盖索引，启动时补齐（IF NOT EXISTS 幂等）。
_LEGACY_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_basis_lookup ON basis_records "
    "(batch_id, student_no, is_batch_slip, effective_date, slip_no, school_id)"
)


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


@dataclass(frozen=True)
class Segment:
    """某学生相对某校的补报/撤回成立区间，端点均为单据生效日。"""

    student_no: str
    school_id: str
    kind: str  # "add" | "withdraw"
    start_date: date
    end_date: date | None


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


def build_student_segments(
    student_no: str, prior, after
) -> list[Segment]:
    """按生效日边界推导该学生所有补报/撤回成立区间。

    归属只在单据生效日处发生变化，因此把时间轴按两侧生效日的并集切成
    半开区间 ``[point_i, point_{i+1})``（末段右端开放），在每个区间内
    用“截至区间起点（含）的最晚一单”分别计算本批前/本批后归属——这与
    清单查询“截至统计日（含当日）”的口径完全一致，不按日展开：

    * 本批后归属某校、本批前不是该校 -> 对该校的“补报”；
    * 本批前归属某校、本批后不再是该校 -> 对该校的“撤回”。

    相邻区间状态相同则合并为一条区间（端点仍是生效日）。归属从未变化的
    学生返回空列表，既不建辅助数据也不留复查依据。
    """
    points = sorted(
        {slip.effective_date for slip in prior}
        | {slip.effective_date for slip in after}
    )
    segments: list[Segment] = []
    # 当前仍开放的区间：(school_id, kind) -> 起点日期。
    open_segments: dict[tuple[str, str], date] = {}

    for index, point in enumerate(points):
        before = latest_as_of(prior, point)
        afterwards = latest_as_of(after, point)
        before_school = before.school_id if before else None
        after_school = afterwards.school_id if afterwards else None

        active: set[tuple[str, str]] = set()
        if before_school != after_school:
            if after_school is not None:
                active.add((after_school, "add"))
            if before_school is not None:
                active.add((before_school, "withdraw"))

        # 本区间不再成立的状态在区间起点关闭（末点即当前生效日）。
        for key in list(open_segments):
            if key not in active:
                segments.append(
                    Segment(
                        student_no=student_no,
                        school_id=key[0],
                        kind=key[1],
                        start_date=open_segments.pop(key),
                        end_date=point,
                    )
                )
        # 本区间新成立的状态从区间起点开启；跨区间持续成立的保持原起点。
        for key in active:
            open_segments.setdefault(key, point)

    for school_id, kind in sorted(open_segments):
        segments.append(
            Segment(
                student_no=student_no,
                school_id=school_id,
                kind=kind,
                start_date=open_segments[(school_id, kind)],
                end_date=None,
            )
        )
    return segments


# ---------------------------------------------------------------------------
# 启动与旧卷升级
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
    _backfill_legacy_query_data(database)


def _views_from_basis_rows(rows) -> tuple[list[SlipView], list[SlipView]]:
    """从一批冻结依据重建“本批前 / 本批后”的单据视图。"""
    prior = [
        SlipView(
            slip_no=row.slip_no,
            student_no=row.student_no,
            school_id=row.school_id,
            effective_date=row.effective_date,
            origin_batch_no=row.origin_batch_no,
        )
        for row in rows
        if not row.is_batch_slip
    ]
    merged = {view.slip_no: view for view in prior}
    for row in rows:
        if row.is_batch_slip:
            merged[row.slip_no] = SlipView(
                slip_no=row.slip_no,
                student_no=row.student_no,
                school_id=row.school_id,
                effective_date=row.effective_date,
                origin_batch_no=row.origin_batch_no,
            )
    return prior, list(merged.values())


def _backfill_legacy_query_data(database: Database) -> None:
    """为旧版 SQLite 卷补齐清单查询数据。

    旧批次的复查依据（``basis_records``）在旧版本提交时已按原口径冻结，
    这里只从这些快照重建 ``correction_segments`` 与候选学校索引，
    不改动任何依据、学籍与批次数据，因此旧名单口径保持不变。
    按批提交、按批跳过，中断后重启可继续，整体幂等。
    """
    with database.session() as session:
        session.execute(text(_LEGACY_INDEX_DDL))
        committed_batches = list(
            session.scalars(
                select(Batch)
                .where(Batch.status == "committed")
                .order_by(Batch.id)
            )
        )

    for batch in committed_batches:
        with database.session() as session:
            try:
                existing = session.scalar(
                    select(func.count())
                    .select_from(CorrectionSegment)
                    .where(CorrectionSegment.batch_id == batch.id)
                )
                if existing:
                    continue

                rows = list(
                    session.scalars(
                        select(BasisRecord)
                        .where(BasisRecord.batch_id == batch.id)
                        .order_by(BasisRecord.id)
                    )
                )
                if not rows:
                    # 没有留存依据的批次本来就不产生任何名单，无需查询数据。
                    continue

                grouped: dict[str, list[BasisRecord]] = defaultdict(list)
                for row in rows:
                    grouped[row.student_no].append(row)

                for student_no in sorted(grouped):
                    prior, after = _views_from_basis_rows(grouped[student_no])
                    segments = build_student_segments(student_no, prior, after)
                    for segment in segments:
                        session.add(
                            CorrectionSegment(
                                batch_id=batch.id,
                                student_no=segment.student_no,
                                school_id=segment.school_id,
                                kind=segment.kind,
                                start_date=segment.start_date,
                                end_date=segment.end_date,
                            )
                        )

                    indexed_schools = {
                        row.school_id
                        for row in session.scalars(
                            select(BasisStudentSchool).where(
                                BasisStudentSchool.batch_id == batch.id,
                                BasisStudentSchool.student_no == student_no,
                            )
                        )
                    }
                    involved = {segment.school_id for segment in segments}
                    for school_id in sorted(involved - indexed_schools):
                        session.add(
                            BasisStudentSchool(
                                batch_id=batch.id,
                                student_no=student_no,
                                school_id=school_id,
                            )
                        )

                session.commit()
            except Exception:
                session.rollback()
                raise


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
    任一校验失败则整体回滚：学籍不变、不产生依据与查询数据、不产生清单。
    学籍（effective_slips）与查询数据（basis_records / correction_segments /
    basis_student_schools）在同一事务内一并生效。
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

            # 按生效日边界推导每个学生的补报/撤回区间（不按日展开）；
            # 归属从未变化的学生得到空区间，不建任何查询数据。
            segments_by_student: dict[str, list[Segment]] = {}
            for student_no in student_nos:
                segments_by_student[student_no] = build_student_segments(
                    student_no,
                    prior_views[student_no],
                    after_views[student_no],
                )
            involved_by_student = {
                student_no: {segment.school_id for segment in segments}
                for student_no, segments in segments_by_student.items()
            }

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

            # 查询辅助数据：补报/撤回成立区间 + 候选学校索引，与学籍同事务生效。
            for student_no, segments in segments_by_student.items():
                for segment in segments:
                    session.add(
                        CorrectionSegment(
                            batch_id=batch.id,
                            student_no=segment.student_no,
                            school_id=segment.school_id,
                            kind=segment.kind,
                            start_date=segment.start_date,
                            end_date=segment.end_date,
                        )
                    )

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
    auxiliary_records_read: int
    basis_records_read: int


def _latest_basis_row(
    session: Session,
    batch_id: int,
    student_no: str,
    is_batch_slip: bool,
    statistics_date: date,
) -> BasisRecord | None:
    """取该生指定类别中、生效日不晚于统计日的最晚一条依据（LIMIT 1 索引取数）。"""
    return session.scalars(
        select(BasisRecord)
        .where(
            BasisRecord.batch_id == batch_id,
            BasisRecord.student_no == student_no,
            BasisRecord.is_batch_slip.is_(is_batch_slip),
            BasisRecord.effective_date <= statistics_date,
        )
        .order_by(
            BasisRecord.effective_date.desc(),
            BasisRecord.slip_no.desc(),
        )
        .limit(1)
    ).first()


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

        # 候选计数走 (batch_id, school_id) 索引，不读取任何依据行。
        candidate_count = session.scalar(
            select(func.count())
            .select_from(BasisStudentSchool)
            .where(
                BasisStudentSchool.batch_id == batch.id,
                BasisStudentSchool.school_id == school_id,
            )
        ) or 0

        if candidate_count == 0:
            return CorrectionList(
                batch_no=batch_no,
                school_id=school_id,
                statistics_date=statistics_date,
                additions=[],
                withdrawals=[],
                candidate_student_count=0,
                listed_student_count=0,
                unmatched_student_count=0,
                auxiliary_records_read=0,
                basis_records_read=0,
            )

        # 第一步：有界读取辅助区间。日期条件直接在复合索引内过滤，
        # 不扫完该校全部候选再回内存筛日期。分页受预算约束：
        # 基础 64 条，每命中一名学生再放宽 4 条。
        auxiliary_read = 0
        hits: dict[str, str] = {}
        offset = 0
        while True:
            budget = AUXILIARY_BASE_BUDGET + BASIS_BUDGET_PER_HIT * len(hits)
            remaining = budget - auxiliary_read
            if remaining <= 0:
                raise DomainError(
                    500,
                    "read_budget_exceeded",
                    "辅助记录读取条数超过预算，已中止查询",
                    {
                        "budget": budget,
                        "auxiliary_records_read": auxiliary_read,
                        "hit_student_count": len(hits),
                    },
                )
            page = list(
                session.scalars(
                    select(CorrectionSegment)
                    .where(
                        CorrectionSegment.batch_id == batch.id,
                        CorrectionSegment.school_id == school_id,
                        CorrectionSegment.start_date <= statistics_date,
                        or_(
                            CorrectionSegment.end_date.is_(None),
                            CorrectionSegment.end_date > statistics_date,
                        ),
                    )
                    .order_by(
                        CorrectionSegment.start_date,
                        CorrectionSegment.end_date,
                        CorrectionSegment.student_no,
                    )
                    .offset(offset)
                    .limit(min(AUXILIARY_PAGE_SIZE, remaining))
                )
            )
            auxiliary_read += len(page)
            if not page:
                break
            for segment in page:
                hits.setdefault(segment.student_no, segment.kind)
            if len(page) < min(AUXILIARY_PAGE_SIZE, remaining):
                break
            offset += len(page)

        # 第二步：每名命中学生至多两次 LIMIT 1 索引取数（本批单 / 历史单各一），
        # 实际读取条数计入 basis_records_read；上限为每生 4 条。
        basis_read = 0
        additions: list[dict] = []
        withdrawals: list[dict] = []
        basis_budget = BASIS_BUDGET_PER_HIT * len(hits)

        for student_no, kind in sorted(hits.items()):
            batch_row = _latest_basis_row(
                session, batch.id, student_no, True, statistics_date
            )
            if batch_row is not None:
                basis_read += 1
            prior_row = _latest_basis_row(
                session, batch.id, student_no, False, statistics_date
            )
            if prior_row is not None:
                basis_read += 1
            if basis_read > basis_budget:
                raise DomainError(
                    500,
                    "read_budget_exceeded",
                    "复查依据读取条数超过预算，已中止查询",
                    {
                        "budget": basis_budget,
                        "basis_records_read": basis_read,
                        "hit_student_count": len(hits),
                    },
                )

            if kind == "add":
                if batch_row is None:
                    raise DomainError(
                        500,
                        "basis_data_inconsistent",
                        f"学生 {student_no} 的补报区间缺少本批单据依据",
                        {"batch_no": batch_no, "student_no": student_no},
                    )
                additions.append(
                    {
                        "student_no": student_no,
                        "school_id": school_id,
                        "slip_no": batch_row.slip_no,
                        "effective_date": batch_row.effective_date,
                        "previous_school_id": (
                            prior_row.school_id if prior_row is not None else None
                        ),
                        "previous_slip_no": (
                            prior_row.slip_no if prior_row is not None else None
                        ),
                    }
                )
            else:
                if prior_row is None:
                    raise DomainError(
                        500,
                        "basis_data_inconsistent",
                        f"学生 {student_no} 的撤回区间缺少历史单据依据",
                        {"batch_no": batch_no, "student_no": student_no},
                    )
                withdrawals.append(
                    {
                        "student_no": student_no,
                        "school_id": school_id,
                        "slip_no": prior_row.slip_no,
                        "effective_date": prior_row.effective_date,
                        "next_school_id": (
                            batch_row.school_id if batch_row is not None else None
                        ),
                        "next_slip_no": (
                            batch_row.slip_no if batch_row is not None else None
                        ),
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
            candidate_student_count=candidate_count,
            listed_student_count=len(hits),
            unmatched_student_count=candidate_count - len(hits),
            auxiliary_records_read=auxiliary_read,
            basis_records_read=basis_read,
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
            "correction_segments": session.scalar(
                select(func.count()).select_from(CorrectionSegment)
            )
            or 0,
        }
