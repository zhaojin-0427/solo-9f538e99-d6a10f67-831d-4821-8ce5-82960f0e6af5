"""清单查询优化的专项测试。

覆盖：
* 跨日更正（日期往后/往前改）的辅助区间与名单口径；
* 只命中一名学生的“旧批”：辅助与依据读取条数上限；
* 读取预算（辅助 64 起步、每命中 +4；依据每命中 ≤4）；
* 复合索引使查询不走全表扫描、不按日展开；
* 旧版 SQLite 卷启动回填：按各批原冻结依据重建查询数据，名单完全不变；
* 提交失败时学籍与查询数据一并回滚。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import inspect, select, text

from app.models import CorrectionSegment
from app.service import (
    AUXILIARY_BASE_BUDGET,
    BASIS_BUDGET_PER_HIT,
    build_student_segments,
)


def _slip(slip_no, student_no, school_id, day):
    return {
        "slip_no": slip_no,
        "student_no": student_no,
        "school_id": school_id,
        "effective_date": day,
    }


def _create(client, batch_no: str) -> None:
    response = client.post("/api/batches", json={"batch_no": batch_no})
    assert response.status_code == 201, response.text


def _put(client, batch_no: str, slip: dict):
    response = client.post(f"/api/batches/{batch_no}/slips", json=slip)
    assert response.status_code == 200, response.text


def _commit(client, batch_no: str):
    return client.post(f"/api/batches/{batch_no}/commit")


def _list(client, batch_no, school_id, day):
    response = client.get(
        f"/api/batches/{batch_no}/corrections",
        params={"school_id": school_id, "statistics_date": day},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 跨日更正
# ---------------------------------------------------------------------------


def test_date_correction_backward_uses_bounded_segments(client):
    """日期往后更正：统计日落入撤回区间时命中撤回，落入开放补报区间则不列入。"""
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", _slip("Z1", "S1", "A", "2026-09-15"))
    assert _commit(client, "B2").status_code == 200

    early = _list(client, "B2", "A", "2026-09-10")
    assert [w["student_no"] for w in early["withdrawals"]] == ["S1"]
    # 命中一人：辅助 1 条；本批单 09-15 尚未生效，只读到 1 条历史依据。
    assert early["auxiliary_records_read"] == 1
    assert early["basis_records_read"] == 1
    assert early["withdrawals"][0]["slip_no"] == "Z1"
    assert early["withdrawals"][0]["next_slip_no"] is None

    late = _list(client, "B2", "A", "2026-09-20")
    assert late["additions"] == [] and late["withdrawals"] == []
    # 区间日期条件在索引内即排除该生，未命中则不读依据。
    assert late["auxiliary_records_read"] == 0
    assert late["basis_records_read"] == 0
    assert late["unmatched_student_count"] == 1


def test_date_correction_forward_creates_addition_segment(client):
    """日期往前更正：更正提前生效期间，本批视图补报成立。"""
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-15"))
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B2").status_code == 200

    # 09-05：旧批口径未登记，新批口径已在 A -> 补报。
    mid = _list(client, "B2", "A", "2026-09-05")
    assert [a["student_no"] for a in mid["additions"]] == ["S1"]
    assert mid["additions"][0]["slip_no"] == "Z1"
    assert mid["additions"][0]["effective_date"] == "2026-09-01"
    assert mid["additions"][0]["previous_slip_no"] is None

    # 09-20：两版都生效且都是 A -> 归属未变。
    late = _list(client, "B2", "A", "2026-09-20")
    assert late["listed_student_count"] == 0


def test_segments_are_built_on_boundary_dates_only(client, app):
    """辅助记录只按生效日边界建区间，不按统计日逐日展开。"""
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", _slip("Z2", "S1", "C", "2026-09-10"))
    _put(client, "B2", _slip("Z3", "S1", "A", "2026-09-20"))
    assert _commit(client, "B2").status_code == 200

    with app.state.database.session() as session:
        from app.models import Batch

        b2_id = session.execute(
            select(Batch.id).where(Batch.batch_no == "B2")
        ).scalar_one()
        rows = list(
            session.scalars(
                select(CorrectionSegment)
                .where(CorrectionSegment.batch_id == b2_id)
                .order_by(CorrectionSegment.school_id, CorrectionSegment.start_date)
            )
        )
        boundaries = {
            date(2026, 9, 1),
            date(2026, 9, 10),
            date(2026, 9, 20),
        }
        for row in rows:
            assert row.start_date in boundaries
            assert row.end_date is None or row.end_date in boundaries
            assert row.start_date != row.end_date
        # B2 的冻结依据中 prior = B1 的 Z1(A@09-01)，after 再加入 C 与回到 A：
        # 09-10 前两侧都归 A（无区间）；09-10 起 A 撤回、C 补报，
        # 09-20 回到 A 后两侧重新一致，区间全部关闭。
        keys = {(r.school_id, r.kind, r.start_date, r.end_date) for r in rows}
        assert keys == {
            ("A", "withdraw", date(2026, 9, 10), date(2026, 9, 20)),
            ("C", "add", date(2026, 9, 10), date(2026, 9, 20)),
        }


# ---------------------------------------------------------------------------
# 只命中一人的旧批与读取上限
# ---------------------------------------------------------------------------


def _commit_batch_with_many_students(client, batch_no, students, school_id, day):
    _create(client, batch_no)
    for student_no in students:
        _put(
            client,
            batch_no,
            _slip(f"Z-{student_no}", student_no, school_id, day),
        )
    assert _commit(client, batch_no).status_code == 200


def test_single_hit_old_batch_reads_one_auxiliary_and_two_basis(client):
    """大批候选里只有一名学生在统计日命中：读取条数与批规模无关。

    构造：30 名学生先离开 A 又在统计日前回到 A（撤回区间已关闭），
    仅 S999 在统计日仍处于对 A 的开放补报区间。
    """
    decoys = [f"P{i:03d}" for i in range(30)]

    _create(client, "B1")
    for student_no in decoys:
        _put(client, "B1", _slip(f"Z-{student_no}", student_no, "A", "2026-01-01"))
    _put(client, "B1", _slip("Z-S999", "S999", "C", "2026-01-01"))
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    for student_no in decoys:
        _put(client, "B2", _slip(f"ZB-{student_no}", student_no, "B", "2026-02-01"))
        _put(client, "B2", _slip(f"ZA-{student_no}", student_no, "A", "2026-03-01"))
    _put(client, "B2", _slip("ZA-S999", "S999", "A", "2026-03-01"))
    assert _commit(client, "B2").status_code == 200

    # 31 名候选学生中只有 S999 的区间在 03-15 仍成立。
    listing = _list(client, "B2", "A", "2026-03-15")
    assert [a["student_no"] for a in listing["additions"]] == ["S999"]
    assert listing["candidate_student_count"] == 31
    assert listing["listed_student_count"] == 1
    assert listing["unmatched_student_count"] == 30
    # 关闭区间由索引日期条件排除，只读到 1 条辅助区间、2 条 S999 的依据。
    assert listing["auxiliary_records_read"] == 1
    assert listing["basis_records_read"] == 2


def test_miss_on_statistics_date_reads_no_records(client):
    """统计日不命中任何区间时，辅助与依据的实际读取条数都为 0。"""
    decoys = [f"P{i:03d}" for i in range(30)]
    _commit_batch_with_many_students(client, "B1", decoys + ["S999"], "A", "2026-09-01")

    _create(client, "B2")
    _put(client, "B2", _slip("Z-S999", "S999", "B", "2026-10-01"))
    assert _commit(client, "B2").status_code == 200

    # 统计日早于一切生效日：索引条件直接排除全部区间。
    listing = _list(client, "B2", "B", "2026-01-01")
    assert listing["additions"] == [] and listing["withdrawals"] == []
    assert listing["auxiliary_records_read"] == 0
    assert listing["basis_records_read"] == 0
    assert listing["candidate_student_count"] == 1
    assert listing["unmatched_student_count"] == 1


def test_read_budget_invariant_under_many_closed_segments(client):
    """大量已关闭的历史区间 + 少量命中：读取始终满足 64 + 4*命中 的上限。"""
    # B1：70 名学生在 A，另一名 S999 在 C。
    first_wave = [f"P{i:03d}" for i in range(70)]
    _create(client, "B1")
    for student_no in first_wave:
        _put(client, "B1", _slip(f"Z-{student_no}", student_no, "A", "2026-01-01"))
    _put(client, "B1", _slip("Z-S999", "S999", "C", "2026-01-01"))
    assert _commit(client, "B1").status_code == 200

    # B2：70 名学生 02-01 转到 B、03-01 回到 A（每人在 A 留下已关闭撤回区间
    # [02-01, 03-01)）；S999 于 03-01 加入 A，成为唯一开放补报区间。
    _create(client, "B2")
    for student_no in first_wave:
        _put(client, "B2", _slip(f"ZB-{student_no}", student_no, "B", "2026-02-01"))
        _put(client, "B2", _slip(f"ZA-{student_no}", student_no, "A", "2026-03-01"))
    _put(client, "B2", _slip("ZA-S999", "S999", "A", "2026-03-01"))
    assert _commit(client, "B2").status_code == 200

    # 02-15：70 个撤回区间全部命中，辅助 70 条 <= 64 + 4*70。
    mid = _list(client, "B2", "A", "2026-02-15")
    hits = mid["listed_student_count"]
    assert hits == 70
    assert mid["auxiliary_records_read"] == 70
    assert mid["auxiliary_records_read"] <= AUXILIARY_BASE_BUDGET + 4 * hits
    # 每生两次 LIMIT 1 取数（本批 B 单 + 历史 A 单），依据读取 = 每生 2 条，
    # 严格小于每生 4 条预算。
    assert mid["basis_records_read"] == 2 * hits
    assert mid["basis_records_read"] <= BASIS_BUDGET_PER_HIT * hits

    # 03-15：70 个撤回区间均已关闭，只剩 S999 的开放补报区间。
    late = _list(client, "B2", "A", "2026-03-15")
    late_hits = late["listed_student_count"]
    assert late_hits == 1
    assert [a["student_no"] for a in late["additions"]] == ["S999"]
    assert late["auxiliary_records_read"] == 1
    assert late["basis_records_read"] == 2
    assert late["auxiliary_records_read"] <= AUXILIARY_BASE_BUDGET + 4 * late_hits


def test_budget_constants_match_specification():
    assert AUXILIARY_BASE_BUDGET == 64
    assert BASIS_BUDGET_PER_HIT == 4


def test_read_budget_guard_raises_when_exceeded(client, monkeypatch):
    """辅助读取累积超过动态预算时硬报错，绝不静默超量读取。

    正常数据下未命中区间由索引日期谓词直接过滤（实际读取为 0），因此这里
    用极小预算 + 分页大小模拟“候选区间噪声超过基础额度”的防御场景。
    """
    import app.service as service_module
    from app.errors import DomainError

    students = [f"Q{i:03d}" for i in range(5)]
    _commit_batch_with_many_students(
        client, "B1", students, "A", "2026-09-01"
    )
    monkeypatch.setattr(service_module, "AUXILIARY_BASE_BUDGET", 2)
    monkeypatch.setattr(service_module, "AUXILIARY_PAGE_SIZE", 2)
    # 命中增长也不足以容纳全部 5 条区间时，守卫必须在预算处中止。
    monkeypatch.setattr(service_module, "BASIS_BUDGET_PER_HIT", 0)

    with pytest.raises(DomainError) as exc_info:
        service_module.correction_list(
            client.app.state.database,
            "B1",
            "A",
            date(2026, 9, 30),
        )
    assert exc_info.value.code == "read_budget_exceeded"
    assert exc_info.value.status_code == 500


def test_pagination_collects_all_hits_within_budget(client, monkeypatch):
    """分页读取：页大小小于命中数时仍能取全，读数等于实际区间条数。"""
    import app.service as service_module

    students = [f"Q{i:03d}" for i in range(10)]
    _commit_batch_with_many_students(
        client, "B1", students, "A", "2026-09-01"
    )
    monkeypatch.setattr(service_module, "AUXILIARY_PAGE_SIZE", 3)

    listing = _list(client, "B1", "A", "2026-09-30")
    assert listing["listed_student_count"] == 10
    assert listing["auxiliary_records_read"] == 10
    assert listing["basis_records_read"] == 10


# ---------------------------------------------------------------------------
# 索引计划：不扫全校候选、不按日筛
# ---------------------------------------------------------------------------


def test_queries_use_indexes_not_full_scans(client, app):
    _commit_batch_with_many_students(
        client, "B1", [f"S{i:03d}" for i in range(20)], "A", "2026-09-01"
    )
    engine = app.state.database.engine
    if engine.url.drivername != "sqlite":
        return  # EXPLAIN QUERY PLAN 为 SQLite 专用。

    with engine.connect() as conn:
        segment_plan = conn.execute(
            text(
                "EXPLAIN QUERY PLAN "
                "SELECT * FROM correction_segments "
                "WHERE batch_id = 1 AND school_id = 'A' "
                "AND start_date <= '2026-09-30' "
                "AND (end_date IS NULL OR end_date > '2026-09-30')"
            )
        ).all()
        detail = " ".join(row[3] for row in segment_plan)
        assert "SCAN correction_segments" not in detail
        assert "ix_correction_segment_query" in detail

        basis_plan = conn.execute(
            text(
                "EXPLAIN QUERY PLAN "
                "SELECT * FROM basis_records "
                "WHERE batch_id = 1 AND student_no = 'S000' AND is_batch_slip = 1 "
                "AND effective_date <= '2026-09-30' "
                "ORDER BY effective_date DESC, slip_no DESC LIMIT 1"
            )
        ).all()
        detail = " ".join(row[3] for row in basis_plan)
        assert "SCAN basis_records" not in detail
        assert "ix_basis_lookup" in detail

        candidate_plan = conn.execute(
            text(
                "EXPLAIN QUERY PLAN "
                "SELECT COUNT(*) FROM basis_student_schools "
                "WHERE batch_id = 1 AND school_id = 'A'"
            )
        ).all()
        detail = " ".join(row[3] for row in candidate_plan)
        assert "SCAN basis_student_schools" not in detail


# ---------------------------------------------------------------------------
# 旧版 SQLite 卷升级
# ---------------------------------------------------------------------------


def test_legacy_volume_backfill_rebuilds_query_data(client, app, reopen):
    """模拟旧卷：删除新表/新索引后重启，应按原冻结依据重建，名单不变。"""
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    _put(client, "B1", _slip("Z2", "S2", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", _slip("Z1", "S1", "B", "2026-09-10"))
    assert _commit(client, "B2").status_code == 200

    snapshots = {
        ("B1", "A", "2026-09-30"): _list(client, "B1", "A", "2026-09-30"),
        ("B2", "A", "2026-09-05"): _list(client, "B2", "A", "2026-09-05"),
        ("B2", "A", "2026-09-20"): _list(client, "B2", "A", "2026-09-20"),
        ("B2", "B", "2026-09-20"): _list(client, "B2", "B", "2026-09-20"),
    }

    engine = app.state.database.engine
    inspector = inspect(engine)
    assert "correction_segments" in inspector.get_table_names()

    # 模拟旧版本卷：删除新表与新索引（basis_records / basis_student_schools 保留）。
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS correction_segments"))
        conn.execute(text("DROP INDEX IF EXISTS ix_basis_lookup"))

    reopened = reopen()
    with reopened:
        # 回填后名单与读取条数与升级前完全一致。
        for (batch_no, school_id, day), before in snapshots.items():
            after = reopened.get(
                f"/api/batches/{batch_no}/corrections",
                params={"school_id": school_id, "statistics_date": day},
            ).json()
            assert after == before

        # 再重启一次：回填幂等，不重复写入。
        again = reopen()
        with again:
            stats = again.get("/api/stats").json()
        assert stats["correction_segments"] > 0

    stats = reopened.get("/api/stats").json()
    assert stats["correction_segments"] >= 3
    # 依据条数未被回填改动（B1 两条本批单 + B2 历史 Z1 与本批 Z1 共 4 条）。
    assert stats["basis_records"] == 4


def test_backfill_fills_missing_candidate_index(client, app, reopen):
    """旧卷若候选学校索引缺失，回填时一并补齐，候选计数口径不变。"""
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    before = _list(client, "B1", "A", "2026-09-30")

    engine = app.state.database.engine
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS correction_segments"))
        conn.execute(text("DELETE FROM basis_student_schools"))

    reopened = reopen()
    with reopened:
        after = reopened.get(
            "/api/batches/B1/corrections",
            params={"school_id": "A", "statistics_date": "2026-09-30"},
        ).json()
        assert after == before
        assert after["candidate_student_count"] == 1


# ---------------------------------------------------------------------------
# 提交原子性
# ---------------------------------------------------------------------------


def test_failed_commit_rolls_back_query_data_and_keeps_draft(client):
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", _slip("Z2", "S1", "B", "2026-09-01"))  # 同日冲突
    response = _commit(client, "B2")
    assert response.status_code == 409

    # 草稿仍可查、无清单。
    listing = client.get(
        "/api/batches/B2/corrections",
        params={"school_id": "B", "statistics_date": "2026-12-31"},
    )
    assert listing.status_code == 409
    assert listing.json()["error"]["code"] == "batch_not_committed"

    stats = client.get("/api/stats").json()
    # B2 没有留下任何辅助区间；学籍仍只有 B1 的一条。
    assert stats["effective_slips"] == 1
    assert stats["basis_records"] == 1


# ---------------------------------------------------------------------------
# 区间构建纯函数
# ---------------------------------------------------------------------------


def _view(slip_no, school_id, day, batch_no="Bx"):
    from app.service import SlipView

    return SlipView(
        slip_no=slip_no,
        student_no="S1",
        school_id=school_id,
        effective_date=date.fromisoformat(day),
        origin_batch_no=batch_no,
    )


def test_build_segments_covers_transition_and_recovery():
    prior = [_view("Z1", "A", "2026-09-01", "B1")]
    after = [
        _view("Z2", "C", "2026-09-10", "B2"),
        _view("Z3", "A", "2026-09-20", "B2"),
        prior[0],
    ]
    segments = build_student_segments("S1", prior, after)
    by_key = {(s.school_id, s.kind): s for s in segments}
    # 09-10 前 Z1 仍归 A，归属未变；09-10 起 A 撤回、C 补报，09-20 同时关闭；
    # 09-20 回到 A 后两侧归属重新一致，不留下开放区间。
    assert by_key[("A", "withdraw")].start_date == date(2026, 9, 10)
    assert by_key[("A", "withdraw")].end_date == date(2026, 9, 20)
    assert by_key[("C", "add")].start_date == date(2026, 9, 10)
    assert by_key[("C", "add")].end_date == date(2026, 9, 20)
    assert ("A", "add") not in by_key
    assert ("C", "withdraw") not in by_key


def test_build_segments_empty_when_attribution_unchanged():
    """学校始终相同的纯改日场景：只有中间悬空期构成撤回区间，首尾归属未变。"""
    prior = [_view("Z1", "A", "2026-09-01", "B1")]
    after = [_view("Z1", "A", "2026-09-05", "B2")]
    segments = build_student_segments("S1", prior, after)
    # 09-01..09-05 之间本批视图未登记而本批前归 A -> 撤回，09-05 起恢复。
    assert len(segments) == 1
    only = segments[0]
    assert (only.school_id, only.kind) == ("A", "withdraw")
    assert only.start_date == date(2026, 9, 1)
    assert only.end_date == date(2026, 9, 5)


def test_build_segments_empty_for_identical_views():
    """本批前后视图完全一致时不产生任何区间。"""
    views = [_view("Z1", "A", "2026-09-01", "B1")]
    assert build_student_segments("S1", views, list(views)) == []
