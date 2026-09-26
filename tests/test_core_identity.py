"""身份合并核心逻辑：别名版本、链式解析、循环拒绝与重放聚合。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.identity import (
    Action,
    AliasEntry,
    IdentityError,
    effective_map,
    resolve,
    validate_merge,
)
from app.core.replay import Event, EventType, replay


def _entry(seq: int, action: Action, merged: str, survivor: str) -> AliasEntry:
    return AliasEntry(
        seq=seq,
        case_id=f"C-{seq}",
        action=action,
        survivor_id=survivor,
        merged_id=merged,
        actor_id="ops",
        reason="",
    )


def test_effective_map_replays_versions_in_order():
    entries = [
        _entry(1, Action.MERGE, "S2", "S1"),
        _entry(2, Action.MERGE, "S3", "S2"),
        _entry(3, Action.UNMERGE, "S2", "S1"),
    ]
    mapping = effective_map(entries)
    # S2->S1 被版本 3 撤销，S3->S2 仍然有效。
    assert mapping == {"S3": "S2"}


def test_effective_map_pinned_to_requested_version():
    entries = [
        _entry(1, Action.MERGE, "S2", "S1"),
        _entry(2, Action.MERGE, "S3", "S2"),
        _entry(3, Action.UNMERGE, "S2", "S1"),
    ]
    assert effective_map(entries, up_to_seq=0) == {}
    assert effective_map(entries, up_to_seq=1) == {"S2": "S1"}
    assert effective_map(entries, up_to_seq=2) == {"S2": "S1", "S3": "S2"}
    assert effective_map(entries, up_to_seq=3) == {"S3": "S2"}


def test_resolve_follows_merge_chain_to_final_survivor():
    mapping = {"S2": "S1", "S3": "S2"}
    assert resolve(mapping, "S3") == "S1"
    assert resolve(mapping, "S2") == "S1"
    assert resolve(mapping, "S1") == "S1"
    assert resolve(mapping, "UNKNOWN") == "UNKNOWN"


def test_resolve_terminates_on_corrupted_cycle():
    # 写入路径会拒绝循环；损坏数据下解析也必须确定性终止。
    mapping = {"A": "B", "B": "A"}
    assert resolve(mapping, "A") in {"A", "B"}
    assert resolve(mapping, "B") in {"A", "B"}


def test_validate_merge_rejects_self_merge():
    with pytest.raises(IdentityError):
        validate_merge({}, "S1", "S1")


def test_validate_merge_rejects_duplicate_active_edge():
    with pytest.raises(IdentityError):
        validate_merge({"S2": "S1"}, "S2", "S9")


def test_validate_merge_rejects_direct_cycle():
    with pytest.raises(IdentityError):
        validate_merge({"S2": "S1"}, "S1", "S2")


def test_validate_merge_rejects_indirect_cycle():
    mapping = {"S2": "S1", "S3": "S2"}
    with pytest.raises(IdentityError):
        validate_merge(mapping, "S1", "S3")


def test_validate_merge_allows_chain_extension():
    mapping = {"S2": "S1"}
    # S3 -> S2 延长合并链：S3 解析到 S1。
    validate_merge(mapping, "S3", "S2")
    mapping["S3"] = "S2"
    assert resolve(mapping, "S3") == "S1"


def _event(event_id, event_type, student_id, payload, plan_version="P1"):
    return Event(
        event_id=event_id,
        plan_version=plan_version,
        event_type=event_type,
        student_id=student_id,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )


def _checkin(eid, student, start, end, activity_type="regular"):
    return _event(
        eid,
        EventType.CHECKIN,
        student,
        {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    )


def test_replay_aggregates_alias_hours_under_survivor():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S2", "2024-03-15T10:00:00+08:00", "2024-03-15T12:00:00+08:00"),
    ]
    state = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        alias_map={"S2": "S1"},
        identity_version=1,
    )
    assert set(state.students) == {"S1"}
    progress = state.students["S1"]
    assert progress.total_seconds == 4 * 3600
    assert progress.source_student_ids == ["S1", "S2"]
    assert state.identity_version == 1
    # 原始主体保留在每条签到记录上。
    originals = {c.event_id: c.original_student_id for c in progress.checkins}
    assert originals == {"E-01": None, "E-02": "S2"}


def test_replay_without_aliases_keeps_legacy_behavior():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S2", "2024-03-15T10:00:00+08:00", "2024-03-15T12:00:00+08:00"),
    ]
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    )
    assert set(state.students) == {"S1", "S2"}
    assert state.students["S1"].source_student_ids == ["S1"]
    assert state.identity_version is None


def test_mentor_confirm_matches_across_merged_identities():
    events = [
        _checkin(
            "E-01",
            "S2",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T12:00:00+08:00",
            activity_type="internship",
        ),
        # 合并后导师用主学号确认 S2 名下的实习签到。
        _event("E-02", EventType.MENTOR_CONFIRM, "S1", {"checkin_event_id": "E-01"}),
    ]
    state = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        alias_map={"S2": "S1"},
    )
    progress = state.students["S1"]
    assert progress.confirmed_seconds == 4 * 3600
    assert progress.pending_seconds == 0


def test_mentor_confirm_from_unrelated_identity_still_ignored():
    events = [
        _checkin(
            "E-01",
            "S2",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T12:00:00+08:00",
            activity_type="internship",
        ),
        _event("E-02", EventType.MENTOR_CONFIRM, "S9", {"checkin_event_id": "E-01"}),
    ]
    state = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        alias_map={"S2": "S1"},
    )
    assert state.students["S1"].pending_seconds == 4 * 3600


def test_unmerge_version_restores_original_aggregation():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S2", "2024-03-15T10:00:00+08:00", "2024-03-15T12:00:00+08:00"),
    ]
    entries = [
        _entry(1, Action.MERGE, "S2", "S1"),
        _entry(2, Action.UNMERGE, "S2", "S1"),
    ]
    merged = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        alias_map=effective_map(entries, up_to_seq=1),
        identity_version=1,
    )
    assert set(merged.students) == {"S1"}

    revoked = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        alias_map=effective_map(entries, up_to_seq=2),
        identity_version=2,
    )
    assert set(revoked.students) == {"S1", "S2"}
    assert revoked.students["S1"].total_seconds == 7200
    assert revoked.students["S2"].total_seconds == 7200
