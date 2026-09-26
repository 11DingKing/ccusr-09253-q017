"""身份合并案件 API：生命周期、合并链、循环拒绝、并发与重启恢复。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import services
from app.db import get_db
from app.main import app
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal


def _create_plan(client, plan=SHANGHAI_PLAN):
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text
    return plan["plan_version"]


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _correction(eid, student, seconds, reason=""):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


def _import(client, pv, events):
    resp = client.post(f"/api/plans/{pv}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _open_case(client, pv, case_id, survivor, merged):
    resp = client.post(
        f"/api/plans/{pv}/identity/cases",
        json={
            "case_id": case_id,
            "survivor_id": survivor,
            "merged_id": merged,
            "reason": "招生系统重复建档",
            "created_by": "registrar",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_evidence(client, pv, case_id, reference="DOC-1"):
    resp = client.post(
        f"/api/plans/{pv}/identity/cases/{case_id}/evidence",
        json={
            "evidence_type": "id_document",
            "reference": reference,
            "detail": {"issuer": "教务处"},
            "submitted_by": "registrar",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _approve(client, pv, case_id, actor="approver"):
    return client.post(
        f"/api/plans/{pv}/identity/cases/{case_id}/approve",
        json={"actor_id": actor, "reason": "证据核验通过"},
    )


def _seed_two_students(client, pv):
    _import(
        client,
        pv,
        [
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
            _checkin("E-02", "S2", "2024-03-15T10:00:00+08:00", "2024-03-15T11:00:00+08:00"),
            _correction("E-03", "S2", 1800, "补课"),
        ],
    )


def test_merge_lifecycle_aggregates_and_preserves_original_subjects(client):
    pv = _create_plan(client)
    _seed_two_students(client, pv)

    _open_case(client, pv, "C-1", survivor="S1", merged="S2")
    _add_evidence(client, pv, "C-1")
    approved = _approve(client, pv, "C-1")
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["state"] == "APPROVED"
    assert body["approved_version"] == 1
    assert len(body["evidence"]) == 1

    # 用被合并的旧学号查询：聚合到主身份，原始主体保留在记录上。
    progress = client.get(f"/api/plans/{pv}/students/S2/progress").json()
    assert progress["student_id"] == "S1"
    assert progress["source_student_ids"] == ["S1", "S2"]
    assert progress["total_seconds"] == 7200 + 3600 + 1800
    by_event = {c["event_id"]: c["student_id"] for c in progress["checkins"]}
    assert by_event == {"E-01": "S1", "E-02": "S2"}
    assert progress["adjustments"][0]["student_id"] == "S2"

    # 主学号查询得到同一份聚合结果。
    same = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert same["total_seconds"] == progress["total_seconds"]

    # 快照只保留规范身份，并标注身份版本。
    snap = client.get(f"/api/plans/{pv}/snapshot").json()
    assert snap["identity_version"] == 1
    assert [s["student_id"] for s in snap["students"]] == ["S1"]

    # 撤销：生成反向版本，学时重新分开，历史版本仍可重放。
    revoked = client.post(
        f"/api/plans/{pv}/identity/cases/C-1/revoke",
        json={"actor_id": "approver", "reason": "误合并，实为两人"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["state"] == "REVOKED"
    assert revoked.json()["revoked_version"] == 2

    versions = client.get(f"/api/plans/{pv}/identity/versions").json()
    assert [(v["seq"], v["action"]) for v in versions] == [(1, "merge"), (2, "unmerge")]

    snap_now = client.get(f"/api/plans/{pv}/snapshot").json()
    assert snap_now["identity_version"] == 2
    assert {s["student_id"] for s in snap_now["students"]} == {"S1", "S2"}

    snap_at_v1 = client.get(f"/api/plans/{pv}/snapshot?identity_version=1").json()
    assert [s["student_id"] for s in snap_at_v1["students"]] == ["S1"]

    alias_map_v1 = client.get(f"/api/plans/{pv}/identity/versions/1").json()
    assert alias_map_v1["aliases"] == {"S2": "S1"}
    alias_map_v2 = client.get(f"/api/plans/{pv}/identity/versions/2").json()
    assert alias_map_v2["aliases"] == {}


def test_approve_requires_evidence(client):
    pv = _create_plan(client)
    _open_case(client, pv, "C-1", survivor="S1", merged="S2")
    resp = _approve(client, pv, "C-1")
    assert resp.status_code == 422
    _add_evidence(client, pv, "C-1")
    assert _approve(client, pv, "C-1").status_code == 200


def test_impact_preview_reports_before_after_and_freezes(client):
    pv = _create_plan(client)
    _seed_two_students(client, pv)
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    _open_case(client, pv, "C-1", survivor="S1", merged="S2")

    preview = client.get(f"/api/plans/{pv}/identity/cases/C-1/preview").json()
    assert preview["applicable"] is True
    assert preview["rejection_reason"] is None
    assert preview["identity_version"] == 0
    assert preview["projected_version"] == 1
    assert preview["before"]["survivor"]["total_seconds"] == 7200
    assert preview["before"]["merged"]["total_seconds"] == 3600 + 1800
    assert preview["after"]["total_seconds"] == 7200 + 3600 + 1800
    assert preview["after"]["source_student_ids"] == ["S1", "S2"]
    assert preview["events_reattributed"] == 2  # S2 的签到与修正
    assert preview["freezes"] == [
        {"freeze_id": "F-01", "identity_version": 0, "immutable": True}
    ]

    # 预览不产生任何写入。
    assert client.get(f"/api/plans/{pv}/identity/versions").json() == []

    # 批准后再预览：映射已存在，标记为不适用并说明原因。
    _add_evidence(client, pv, "C-1")
    _approve(client, pv, "C-1")
    again = client.get(f"/api/plans/{pv}/identity/cases/C-1/preview").json()
    assert again["applicable"] is False
    assert again["rejection_reason"]
    assert again["after"] is None


def test_self_merge_rejected_at_open(client):
    pv = _create_plan(client)
    resp = client.post(
        f"/api/plans/{pv}/identity/cases",
        json={
            "case_id": "C-X",
            "survivor_id": "S1",
            "merged_id": "S1",
            "reason": "",
            "created_by": "registrar",
        },
    )
    assert resp.status_code == 422


def test_cycle_merge_rejected_at_approve(client):
    pv = _create_plan(client)
    _open_case(client, pv, "C-1", survivor="S1", merged="S2")
    _add_evidence(client, pv, "C-1")
    assert _approve(client, pv, "C-1").status_code == 200

    # 直接二元循环：S1 -> S2。
    _open_case(client, pv, "C-2", survivor="S2", merged="S1")
    _add_evidence(client, pv, "C-2")
    resp = _approve(client, pv, "C-2")
    assert resp.status_code == 422
    assert "循环" in resp.json()["detail"]

    # 延长合并链 S3 -> S2 合法；随后的 S1 -> S3 构成三元循环，拒绝。
    _open_case(client, pv, "C-3", survivor="S2", merged="S3")
    _add_evidence(client, pv, "C-3")
    assert _approve(client, pv, "C-3").status_code == 200

    _open_case(client, pv, "C-4", survivor="S3", merged="S1")
    _add_evidence(client, pv, "C-4")
    resp = _approve(client, pv, "C-4")
    assert resp.status_code == 422

    preview = client.get(f"/api/plans/{pv}/identity/cases/C-4/preview").json()
    assert preview["applicable"] is False
    assert "循环" in preview["rejection_reason"]


def test_merge_chain_replays_at_requested_identity_version(client):
    pv = _create_plan(client)
    _import(
        client,
        pv,
        [
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
            _checkin("E-02", "S2", "2024-03-15T09:00:00+08:00", "2024-03-15T10:00:00+08:00"),
            _checkin("E-03", "S3", "2024-03-15T10:00:00+08:00", "2024-03-15T11:00:00+08:00"),
        ],
    )
    _open_case(client, pv, "C-1", survivor="S1", merged="S2")
    _add_evidence(client, pv, "C-1")
    assert _approve(client, pv, "C-1").status_code == 200
    _open_case(client, pv, "C-2", survivor="S2", merged="S3")
    _add_evidence(client, pv, "C-2")
    assert _approve(client, pv, "C-2").status_code == 200

    # 版本 0：三个身份各自独立。
    snap0 = client.get(f"/api/plans/{pv}/snapshot?identity_version=0").json()
    assert {s["student_id"] for s in snap0["students"]} == {"S1", "S2", "S3"}

    # 版本 1：S2 并入 S1，S3 仍独立。
    snap1 = client.get(f"/api/plans/{pv}/snapshot?identity_version=1").json()
    by_id = {s["student_id"]: s for s in snap1["students"]}
    assert set(by_id) == {"S1", "S3"}
    assert by_id["S1"]["total_seconds"] == 7200

    # 最新版本（合并链 S3->S2->S1）：全部聚合到 S1。
    snap2 = client.get(f"/api/plans/{pv}/snapshot").json()
    assert snap2["identity_version"] == 2
    assert len(snap2["students"]) == 1
    merged = snap2["students"][0]
    assert merged["student_id"] == "S1"
    assert merged["total_seconds"] == 3 * 3600
    assert merged["source_student_ids"] == ["S1", "S2", "S3"]
    originals = {c["event_id"]: c["student_id"] for c in merged["checkins"]}
    assert originals == {"E-01": "S1", "E-02": "S2", "E-03": "S3"}

    # 用链尾的旧学号查询，不同版本下解析到不同规范身份。
    at_v1 = client.get(f"/api/plans/{pv}/students/S3/progress?identity_version=1").json()
    assert at_v1["student_id"] == "S3"
    at_latest = client.get(f"/api/plans/{pv}/students/S3/progress").json()
    assert at_latest["student_id"] == "S1"

    resp = client.get(f"/api/plans/{pv}/snapshot?identity_version=-1")
    assert resp.status_code == 422


def test_freeze_not_rewritten_by_merge_or_revoke(client):
    pv = _create_plan(client)
    _seed_two_students(client, pv)

    f1 = client.post(f"/api/plans/{pv}/freezes/F-01", json={}).json()
    assert f1["identity_version"] == 0
    assert {s["student_id"] for s in f1["students"]} == {"S1", "S2"}

    _open_case(client, pv, "C-1", survivor="S1", merged="S2")
    _add_evidence(client, pv, "C-1")
    _approve(client, pv, "C-1")

    # 已签发的冻结保持原样：两个独立身份、身份版本 0。
    f1_after = client.get(f"/api/plans/{pv}/freezes/F-01").json()
    assert f1_after == f1

    # 新冻结反映合并后的聚合，并记录身份版本 1。
    f2 = client.post(f"/api/plans/{pv}/freezes/F-02", json={}).json()
    assert f2["identity_version"] == 1
    assert [s["student_id"] for s in f2["students"]] == ["S1"]
    assert f2["students"][0]["total_seconds"] == 7200 + 3600 + 1800

    diff = client.get(f"/api/plans/{pv}/freezes/F-01/diff/F-02").json()
    assert diff["old_identity_version"] == 0
    assert diff["new_identity_version"] == 1
    changed = {c["student_id"]: c["change_type"] for c in diff["student_changes"]}
    assert changed == {"S1": "modified", "S2": "removed"}

    # 撤销合并后，两个已签发的冻结都不被改写。
    client.post(
        f"/api/plans/{pv}/identity/cases/C-1/revoke",
        json={"actor_id": "approver", "reason": "误合并"},
    )
    assert client.get(f"/api/plans/{pv}/freezes/F-01").json() == f1
    assert client.get(f"/api/plans/{pv}/freezes/F-02").json() == f2

    # 冻结解释按签发时的身份原样提供，不做别名解析。
    explained = client.get(f"/api/plans/{pv}/freezes/F-01/explain/S2").json()
    assert explained["student_id"] == "S2"
    assert explained["total_seconds"] == 3600 + 1800


def test_case_state_guards_and_not_found(client):
    pv = _create_plan(client)
    _open_case(client, pv, "C-1", survivor="S1", merged="S2")

    # 重复案件编号冲突。
    dup = client.post(
        f"/api/plans/{pv}/identity/cases",
        json={
            "case_id": "C-1",
            "survivor_id": "S1",
            "merged_id": "S2",
            "reason": "",
            "created_by": "registrar",
        },
    )
    assert dup.status_code == 409

    # 未批准的案件不能撤销。
    revoke = client.post(
        f"/api/plans/{pv}/identity/cases/C-1/revoke",
        json={"actor_id": "approver", "reason": "太早"},
    )
    assert revoke.status_code == 409

    _add_evidence(client, pv, "C-1")
    _approve(client, pv, "C-1")

    # 已批准的案件不能再补证据、不能重复批准。
    late_evidence = client.post(
        f"/api/plans/{pv}/identity/cases/C-1/evidence",
        json={
            "evidence_type": "id_document",
            "reference": "DOC-2",
            "detail": {},
            "submitted_by": "registrar",
        },
    )
    assert late_evidence.status_code == 409
    assert _approve(client, pv, "C-1").status_code == 409

    # 未知案件与未知培养方案。
    assert client.get(f"/api/plans/{pv}/identity/cases/NOPE").status_code == 404
    assert (
        client.post(
            f"/api/plans/{pv}/identity/cases/NOPE/approve",
            json={"actor_id": "a"},
        ).status_code
        == 404
    )
    assert client.get("/api/plans/NOPE/identity/cases").status_code == 404


def test_concurrent_event_import_stays_consistent(client):
    pv = _create_plan(client)
    from datetime import datetime, timedelta, timezone

    base = datetime(2024, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    events = []
    for i in range(100):
        start = base + timedelta(hours=3 * i)
        end = start + timedelta(hours=1)
        events.append(
            _checkin(
                f"E-{i:03d}", "S1", start.isoformat(), end.isoformat()
            )
        )

    results: list[dict] = []
    lock = threading.Lock()

    def _import_batch():
        session = TestSessionLocal()
        try:
            outcome = services.import_events(
                session, plan_version=pv, events=list(events)
            )
            with lock:
                results.append(outcome)
        finally:
            session.close()

    threads = [threading.Thread(target=_import_batch) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 每条事件全局只被接受一次，其余计入重复。
    assert sum(r["accepted"] for r in results) == 100
    assert sum(len(r["duplicates"]) for r in results) == 300
    for r in results:
        assert r["accepted"] + len(r["duplicates"]) == 100

    progress = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert progress["total_seconds"] == 100 * 3600


def test_concurrent_opposing_approves_exactly_one_wins(client):
    pv = _create_plan(client)
    _open_case(client, pv, "C-AB", survivor="SA", merged="SB")
    _add_evidence(client, pv, "C-AB")
    _open_case(client, pv, "C-BA", survivor="SB", merged="SA")
    _add_evidence(client, pv, "C-BA")

    outcomes: list[tuple[str, object]] = []
    lock = threading.Lock()

    def _approve_case(case_id):
        session = TestSessionLocal()
        try:
            result = services.approve_merge(
                session, pv, case_id, actor_id="approver"
            )
            with lock:
                outcomes.append((case_id, ("ok", result["approved_version"])))
        except services.MergeValidationError as exc:
            with lock:
                outcomes.append((case_id, ("rejected", str(exc))))
        finally:
            session.close()

    threads = [
        threading.Thread(target=_approve_case, args=("C-AB",)),
        threading.Thread(target=_approve_case, args=("C-BA",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 相反方向的合并并发批准：恰好一个成功，另一个因循环被拒绝。
    assert len(outcomes) == 2
    kinds = sorted(kind for _, (kind, _) in outcomes)
    assert kinds == ["ok", "rejected"]

    versions = client.get(f"/api/plans/{pv}/identity/versions").json()
    assert len(versions) == 1
    cases = {c["case_id"]: c["state"] for c in client.get(f"/api/plans/{pv}/identity/cases").json()}
    assert sorted(cases.values()) == ["APPROVED", "OPENED"]


def test_restart_recovers_cases_versions_and_aggregation(client):
    pv = _create_plan(client)
    _seed_two_students(client, pv)
    _open_case(client, pv, "C-1", survivor="S1", merged="S2")
    _add_evidence(client, pv, "C-1")
    _approve(client, pv, "C-1")

    # 模拟进程重启：全新引擎、会话与客户端指向同一数据库文件。
    engine2 = create_engine(
        "sqlite:///./practice_hours_test.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    session2 = sessionmaker(
        bind=engine2, autoflush=False, autocommit=False, future=True
    )()

    def override2():
        yield session2

    app.dependency_overrides[get_db] = override2
    try:
        with TestClient(app) as client2:
            case = client2.get(f"/api/plans/{pv}/identity/cases/C-1").json()
            assert case["state"] == "APPROVED"
            assert case["approved_version"] == 1
            assert case["evidence"][0]["reference"] == "DOC-1"

            versions = client2.get(f"/api/plans/{pv}/identity/versions").json()
            assert [(v["seq"], v["action"]) for v in versions] == [(1, "merge")]

            progress = client2.get(f"/api/plans/{pv}/students/S2/progress").json()
            assert progress["student_id"] == "S1"
            assert progress["total_seconds"] == 7200 + 3600 + 1800

            # 重启后撤销仍然可用，并生成连续的反向版本号。
            revoked = client2.post(
                f"/api/plans/{pv}/identity/cases/C-1/revoke",
                json={"actor_id": "approver", "reason": "误合并"},
            )
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["revoked_version"] == 2
            restored = client2.get(f"/api/plans/{pv}/students/S2/progress").json()
            assert restored["student_id"] == "S2"
    finally:
        app.dependency_overrides.clear()
        session2.close()
        engine2.dispose()
