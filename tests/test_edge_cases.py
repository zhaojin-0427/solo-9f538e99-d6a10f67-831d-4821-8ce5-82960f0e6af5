"""边界场景与输入校验测试。"""

from __future__ import annotations


def _create(client, batch_no):
    assert client.post("/api/batches", json={"batch_no": batch_no}).status_code == 201


def _put(client, batch_no, slip_no, student_no, school_id, day):
    response = client.post(
        f"/api/batches/{batch_no}/slips",
        json={
            "slip_no": slip_no,
            "student_no": student_no,
            "school_id": school_id,
            "effective_date": day,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _commit(client, batch_no):
    return client.post(f"/api/batches/{batch_no}/commit")


def _list(client, batch_no, school_id, day):
    response = client.get(
        f"/api/batches/{batch_no}/corrections",
        params={"school_id": school_id, "statistics_date": day},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_multiple_transitions_at_different_dates(client):
    """一个学生多段转学：不同统计日可能补报、撤回或不变。"""
    _create(client, "B1")
    _put(client, "B1", "Z1", "S1", "A", "2026-09-01")
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", "Z2", "S1", "C", "2026-09-10")
    _put(client, "B2", "Z3", "S1", "A", "2026-09-20")
    assert _commit(client, "B2").status_code == 200

    # 09-05：仍在 A，未变化。
    assert _list(client, "B2", "A", "2026-09-05")["listed_student_count"] == 0
    # 09-15：A -> C，A 撤回、C 补报。
    a_mid = _list(client, "B2", "A", "2026-09-15")
    assert [w["student_no"] for w in a_mid["withdrawals"]] == ["S1"]
    c_mid = _list(client, "B2", "C", "2026-09-15")
    assert [a["student_no"] for a in c_mid["additions"]] == ["S1"]
    # 09-25：又回到 A，与更正前一致 -> 归属未变，不列入。
    a_late = _list(client, "B2", "A", "2026-09-25")
    assert a_late["listed_student_count"] == 0


def test_batches_can_commit_out_of_creation_order(client):
    """先提交的批次不因后提交批次而改变结论；后提交批次按已生效状态取依据。"""
    _create(client, "B1")
    _create(client, "B2")
    _put(client, "B1", "Z1", "S1", "A", "2026-09-01")
    _put(client, "B2", "Z2", "S1", "B", "2026-09-10")
    assert _commit(client, "B2").status_code == 200
    assert _commit(client, "B1").status_code == 200

    # B2 先提交：当时无任何已生效单据，学生归入 B。
    b2_b = _list(client, "B2", "B", "2026-09-15")
    assert [a["student_no"] for a in b2_b["additions"]] == ["S1"]
    # B2 之后 B1 提交，不改变 B2 的冻结依据。
    assert _list(client, "B2", "A", "2026-09-15")["candidate_student_count"] == 0

    # B1 后提交：其依据已包含 B2 的已生效单。
    b1 = _list(client, "B1", "A", "2026-09-05")
    assert [a["student_no"] for a in b1["additions"]] == ["S1"]
    # 统计日 09-15 时归属仍为 B（B2 的单更新），B1 的 A 视图不列入。
    assert _list(client, "B1", "A", "2026-09-15")["listed_student_count"] == 0


def test_delete_slip_before_commit(client):
    _create(client, "B1")
    _put(client, "B1", "Z1", "S1", "A", "2026-09-01")
    assert client.delete("/api/batches/B1/slips/Z1").status_code == 204
    assert client.get("/api/batches/B1/slips").json() == []
    response = _commit(client, "B1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "batch_empty"


def test_delete_missing_slip(client):
    _create(client, "B1")
    response = client.delete("/api/batches/B1/slips/NOPE")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "slip_not_found"


def test_unknown_batch(client):
    assert client.get("/api/batches/NOPE").status_code == 404
    assert _commit(client, "NOPE").status_code == 404
    assert (
        client.get(
            "/api/batches/NOPE/corrections",
            params={"school_id": "A", "statistics_date": "2026-09-01"},
        ).status_code
        == 404
    )


def test_validation_rejects_empty_and_bad_dates(client):
    response = client.post("/api/batches", json={"batch_no": "   "})
    assert response.status_code == 422

    _create(client, "B1")
    bad_date = client.post(
        "/api/batches/B1/slips",
        json={
            "slip_no": "Z1",
            "student_no": "S1",
            "school_id": "A",
            "effective_date": "not-a-date",
        },
    )
    assert bad_date.status_code == 422

    assert (
        client.get(
            "/api/batches/B1/corrections",
            params={"school_id": "A", "statistics_date": "2026-13-01"},
        ).status_code
        == 422
    )


def test_slip_bound_to_student_across_draft_batches(client):
    _create(client, "B1")
    _put(client, "B1", "Z1", "S1", "A", "2026-09-01")

    _create(client, "B2")
    response = client.post(
        "/api/batches/B2/slips",
        json={
            "slip_no": "Z1",
            "student_no": "S2",
            "school_id": "A",
            "effective_date": "2026-09-01",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "slip_student_immutable"


def test_conflicting_same_day_conflict_across_students_ok(client):
    """同日多单只要属于不同学生即可。"""
    _create(client, "B1")
    _put(client, "B1", "Z1", "S1", "A", "2026-09-01")
    _put(client, "B1", "Z2", "S2", "B", "2026-09-01")
    assert _commit(client, "B1").status_code == 200


def test_correction_inside_same_batch(client):
    """同一批内更正日期与学校，提交时只应用最新版本。"""
    _create(client, "B1")
    _put(client, "B1", "Z1", "S1", "A", "2026-09-01")
    _put(client, "B1", "Z1", "S1", "B", "2026-09-10")
    receipt = _commit(client, "B1").json()
    assert receipt["slip_count"] == 1

    a_list = _list(client, "B1", "A", "2026-09-30")
    assert a_list["additions"] == [] and a_list["withdrawals"] == []
    b_list = _list(client, "B1", "B", "2026-09-30")
    assert [a["student_no"] for a in b_list["additions"]] == ["S1"]
    assert b_list["additions"][0]["slip_no"] == "Z1"


def test_no_change_batch_retains_no_basis(client):
    """批次内容与已生效状态完全一致时，不产生依据、不进任何学校名单。"""
    _create(client, "B1")
    _put(client, "B1", "Z1", "S1", "A", "2026-09-01")
    assert _commit(client, "B1").status_code == 200

    _create(client, "B2")
    _put(client, "B2", "Z1", "S1", "A", "2026-09-01")
    receipt = _commit(client, "B2").json()
    assert receipt["basis_record_count"] == 0
    assert receipt["involved_student_count"] == 0

    listing = _list(client, "B2", "A", "2026-09-30")
    assert listing["candidate_student_count"] == 0
    assert listing["basis_records_read"] == 0
    assert listing["listed_student_count"] == 0


def test_receipt_contains_batch_no_and_basis_counts(client):
    _create(client, "B1")
    _put(client, "B1", "Z1", "S1", "A", "2026-09-01")
    body = _commit(client, "B1").json()
    assert body["batch_no"] == "B1"
    assert (
        body["basis_record_count"]
        == body["basis_prior_record_count"] + body["basis_batch_record_count"]
    )


def test_list_batches_and_status_filter(client):
    _create(client, "B1")
    _create(client, "B2")
    _put(client, "B2", "Z1", "S1", "A", "2026-09-01")
    assert _commit(client, "B2").status_code == 200

    everything = client.get("/api/batches").json()
    assert [b["batch_no"] for b in everything] == ["B1", "B2"]

    drafts = client.get("/api/batches", params={"status": "draft"}).json()
    assert [b["batch_no"] for b in drafts] == ["B1"]

    committed = client.get("/api/batches", params={"status": "committed"}).json()
    assert [b["batch_no"] for b in committed] == ["B2"]


def test_stats_endpoint(client):
    _create(client, "B1")
    _put(client, "B1", "Z1", "S1", "A", "2026-09-01")
    assert _commit(client, "B1").status_code == 200
    stats = client.get("/api/stats").json()
    assert stats["batches"] == 1
    assert stats["committed_batches"] == 1
    assert stats["effective_slips"] == 1
    assert stats["basis_records"] == 1
