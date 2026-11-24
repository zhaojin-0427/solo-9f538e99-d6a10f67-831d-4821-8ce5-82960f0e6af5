"""学籍更正清单语义测试。"""

from __future__ import annotations


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
    return response.json()


def _commit(client, batch_no: str):
    return client.post(f"/api/batches/{batch_no}/commit")


def _list(client, batch_no, school_id, day):
    response = client.get(
        f"/api/batches/{batch_no}/corrections",
        params={"school_id": school_id, "statistics_date": day},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_basic_addition_and_withdrawal(client):
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    _put(client, "B1", _slip("Z2", "S2", "A", "2026-09-01"))

    receipt = _commit(client, "B1")
    assert receipt.status_code == 200, receipt.text
    body = receipt.json()
    assert body["batch_no"] == "B1"
    assert body["slip_count"] == 2
    # 无历史单据：两条依据均为本批单据快照。
    assert body["basis_record_count"] == 2
    assert body["basis_prior_record_count"] == 0
    assert body["basis_batch_record_count"] == 2

    a_list = _list(client, "B1", "A", "2026-09-10")
    assert a_list["addition_count"] == 2
    assert a_list["withdrawal_count"] == 0
    assert {item["student_no"] for item in a_list["additions"]} == {"S1", "S2"}
    # 两名候选学生共读取 2 条辅助区间、2 条依据。
    assert a_list["candidate_student_count"] == 2
    assert a_list["auxiliary_records_read"] == 2
    assert a_list["basis_records_read"] == 2

    # 统计日早于生效日：归属不变（均未登记），不列入。
    early = _list(client, "B1", "A", "2026-08-31")
    assert early["addition_count"] == 0
    assert early["withdrawal_count"] == 0
    assert early["listed_student_count"] == 0


def test_prior_records_are_included_in_basis(client):
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", _slip("Z2", "S1", "B", "2026-09-10"))
    receipt = _commit(client, "B2").json()
    # 本批之前 1 条（Z1）+ 本批 1 条（Z2）。
    assert receipt["basis_prior_record_count"] == 1
    assert receipt["basis_batch_record_count"] == 1
    assert receipt["basis_record_count"] == 2

    a_list = _list(client, "B2", "A", "2026-09-20")
    assert [i["student_no"] for i in a_list["withdrawals"]] == ["S1"]
    assert a_list["withdrawals"][0]["next_school_id"] == "B"

    b_list = _list(client, "B2", "B", "2026-09-20")
    assert [i["student_no"] for i in b_list["additions"]] == ["S1"]


def test_attribution_unchanged_is_not_listed(client):
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    # 本批改的是生效日，学校仍为 A。
    _create(client, "B2")
    _put(client, "B2", _slip("Z1", "S1", "A", "2026-09-05"))
    receipt = _commit(client, "B2").json()
    # 依据包含更正前的 Z1 与更正后的 Z1 两个版本，才能判定归属变化。
    assert receipt["basis_record_count"] == 2
    assert receipt["basis_prior_record_count"] == 1
    assert receipt["basis_batch_record_count"] == 1

    # 统计日 09-03：更正前属于 A，更正后还没生效 -> 撤回。
    early = _list(client, "B2", "A", "2026-09-03")
    assert len(early["withdrawals"]) == 1

    # 统计日 09-06：更正前属于 A，更正后仍属于 A -> 归属未变，不列入。
    late = _list(client, "B2", "A", "2026-09-06")
    assert late["additions"] == []
    assert late["withdrawals"] == []
    assert late["candidate_student_count"] == 1
    assert late["unmatched_student_count"] == 1


def test_student_day_conflict_rejected_without_side_effects(client):
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", _slip("Z2", "S1", "B", "2026-09-01"))
    response = _commit(client, "B2")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "student_day_conflict"

    # 未成功提交：批次仍为草稿，不产生清单，学籍未变。
    detail = client.get("/api/batches/B2").json()
    assert detail["status"] == "draft"
    assert detail["receipt"] is None
    listing = client.get(
        "/api/batches/B2/corrections",
        params={"school_id": "B", "statistics_date": "2026-12-31"},
    )
    assert listing.status_code == 409
    assert listing.json()["error"]["code"] == "batch_not_committed"

    # 学生仍在 A。
    a_list = _list(client, "B1", "A", "2026-12-31")
    assert [i["student_no"] for i in a_list["additions"]] == ["S1"]


def test_slip_student_is_immutable(client):
    _create(client, "B1")
    first = _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert first["action"] == "created"

    response = client.post(
        "/api/batches/B1/slips",
        json=_slip("Z1", "S2", "A", "2026-09-01"),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "slip_student_immutable"

    # 日期和学校可以改。
    correction = client.post(
        "/api/batches/B1/slips",
        json=_slip("Z1", "S1", "B", "2026-09-20"),
    ).json()
    assert correction["action"] == "updated"
    assert correction["slip"]["school_id"] == "B"
    assert correction["slip"]["effective_date"] == "2026-09-20"


def test_committed_batch_is_read_only(client):
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    response = client.post(
        "/api/batches/B1/slips",
        json=_slip("Z1", "S1", "B", "2026-09-01"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "batch_not_editable"

    again = _commit(client, "B1")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "batch_not_committed"


def test_empty_batch_cannot_commit(client):
    _create(client, "B1")
    response = _commit(client, "B1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "batch_empty"


def test_duplicate_batch_no_conflicts(client):
    _create(client, "B1")
    response = client.post("/api/batches", json={"batch_no": "B1"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "batch_exists"


def test_later_backfill_does_not_change_old_report(client):
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", _slip("Z2", "S1", "B", "2026-09-10"))
    assert _commit(client, "B2").status_code == 200

    # 旧批清单内容不变：仍认为 S1 在 B1 后加入 A。
    old = _list(client, "B1", "A", "2026-09-30")
    assert [i["student_no"] for i in old["additions"]] == ["S1"]
    assert old["auxiliary_records_read"] == 1
    assert old["basis_records_read"] == 1

    # 新批清单按自己的依据计算。
    new = _list(client, "B2", "A", "2026-09-30")
    assert [i["student_no"] for i in new["withdrawals"]] == ["S1"]
    assert new["auxiliary_records_read"] == 1
    assert new["basis_records_read"] == 2


def test_backfill_only_checks_its_own_students(client):
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    _put(client, "B1", _slip("Z2", "S2", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    # 只补录 S2，不涉及 S1：依据只含 S2 的记录。
    _create(client, "B2")
    _put(client, "B2", _slip("Z3", "S2", "B", "2026-09-10"))
    receipt = _commit(client, "B2").json()
    assert receipt["affected_student_count"] == 1
    assert receipt["basis_prior_record_count"] == 1
    assert receipt["basis_batch_record_count"] == 1

    # 查询 A 校只读取 S2 的辅助区间与 2 条依据，与 S1 无关。
    listing = _list(client, "B2", "A", "2026-09-30")
    assert listing["candidate_student_count"] == 1
    assert listing["auxiliary_records_read"] == 1
    assert listing["basis_records_read"] == 2
    assert [i["student_no"] for i in listing["withdrawals"]] == ["S2"]


def test_unmatched_reads_are_counted(client):
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", _slip("Z2", "S1", "B", "2026-09-10"))
    assert _commit(client, "B2").status_code == 200

    # A 校：统计日 09-20 命中撤回，读取本批/历史依据各 1 条 + 1 条辅助区间。
    hit = _list(client, "B2", "A", "2026-09-20")
    assert len(hit["withdrawals"]) == 1
    assert hit["auxiliary_records_read"] == 1
    assert hit["basis_records_read"] == 2

    # 统计日 09-05：更正尚未生效，归属仍为 A -> 辅助区间在索引内即被日期条件
    # 排除，未命中；不读取任何复查依据。
    miss_early = _list(client, "B2", "A", "2026-09-05")
    assert miss_early["additions"] == [] and miss_early["withdrawals"] == []
    assert miss_early["candidate_student_count"] == 1
    assert miss_early["auxiliary_records_read"] == 0
    assert miss_early["basis_records_read"] == 0
    assert miss_early["unmatched_student_count"] == 1

    # 统计日 08-31：两边都未登记 -> 同样未命中，不读取依据。
    miss = _list(client, "B2", "A", "2026-08-31")
    assert miss["additions"] == [] and miss["withdrawals"] == []
    assert miss["candidate_student_count"] == 1
    assert miss["auxiliary_records_read"] == 0
    assert miss["basis_records_read"] == 0
    assert miss["unmatched_student_count"] == 1


def test_report_survives_restart(client, reopen):
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200
    before = _list(client, "B1", "A", "2026-09-30")

    other = reopen()
    with other:
        after = other.get(
            "/api/batches/B1/corrections",
            params={"school_id": "A", "statistics_date": "2026-09-30"},
        ).json()
        detail = other.get("/api/batches/B1").json()

    assert after == before
    assert detail["status"] == "committed"
    assert detail["receipt"]["basis_record_count"] == 1


def test_date_correction_across_batches(client):
    """试跑：更正转学日期后，旧批结论不变，新批结论按更正后的日期。"""
    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", _slip("Z1", "S1", "A", "2026-09-15"))
    assert _commit(client, "B2").status_code == 200

    # 旧批：09-10 时仍按 09-01 生效。
    old = _list(client, "B1", "A", "2026-09-10")
    assert [i["student_no"] for i in old["additions"]] == ["S1"]

    # 新批：09-10 时更正后尚未生效，归属变为未登记 -> 撤回。
    new = _list(client, "B2", "A", "2026-09-10")
    assert [i["student_no"] for i in new["withdrawals"]] == ["S1"]

    # 09-20 后，更正后的日期已生效，归属仍为 A -> 新批不列入。
    after = _list(client, "B2", "A", "2026-09-20")
    assert after["additions"] == [] and after["withdrawals"] == []


def test_report_reads_frozen_basis_not_history(client, app):
    """即使外部改动了现行学籍表，旧批清单也不受影响（只读冻结依据）。"""
    from sqlalchemy import update

    from app.models import EffectiveSlip

    _create(client, "B1")
    _put(client, "B1", _slip("Z1", "S1", "A", "2026-09-01"))
    assert _commit(client, "B1").status_code == 200
    before = _list(client, "B1", "A", "2026-09-30")
    assert before["addition_count"] == 1

    with app.state.database.session() as session:
        session.execute(
            update(EffectiveSlip)
            .where(EffectiveSlip.slip_no == "Z1")
            .values(school_id="SCHOOL-X")
        )
        session.commit()

    after = _list(client, "B1", "A", "2026-09-30")
    assert after == before


def test_openapi_docs_available(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/batches/{batch_no}/corrections" in paths
    docs = client.get("/docs")
    assert docs.status_code == 200


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}
