"""服务端业务模块。"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import services
from app.db import get_db
from app.main import app
from app.models import Base
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

PLAN = dict(SHANGHAI_PLAN)
PV = PLAN["plan_version"]


def _create_plan(client):
    resp = client.post("/api/plans", json=PLAN)
    assert resp.status_code == 201, resp.text


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


def _import(client, events):
    resp = client.post(f"/api/plans/{PV}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _open_case(client, case_id, survivor, merged):
    return client.post(
        f"/api/plans/{PV}/identity-merges",
        json={
            "case_id": case_id,
            "survivor_student_id": survivor,
            "merged_student_id": merged,
            "actor_id": "ops-1",
            "reason": "duplicate enrollment identity",
        },
    )


def _add_evidence(client, case_id, evidence_id="EV-1"):
    return client.post(
        f"/api/plans/{PV}/identity-merges/{case_id}/evidence",
        json={
            "evidence_id": evidence_id,
            "kind": "enrollment_record",
            "reference": "registrar://tickets/42",
            "actor_id": "ops-1",
        },
    )


def _approve(client, case_id):
    return client.post(
        f"/api/plans/{PV}/identity-merges/{case_id}/approve",
        json={"actor_id": "registrar-1", "reason": "ownership verified"},
    )


def _revoke(client, case_id):
    return client.post(
        f"/api/plans/{PV}/identity-merges/{case_id}/revoke",
        json={"actor_id": "registrar-1", "reason": "mis-merge reported"},
    )


def _progress(client, student, alias_version=None):
    params = {} if alias_version is None else {"alias_version": alias_version}
    return client.get(
        f"/api/plans/{PV}/students/{student}/progress", params=params
    )


def _snapshot(client, alias_version=None):
    params = {} if alias_version is None else {"alias_version": alias_version}
    return client.get(f"/api/plans/{PV}/snapshot", params=params)


def test_merge_lifecycle_aggregates_then_revoke_restores(client):
    _create_plan(client)
    _import(
        client,
        [
            _checkin("E-01", "S-OLD", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
            _checkin("E-02", "S-NEW", "2024-03-15T10:00:00+08:00", "2024-03-15T11:00:00+08:00"),
            {
                "event_id": "E-03",
                "event_type": "leave_correction",
                "student_id": "S-OLD",
                "payload": {"adjustment_seconds": 600, "reason": "make-up"},
            },
        ],
    )

    # 开立案件
    resp = _open_case(client, "MC-01", "S-NEW", "S-OLD")
    assert resp.status_code == 201, resp.text
    case = resp.json()
    assert case["state"] == "opened"
    assert case["version"] == 1
    assert case["audit"][0]["action"] == "open"

    # 未验证归属证据不能批准
    assert _approve(client, "MC-01").status_code == 409

    # 补充证据
    resp = _add_evidence(client, "MC-01")
    assert resp.status_code == 201, resp.text
    assert resp.json()["evidence"][0]["evidence_id"] == "EV-1"

    # 影响预览：分别展示合并前与合并后的聚合结果
    impact = client.get("/api/plans/{pv}/identity-merges/MC-01/impact".format(pv=PV))
    assert impact.status_code == 200, impact.text
    preview = impact.json()
    assert preview["approvable"] is True
    assert preview["blockers"] == []
    assert preview["before"]["survivor"]["total_seconds"] == 3600
    assert preview["before"]["merged"]["total_seconds"] == 4200
    assert preview["after"]["canonical_student_id"] == "S-NEW"
    assert preview["after"]["student"]["total_seconds"] == 7800

    # 批准：生成别名版本 1
    resp = _approve(client, "MC-01")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["case"]["state"] == "approved"
    assert body["alias_version"] == 1
    assert body["revision"]["action"] == "merge"
    assert body["revision"]["alias_student_id"] == "S-OLD"
    assert body["revision"]["canonical_student_id"] == "S-NEW"

    # 重放聚合到根身份，但保留原始主体
    merged_view = _progress(client, "S-NEW").json()
    assert merged_view["student_id"] == "S-NEW"
    assert merged_view["source_student_ids"] == ["S-NEW", "S-OLD"]
    assert merged_view["total_seconds"] == 7800
    assert {c["student_id"] for c in merged_view["checkins"]} == {"S-OLD", "S-NEW"}
    assert merged_view["adjustments"][0]["student_id"] == "S-OLD"

    # 查询别名也会被解析到同一聚合组
    alias_view = _progress(client, "S-OLD").json()
    assert alias_view["student_id"] == "S-NEW"
    assert alias_view["total_seconds"] == 7800

    # 指定版本重放：版本 0 下两者仍然独立
    assert _progress(client, "S-OLD", alias_version=0).json()["total_seconds"] == 4200
    assert _progress(client, "S-NEW", alias_version=0).json()["total_seconds"] == 3600
    snap_v1 = _snapshot(client, alias_version=1).json()
    assert snap_v1["alias_version"] == 1
    assert [s["student_id"] for s in snap_v1["students"]] == ["S-NEW"]

    # 发现误合并：撤销生成反向版本，历史版本不被改写
    resp = _revoke(client, "MC-01")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["case"]["state"] == "revoked"
    assert body["alias_version"] == 2
    assert body["revision"]["action"] == "unmerge"

    assert _progress(client, "S-OLD").json()["total_seconds"] == 4200
    assert _progress(client, "S-NEW").json()["total_seconds"] == 3600
    # 版本 1 的聚合视图仍可重放
    assert _progress(client, "S-OLD", alias_version=1).json()["total_seconds"] == 7800

    registry = client.get(f"/api/plans/{PV}/identity-aliases").json()
    assert registry["current_version"] == 2
    assert registry["aliases"] == []
    assert [r["action"] for r in registry["revisions"]] == ["merge", "unmerge"]


def test_mentor_confirm_matches_across_merged_identities(client):
    _create_plan(client)
    _import(
        client,
        [
            _checkin(
                "E-01",
                "S-OLD",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T12:00:00+08:00",
                activity_type="internship",
            )
        ],
    )
    _open_case(client, "MC-01", "S-NEW", "S-OLD")
    _add_evidence(client, "MC-01")
    assert _approve(client, "MC-01").status_code == 200

    # 导师在新账号下确认旧账号的实习签到
    _import(
        client,
        [
            {
                "event_id": "E-02",
                "event_type": "mentor_confirm",
                "student_id": "S-NEW",
                "payload": {"checkin_event_id": "E-01"},
            }
        ],
    )
    merged = _progress(client, "S-NEW").json()
    assert merged["confirmed_seconds"] == 4 * 3600
    assert merged["pending_seconds"] == 0

    # 在合并前的版本下，确认不属于同一主体，实习仍处于待确认
    before = _progress(client, "S-OLD", alias_version=0).json()
    assert before["pending_seconds"] == 4 * 3600
    assert before["confirmed_seconds"] == 0


def test_merge_chain_resolves_to_root(client):
    _create_plan(client)
    _import(
        client,
        [
            _checkin("E-01", "SX", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
            _checkin("E-02", "SY", "2024-03-15T10:00:00+08:00", "2024-03-15T11:00:00+08:00"),
            _checkin("E-03", "SZ", "2024-03-15T13:00:00+08:00", "2024-03-15T14:00:00+08:00"),
        ],
    )
    # 第一条合并：SX -> SY
    _open_case(client, "MC-1", "SY", "SX")
    _add_evidence(client, "MC-1")
    assert _approve(client, "MC-1").status_code == 200
    # 第二条合并：SY -> SZ，形成 SX -> SY -> SZ 合并链
    _open_case(client, "MC-2", "SZ", "SY")
    _add_evidence(client, "MC-2")
    resp = _approve(client, "MC-2")
    assert resp.status_code == 200, resp.text
    assert resp.json()["alias_version"] == 2

    root = _progress(client, "SZ").json()
    assert root["total_seconds"] == 3 * 3600
    assert root["source_student_ids"] == ["SX", "SY", "SZ"]

    # 链上任意标识都解析到根身份
    for alias in ("SX", "SY"):
        view = _progress(client, alias).json()
        assert view["student_id"] == "SZ"
        assert view["total_seconds"] == 3 * 3600

    # 指定版本重放：版本 1 只有第一环，版本 0 全部独立
    v1 = _snapshot(client, alias_version=1).json()
    totals_v1 = {s["student_id"]: s["total_seconds"] for s in v1["students"]}
    assert totals_v1 == {"SY": 2 * 3600, "SZ": 3600}
    v0 = _snapshot(client, alias_version=0).json()
    totals_v0 = {s["student_id"]: s["total_seconds"] for s in v0["students"]}
    assert totals_v0 == {"SX": 3600, "SY": 3600, "SZ": 3600}

    registry = client.get(f"/api/plans/{PV}/identity-aliases").json()
    aliases = {a["alias_student_id"]: a for a in registry["aliases"]}
    assert aliases["SX"]["canonical_student_id"] == "SZ"
    assert aliases["SX"]["linked_student_id"] == "SY"
    assert aliases["SY"]["canonical_student_id"] == "SZ"


def test_direct_cycle_is_rejected(client):
    _create_plan(client)
    _open_case(client, "MC-1", "SY", "SX")
    _add_evidence(client, "MC-1")
    assert _approve(client, "MC-1").status_code == 200

    # 反向合并 SY -> SX 会形成循环
    assert _open_case(client, "MC-2", "SX", "SY").status_code == 201
    assert _add_evidence(client, "MC-2").status_code == 201

    impact = client.get(f"/api/plans/{PV}/identity-merges/MC-2/impact").json()
    assert impact["approvable"] is False
    assert "cycle_detected" in impact["blockers"]

    resp = _approve(client, "MC-2")
    assert resp.status_code == 409
    assert "循环" in resp.json()["detail"]


def test_transitive_cycle_is_rejected(client):
    _create_plan(client)
    for case_id, survivor, merged in (("MC-1", "SY", "SX"), ("MC-2", "SZ", "SY")):
        _open_case(client, case_id, survivor, merged)
        _add_evidence(client, case_id)
        assert _approve(client, case_id).status_code == 200

    # SZ -> SX 会闭合 SX -> SY -> SZ 的链
    _open_case(client, "MC-3", "SX", "SZ")
    _add_evidence(client, "MC-3")
    resp = _approve(client, "MC-3")
    assert resp.status_code == 409
    assert "循环" in resp.json()["detail"]


def test_self_merge_is_rejected(client):
    _create_plan(client)
    resp = _open_case(client, "MC-1", "S1", "S1")
    assert resp.status_code == 409


def test_realias_requires_revoke_first(client):
    _create_plan(client)
    _open_case(client, "MC-1", "SY", "SX")
    _add_evidence(client, "MC-1")
    assert _approve(client, "MC-1").status_code == 200

    # SX 已是生效别名，不能直接改挂到 SZ
    _open_case(client, "MC-2", "SZ", "SX")
    _add_evidence(client, "MC-2")
    resp = _approve(client, "MC-2")
    assert resp.status_code == 409
    assert "需先撤销" in resp.json()["detail"]

    # 撤销后可以重新建立映射
    assert _revoke(client, "MC-1").status_code == 200
    resp = _approve(client, "MC-2")
    assert resp.status_code == 200, resp.text
    registry = client.get(f"/api/plans/{PV}/identity-aliases").json()
    assert registry["aliases"] == [
        {"alias_student_id": "SX", "canonical_student_id": "SZ", "linked_student_id": "SZ"}
    ]


def test_approve_and_revoke_are_idempotent(client):
    _create_plan(client)
    _open_case(client, "MC-1", "SY", "SX")
    _add_evidence(client, "MC-1")
    first = _approve(client, "MC-1").json()
    second = _approve(client, "MC-1").json()
    assert first["alias_version"] == second["alias_version"] == 1

    third = _revoke(client, "MC-1").json()
    fourth = _revoke(client, "MC-1").json()
    assert third["alias_version"] == fourth["alias_version"] == 2

    registry = client.get(f"/api/plans/{PV}/identity-aliases").json()
    assert len(registry["revisions"]) == 2


def test_freezes_are_not_rewritten_by_merge_or_revoke(client):
    _create_plan(client)
    _import(
        client,
        [
            _checkin("E-01", "SX", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
            _checkin("E-02", "SY", "2024-03-15T10:00:00+08:00", "2024-03-15T11:00:00+08:00"),
        ],
    )
    f1 = client.post(f"/api/plans/{PV}/freezes/F-01", json={}).json()
    assert f1["alias_version"] == 0
    assert {s["student_id"] for s in f1["students"]} == {"SX", "SY"}

    _open_case(client, "MC-1", "SY", "SX")
    _add_evidence(client, "MC-1")
    assert _approve(client, "MC-1").status_code == 200

    f2 = client.post(f"/api/plans/{PV}/freezes/F-02", json={}).json()
    assert f2["alias_version"] == 1
    assert [s["student_id"] for s in f2["students"]] == ["SY"]
    assert f2["students"][0]["total_seconds"] == 7200
    assert f2["students"][0]["source_student_ids"] == ["SX", "SY"]

    # 误合并撤销后，已签发的冻结不被改写
    assert _revoke(client, "MC-1").status_code == 200

    f1_again = client.get(f"/api/plans/{PV}/freezes/F-01").json()
    assert f1_again["alias_version"] == 0
    assert {s["student_id"] for s in f1_again["students"]} == {"SX", "SY"}

    f2_again = client.get(f"/api/plans/{PV}/freezes/F-02").json()
    assert f2_again["alias_version"] == 1
    assert [s["student_id"] for s in f2_again["students"]] == ["SY"]
    assert f2_again["students"][0]["total_seconds"] == 7200

    # 当前实时视图已拆分，但指定冻结时的版本可重放出同一聚合
    live = _snapshot(client).json()
    assert live["alias_version"] == 2
    assert {s["student_id"] for s in live["students"]} == {"SX", "SY"}
    replayed = _snapshot(client, alias_version=1).json()
    assert [s["student_id"] for s in replayed["students"]] == ["SY"]
    assert replayed["students"][0]["total_seconds"] == 7200

    diff = client.get(f"/api/plans/{PV}/freezes/F-01/diff/F-02").json()
    assert diff["students_affected"] == 2  # SX 消失、SY 增加


def test_alias_version_out_of_range_rejected(client):
    _create_plan(client)
    assert _snapshot(client, alias_version=0).status_code == 200
    assert _snapshot(client, alias_version=1).status_code == 404
    assert _snapshot(client, alias_version=-1).status_code == 404

    _open_case(client, "MC-1", "SY", "SX")
    _add_evidence(client, "MC-1")
    assert _approve(client, "MC-1").status_code == 200

    assert _snapshot(client, alias_version=1).status_code == 200
    assert _snapshot(client, alias_version=2).status_code == 404
    assert _progress(client, "SX", alias_version=9).status_code == 404


def test_duplicate_case_and_evidence_rejected(client):
    _create_plan(client)
    assert _open_case(client, "MC-1", "SY", "SX").status_code == 201
    assert _open_case(client, "MC-1", "SY", "SX").status_code == 409
    assert _add_evidence(client, "MC-1", "EV-1").status_code == 201
    assert _add_evidence(client, "MC-1", "EV-1").status_code == 409


def test_missing_case_returns_404(client):
    _create_plan(client)
    assert client.get(f"/api/plans/{PV}/identity-merges/NOPE").status_code == 404
    assert _add_evidence(client, "NOPE").status_code == 404
    assert client.get(f"/api/plans/{PV}/identity-merges/NOPE/impact").status_code == 404
    assert _approve(client, "NOPE").status_code == 404
    assert _revoke(client, "NOPE").status_code == 404


def test_merge_case_requires_existing_plan(client):
    resp = client.post(
        "/api/plans/NOPE/identity-merges",
        json={
            "case_id": "MC-1",
            "survivor_student_id": "SY",
            "merged_student_id": "SX",
            "actor_id": "ops-1",
        },
    )
    assert resp.status_code == 404


def test_revoke_before_approve_rejected(client):
    _create_plan(client)
    _open_case(client, "MC-1", "SY", "SX")
    _add_evidence(client, "MC-1")
    assert _revoke(client, "MC-1").status_code == 409


def test_impact_preview_reports_missing_evidence(client):
    _create_plan(client)
    _open_case(client, "MC-1", "SY", "SX")
    impact = client.get(f"/api/plans/{PV}/identity-merges/MC-1/impact").json()
    assert impact["approvable"] is False
    assert "evidence_missing" in impact["blockers"]
    assert impact["evidence_count"] == 0


def test_concurrent_event_import_during_merge(client):
    """并发导入事件与批准合并互不干扰，重放结果确定。"""
    from datetime import datetime, timedelta, timezone

    _create_plan(client)
    _open_case(client, "MC-1", "SY", "SX")
    _add_evidence(client, "MC-1")

    # 40 个互不重叠的 30 分钟签到，SX/SY 各半
    base = datetime(2024, 3, 15, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    events = []
    for i in range(40):
        start = base + timedelta(minutes=30 * i)
        end = start + timedelta(minutes=30)
        student = "SX" if i % 2 == 0 else "SY"
        events.append(
            _checkin(f"E-{i:03d}", student, start.isoformat(), end.isoformat())
        )
    # 8 个批次，相邻批次重叠一半，验证并发幂等
    batches = [events[i * 5 : i * 5 + 10] for i in range(8)]

    accepted_total = 0
    duplicate_total = 0
    lock = threading.Lock()

    def _import_batch(batch):
        nonlocal accepted_total, duplicate_total
        session = TestSessionLocal()
        try:
            result = services.import_events(session, plan_version=PV, events=batch)
            with lock:
                accepted_total += result["accepted"]
                duplicate_total += len(result["duplicates"])
        finally:
            session.close()

    threads = [threading.Thread(target=_import_batch, args=(b,)) for b in batches]
    for t in threads:
        t.start()

    # 导入进行的同时批准合并
    approve_session = TestSessionLocal()
    try:
        outcome = services.approve_merge_case(
            approve_session,
            plan_version=PV,
            case_id="MC-1",
            actor_id="registrar-1",
            reason="ownership verified",
        )
        assert outcome["alias_version"] == 1
    finally:
        approve_session.close()

    for t in threads:
        t.join()

    # 每个事件恰好被接受一次，其余为重复
    assert accepted_total == 40
    assert accepted_total + duplicate_total == sum(len(b) for b in batches)

    merged = _progress(client, "SY").json()
    assert merged["total_seconds"] == 40 * 1800
    assert merged["source_student_ids"] == ["SX", "SY"]

    # 再次导入全部为重复，状态不变
    again = _import(client, events)
    assert again["accepted"] == 0
    assert len(again["duplicates"]) == 40
    assert _progress(client, "SY").json()["total_seconds"] == 40 * 1800


def test_concurrent_approvals_get_distinct_versions(client):
    """并发批准不同案件时，别名版本号分配互不冲突。"""
    _create_plan(client)
    for case_id, survivor, merged in (("MC-1", "SY", "SX"), ("MC-2", "SW", "SV")):
        _open_case(client, case_id, survivor, merged)
        _add_evidence(client, case_id)

    results: dict[str, int] = {}
    lock = threading.Lock()

    def _approve_case(case_id):
        session = TestSessionLocal()
        try:
            outcome = services.approve_merge_case(
                session,
                plan_version=PV,
                case_id=case_id,
                actor_id="registrar-1",
                reason="ownership verified",
            )
            with lock:
                results[case_id] = outcome["alias_version"]
        finally:
            session.close()

    threads = [
        threading.Thread(target=_approve_case, args=(c,)) for c in ("MC-1", "MC-2")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == [1, 2]
    registry = client.get(f"/api/plans/{PV}/identity-aliases").json()
    assert registry["current_version"] == 2
    assert len(registry["revisions"]) == 2


def test_restart_recovers_merge_state(tmp_path):
    """重启后（新引擎读取同一数据库文件）别名注册表与案件状态完整恢复。"""
    db_url = f"sqlite:///{tmp_path}/restart.db"

    def make_client():
        engine = create_engine(db_url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        session_local = sessionmaker(
            bind=engine, autoflush=False, autocommit=False, future=True
        )

        def override():
            session = session_local()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override
        return TestClient(app), engine

    client1, engine1 = make_client()
    try:
        with client1:
            _create_plan(client1)
            _import(
                client1,
                [
                    _checkin("E-01", "SX", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
                    _checkin("E-02", "SY", "2024-03-15T10:00:00+08:00", "2024-03-15T11:00:00+08:00"),
                ],
            )
            _open_case(client1, "MC-1", "SY", "SX")
            _add_evidence(client1, "MC-1")
            assert _approve(client1, "MC-1").status_code == 200
            assert _progress(client1, "SX").json()["total_seconds"] == 7200
    finally:
        app.dependency_overrides.clear()
        engine1.dispose()

    # 模拟重启：全新的引擎、会话工厂与客户端
    client2, engine2 = make_client()
    try:
        with client2:
            registry = client2.get(f"/api/plans/{PV}/identity-aliases").json()
            assert registry["current_version"] == 1
            assert registry["aliases"][0]["alias_student_id"] == "SX"
            assert registry["aliases"][0]["canonical_student_id"] == "SY"

            case = client2.get(f"/api/plans/{PV}/identity-merges/MC-1").json()
            assert case["state"] == "approved"
            assert len(case["evidence"]) == 1

            # 聚合视图在重启后仍然成立
            view = _progress(client2, "SX").json()
            assert view["student_id"] == "SY"
            assert view["total_seconds"] == 7200

            # 版本计数器从持久化的注册表恢复：新批准得到版本 2
            _open_case(client2, "MC-2", "SY", "SZ")
            _add_evidence(client2, "MC-2")
            resp = _approve(client2, "MC-2")
            assert resp.status_code == 200, resp.text
            assert resp.json()["alias_version"] == 2

            # 撤销误合并生成版本 3，SX 恢复独立
            resp = _revoke(client2, "MC-1")
            assert resp.status_code == 200, resp.text
            assert resp.json()["alias_version"] == 3
            assert _progress(client2, "SX").json()["student_id"] == "SX"
            # 历史版本仍可重放
            assert _progress(client2, "SX", alias_version=1).json()["student_id"] == "SY"
    finally:
        app.dependency_overrides.clear()
        engine2.dispose()
