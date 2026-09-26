"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.compliance import identity_merges as merges
from app.compliance.identity_merges import (
    AliasRevision,
    DomainError,
    RevisionAction,
    State,
)

NOW = datetime(2024, 9, 1, 8, 0, tzinfo=timezone.utc)


def _open(case_id="MC-01", survivor="S-CANON", merged="S-ALIAS", **kwargs):
    return merges.open_case(
        case_id=case_id,
        plan_version="P1",
        survivor_student_id=survivor,
        merged_student_id=merged,
        actor_id="ops-1",
        now=NOW,
        reason=kwargs.get("reason", "duplicate enrollment"),
    )


def _with_evidence(case):
    return merges.add_evidence(
        case,
        evidence_id="EV-1",
        kind="enrollment_record",
        reference="registrar://tickets/42",
        actor_id="ops-1",
        now=NOW + timedelta(minutes=1),
    )


def _revision(version, action, alias, canonical, case_id="MC-01"):
    return AliasRevision(
        version=version,
        case_id=case_id,
        action=action,
        alias_student_id=alias,
        canonical_student_id=canonical,
        actor_id="ops-1",
        reason="",
        created_at=NOW,
    )


def test_open_case_validates_identifiers():
    with pytest.raises(DomainError):
        merges.open_case(
            case_id=" ",
            plan_version="P1",
            survivor_student_id="A",
            merged_student_id="B",
            actor_id="ops",
            now=NOW,
        )
    with pytest.raises(DomainError):
        merges.open_case(
            case_id="MC",
            plan_version="P1",
            survivor_student_id="A",
            merged_student_id="B",
            actor_id="ops",
            now=NOW.replace(tzinfo=None),
        )


def test_self_merge_is_rejected_at_open():
    with pytest.raises(DomainError, match="同一学员"):
        _open(survivor="S1", merged="S1")


def test_open_case_initializes_state_and_audit():
    case = _open()
    assert case.state == State.OPENED
    assert case.version == 1
    assert len(case.audit) == 1
    assert case.audit[0].action == "open"
    assert merges.verify_audit(case)


def test_evidence_requires_open_state_and_unique_ids():
    case = _open()
    case = _with_evidence(case)
    assert len(case.evidence) == 1
    assert case.version == 2
    assert merges.verify_audit(case)

    with pytest.raises(DomainError, match="证据编号已存在"):
        _with_evidence(case)

    approved = merges.approve(case, "lead-1", NOW, "verified")
    with pytest.raises(DomainError, match="仅进行中的案件"):
        merges.add_evidence(
            approved,
            evidence_id="EV-2",
            kind="id_document",
            reference="doc://1",
            actor_id="ops-1",
            now=NOW,
        )


def test_approve_requires_evidence():
    case = _open()
    with pytest.raises(DomainError, match="缺少归属证据"):
        merges.approve(case, "lead-1", NOW, "verified")


def test_approve_and_revoke_transitions_are_audited():
    case = _with_evidence(_open())
    approved = merges.approve(case, "lead-1", NOW, "verified ownership")
    assert approved.state == State.APPROVED
    revoked = merges.revoke(approved, "lead-1", NOW, "mis-merge found")
    assert revoked.state == State.REVOKED
    assert merges.verify_audit(revoked)
    actions = [entry.action for entry in revoked.audit]
    assert actions == ["open", "evidence", "transition", "transition"]


def test_revoke_requires_approval_first():
    case = _with_evidence(_open())
    with pytest.raises(DomainError, match="不允许"):
        merges.revoke(case, "lead-1", NOW, "too early")


def test_transition_requires_actor_and_reason():
    case = _with_evidence(_open())
    with pytest.raises(DomainError):
        merges.approve(case, " ", NOW, "ok")
    with pytest.raises(DomainError):
        merges.approve(case, "lead-1", NOW, "  ")


def test_merge_and_reversal_revisions_mirror_the_case():
    case = _with_evidence(_open())
    approved = merges.approve(case, "lead-1", NOW, "ok")
    forward = merges.merge_revision(
        approved, version=1, actor_id="lead-1", now=NOW, reason="ok"
    )
    assert forward.action == RevisionAction.MERGE
    assert forward.alias_student_id == "S-ALIAS"
    assert forward.canonical_student_id == "S-CANON"

    revoked = merges.revoke(approved, "lead-1", NOW, "mis-merge")
    reversal = merges.reversal_revision(
        revoked, version=2, actor_id="lead-1", now=NOW, reason="mis-merge"
    )
    assert reversal.action == RevisionAction.UNMERGE
    assert reversal.alias_student_id == forward.alias_student_id
    assert reversal.canonical_student_id == forward.canonical_student_id

    with pytest.raises(DomainError):
        merges.reversal_revision(approved, version=3, actor_id="a", now=NOW, reason="x")
    with pytest.raises(DomainError):
        merges.merge_revision(revoked, version=3, actor_id="a", now=NOW, reason="x")


def test_build_alias_links_folds_revisions_and_versions():
    revisions = [
        _revision(1, RevisionAction.MERGE, "X", "Y"),
        _revision(2, RevisionAction.MERGE, "Y", "Z", case_id="MC-02"),
        _revision(3, RevisionAction.UNMERGE, "X", "Y"),
    ]
    links = merges.build_alias_links(revisions)
    assert links == {"Y": "Z"}  # X->Y 被版本 3 的反向版本移除

    at_v1 = merges.build_alias_links(revisions, up_to_version=1)
    assert at_v1 == {"X": "Y"}
    at_v2 = merges.build_alias_links(revisions, up_to_version=2)
    assert at_v2 == {"X": "Y", "Y": "Z"}


def test_resolve_root_follows_merge_chains():
    links = {"X": "Y", "Y": "Z", "U": "V"}
    assert merges.resolve_root(links, "X") == "Z"
    assert merges.resolve_root(links, "Y") == "Z"
    assert merges.resolve_root(links, "Z") == "Z"
    assert merges.resolve_root(links, "U") == "V"
    resolved = merges.resolve_links(links)
    assert resolved == {"X": "Z", "Y": "Z", "U": "V"}


def test_resolve_root_detects_corrupted_cycle():
    with pytest.raises(DomainError, match="循环"):
        merges.resolve_root({"A": "B", "B": "A"}, "A")


def test_would_cycle_detects_direct_transitive_and_self():
    links = {"X": "Y", "Y": "Z"}
    assert merges.would_cycle(links, "Z", "X") is True  # Z->X 形成 X->Y->Z->X
    assert merges.would_cycle(links, "Y", "X") is True  # 直接互指
    assert merges.would_cycle(links, "A", "A") is True  # 自合并
    assert merges.would_cycle(links, "W", "Z") is False  # 新链末端
    assert merges.would_cycle(links, "A", "Z") is False  # 挂到已有链上


def test_ensure_linkable_rejects_realias_and_cycle():
    links = {"X": "Y"}
    with pytest.raises(DomainError, match="需先撤销"):
        merges.ensure_linkable(links, "X", "Z")
    with pytest.raises(DomainError, match="循环"):
        merges.ensure_linkable(links, "Y", "X")
    merges.ensure_linkable(links, "W", "Y")  # 合法：W 挂到 Y 上


def test_summarize_counts_states_and_audit():
    opened = _open(case_id="MC-1")
    approved = merges.approve(
        _with_evidence(_open(case_id="MC-2")), "lead", NOW, "ok"
    )
    revoked = merges.revoke(approved, "lead", NOW, "undo")
    counts = merges.summarize([opened, approved, revoked])
    assert counts["opened"] == 1
    assert counts["approved"] == 0  # approved 已被撤销推进
    assert counts["revoked"] == 1
    assert counts["invalid_audit"] == 0
