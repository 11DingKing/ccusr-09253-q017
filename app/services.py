"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .compliance import identity_merges
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .repository import (
    get_alias_revision_for_case,
    get_freeze,
    get_merge_case_row,
    get_plan,
    insert_alias_revision,
    insert_events,
    insert_freeze,
    insert_merge_case,
    insert_merge_evidence,
    list_alias_revisions,
    list_merge_case_rows,
    list_merge_evidence,
    load_events,
    max_event_id,
    update_merge_case_row,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class MergeCaseNotFoundError(Exception):
    pass


class MergeConflictError(Exception):
    """身份合并的业务规则冲突（状态、证据、循环、并发）。"""


class AliasVersionNotFoundError(Exception):
    pass


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


# --- 别名注册表 -----------------------------------------------------------


def _revision_row_to_domain(row) -> identity_merges.AliasRevision:
    return identity_merges.AliasRevision(
        version=row.version,
        case_id=row.case_id,
        action=identity_merges.RevisionAction(row.action),
        alias_student_id=row.alias_student_id,
        canonical_student_id=row.canonical_student_id,
        actor_id=row.actor_id,
        reason=row.reason,
        created_at=_as_utc(row.created_at),
    )


def _domain_revisions(
    db: Session, plan_version: str
) -> list[identity_merges.AliasRevision]:
    return [
        _revision_row_to_domain(row)
        for row in list_alias_revisions(db, plan_version)
    ]


def _resolved_alias_map(
    db: Session, plan_version: str, alias_version: int | None
) -> tuple[dict[str, str], int]:
    """解析指定版本下“别名 -> 根身份”的映射；默认使用最新版本。"""
    revisions = _domain_revisions(db, plan_version)
    current = max((r.version for r in revisions), default=0)
    if alias_version is None:
        alias_version = current
    if alias_version < 0 or alias_version > current:
        raise AliasVersionNotFoundError(
            f"alias version {alias_version} does not exist for plan "
            f"'{plan_version}' (current: {current})"
        )
    links = identity_merges.build_alias_links(revisions, up_to_version=alias_version)
    return identity_merges.resolve_links(links), alias_version


def current_snapshot(
    db: Session, plan_version: str, alias_version: int | None = None
) -> Snapshot:
    plan = _require_plan(db, plan_version)
    aliases, version = _resolved_alias_map(db, plan_version, alias_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        aliases=aliases,
        alias_version=version,
    )


def student_progress(
    db: Session,
    plan_version: str,
    student_id: str,
    alias_version: int | None = None,
) -> dict[str, Any] | None:
    aliases, _ = _resolved_alias_map(db, plan_version, alias_version)
    snap = current_snapshot(db, plan_version, alias_version)
    canonical = aliases.get(student_id, student_id)
    return explain_student(snap, canonical)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    aliases, alias_version = _resolved_alias_map(db, plan_version, None)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
        aliases=aliases,
        alias_version=alias_version,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


# --- 身份合并案件 ---------------------------------------------------------


def _audit_entry_to_domain(entry: dict[str, Any]) -> identity_merges.AuditEntry:
    return identity_merges.AuditEntry(
        sequence=int(entry["sequence"]),
        action=str(entry["action"]),
        actor_id=str(entry["actor_id"]),
        occurred_at=_as_utc(datetime.fromisoformat(entry["occurred_at"])),
        before=str(entry["before"]),
        after=str(entry["after"]),
        reason=str(entry["reason"]),
        fingerprint=str(entry["fingerprint"]),
    )


def _audit_entry_to_dict(entry: identity_merges.AuditEntry) -> dict[str, Any]:
    return {
        "sequence": entry.sequence,
        "action": entry.action,
        "actor_id": entry.actor_id,
        "occurred_at": _iso_z(entry.occurred_at),
        "before": entry.before,
        "after": entry.after,
        "reason": entry.reason,
        "fingerprint": entry.fingerprint,
    }


def _case_row_to_domain(row, evidence_rows) -> identity_merges.MergeCase:
    return identity_merges.MergeCase(
        case_id=row.case_id,
        plan_version=row.plan_version,
        survivor_student_id=row.survivor_student_id,
        merged_student_id=row.merged_student_id,
        state=identity_merges.State(row.state),
        reason=row.reason,
        created_by=row.created_by,
        version=row.version,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        evidence=tuple(
            identity_merges.Evidence(
                evidence_id=e.evidence_id,
                kind=e.kind,
                reference=e.reference,
                submitted_by=e.submitted_by,
                submitted_at=_as_utc(e.created_at),
            )
            for e in evidence_rows
        ),
        audit=tuple(_audit_entry_to_domain(a) for a in (row.audit or [])),
    )


def _case_to_dict(case: identity_merges.MergeCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "plan_version": case.plan_version,
        "survivor_student_id": case.survivor_student_id,
        "merged_student_id": case.merged_student_id,
        "state": case.state.value,
        "reason": case.reason,
        "created_by": case.created_by,
        "version": case.version,
        "created_at": _iso_z(case.created_at),
        "updated_at": _iso_z(case.updated_at),
        "evidence": [
            {
                "evidence_id": e.evidence_id,
                "kind": e.kind,
                "reference": e.reference,
                "submitted_by": e.submitted_by,
                "submitted_at": _iso_z(e.submitted_at),
            }
            for e in case.evidence
        ],
        "audit": [_audit_entry_to_dict(a) for a in case.audit],
    }


def _revision_to_dict(revision: identity_merges.AliasRevision) -> dict[str, Any]:
    return {
        "version": revision.version,
        "case_id": revision.case_id,
        "action": revision.action.value,
        "alias_student_id": revision.alias_student_id,
        "canonical_student_id": revision.canonical_student_id,
        "actor_id": revision.actor_id,
        "reason": revision.reason,
        "created_at": _iso_z(revision.created_at),
    }


def _require_case_row(db: Session, plan_version: str, case_id: str):
    row = get_merge_case_row(db, plan_version, case_id)
    if row is None:
        raise MergeCaseNotFoundError(
            f"merge case '{case_id}' for plan '{plan_version}' does not exist"
        )
    return row


def _require_merge_case(
    db: Session, plan_version: str, case_id: str
) -> identity_merges.MergeCase:
    row = _require_case_row(db, plan_version, case_id)
    return _case_row_to_domain(row, list_merge_evidence(db, plan_version, case_id))


def open_merge_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    survivor_student_id: str,
    merged_student_id: str,
    actor_id: str,
    reason: str = "",
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    now = datetime.now(timezone.utc)
    try:
        case = identity_merges.open_case(
            case_id=case_id,
            plan_version=plan_version,
            survivor_student_id=survivor_student_id,
            merged_student_id=merged_student_id,
            actor_id=actor_id,
            now=now,
            reason=reason,
        )
    except identity_merges.DomainError as exc:
        raise MergeConflictError(str(exc)) from exc
    inserted = insert_merge_case(
        db,
        plan_version=plan_version,
        case_id=case.case_id,
        survivor_student_id=case.survivor_student_id,
        merged_student_id=case.merged_student_id,
        state=case.state.value,
        reason=case.reason,
        created_by=case.created_by,
        version=case.version,
        audit=[_audit_entry_to_dict(a) for a in case.audit],
        created_at=case.created_at,
        updated_at=case.updated_at,
    )
    if not inserted:
        db.rollback()
        raise MergeConflictError(
            f"merge case '{case_id}' for plan '{plan_version}' already exists"
        )
    db.commit()
    return _case_to_dict(case)


def get_merge_case(
    db: Session, plan_version: str, case_id: str
) -> dict[str, Any] | None:
    _require_plan(db, plan_version)
    row = get_merge_case_row(db, plan_version, case_id)
    if row is None:
        return None
    case = _case_row_to_domain(row, list_merge_evidence(db, plan_version, case_id))
    return _case_to_dict(case)


def list_merge_cases(db: Session, plan_version: str) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    result = []
    for row in list_merge_case_rows(db, plan_version):
        evidence = list_merge_evidence(db, plan_version, row.case_id)
        result.append(_case_to_dict(_case_row_to_domain(row, evidence)))
    return result


def add_merge_evidence(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    evidence_id: str,
    kind: str,
    reference: str,
    actor_id: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    case = _require_merge_case(db, plan_version, case_id)
    now = datetime.now(timezone.utc)
    try:
        updated = identity_merges.add_evidence(
            case,
            evidence_id=evidence_id,
            kind=kind,
            reference=reference,
            actor_id=actor_id,
            now=now,
        )
    except identity_merges.DomainError as exc:
        raise MergeConflictError(str(exc)) from exc
    item = updated.evidence[-1]
    inserted = insert_merge_evidence(
        db,
        plan_version=plan_version,
        case_id=case_id,
        evidence_id=item.evidence_id,
        kind=item.kind,
        reference=item.reference,
        submitted_by=item.submitted_by,
        created_at=item.submitted_at,
    )
    if not inserted:
        db.rollback()
        raise MergeConflictError("证据编号已存在")
    ok = update_merge_case_row(
        db,
        plan_version=plan_version,
        case_id=case_id,
        expected_version=case.version,
        state=updated.state.value,
        version=updated.version,
        audit=[_audit_entry_to_dict(a) for a in updated.audit],
        updated_at=now,
    )
    if not ok:
        db.rollback()
        raise MergeConflictError("案件已被并发修改，请重试")
    db.commit()
    return _case_to_dict(updated)


def preview_merge_impact(
    db: Session, plan_version: str, case_id: str
) -> dict[str, Any]:
    """影响预览：按当前事件与别名注册表，模拟批准后的聚合结果。"""
    plan = _require_plan(db, plan_version)
    case = _require_merge_case(db, plan_version, case_id)
    revisions = _domain_revisions(db, plan_version)
    current_version = max((r.version for r in revisions), default=0)
    links = identity_merges.build_alias_links(revisions)

    blockers: list[str] = []
    if case.state != identity_merges.State.OPENED:
        blockers.append("case_not_open")
    if not case.evidence:
        blockers.append("evidence_missing")
    if case.merged_student_id in links:
        blockers.append("alias_already_mapped")
    cycle = identity_merges.would_cycle(
        links, case.merged_student_id, case.survivor_student_id
    )
    if cycle:
        blockers.append("cycle_detected")

    events = load_events(db, plan_version)
    resolved_now = identity_merges.resolve_links(links)
    snap_now = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        aliases=resolved_now,
        alias_version=current_version,
    )
    before_survivor = explain_student(
        snap_now, identity_merges.resolve_root(links, case.survivor_student_id)
    )
    before_merged = explain_student(
        snap_now, identity_merges.resolve_root(links, case.merged_student_id)
    )

    root_after = case.survivor_student_id
    after_student = None
    if not cycle:
        hypothetical = dict(links)
        hypothetical[case.merged_student_id] = case.survivor_student_id
        root_after = identity_merges.resolve_root(
            hypothetical, case.survivor_student_id
        )
        snap_after = build_snapshot(
            events,
            plan_version=plan_version,
            timezone_name=plan.iana_timezone,
            required_seconds=plan.required_seconds,
            aliases=identity_merges.resolve_links(hypothetical),
            alias_version=current_version + 1,
        )
        after_student = explain_student(snap_after, root_after)

    return {
        "case_id": case.case_id,
        "plan_version": plan_version,
        "state": case.state.value,
        "survivor_student_id": case.survivor_student_id,
        "merged_student_id": case.merged_student_id,
        "current_alias_version": current_version,
        "evidence_count": len(case.evidence),
        "approvable": not blockers,
        "blockers": blockers,
        "before": {"survivor": before_survivor, "merged": before_merged},
        "after": {"canonical_student_id": root_after, "student": after_student},
    }


def _approve_merge_once(
    db: Session, *, plan_version: str, case_id: str, actor_id: str, reason: str
) -> dict[str, Any]:
    row = _require_case_row(db, plan_version, case_id)
    if row.state == identity_merges.State.APPROVED.value:
        # 幂等：重复批准返回首次生成的版本，不追加新记录。
        revision_row = get_alias_revision_for_case(
            db, plan_version, case_id, identity_merges.RevisionAction.MERGE.value
        )
        assert revision_row is not None
        case = _case_row_to_domain(
            row, list_merge_evidence(db, plan_version, case_id)
        )
        return {
            "case": _case_to_dict(case),
            "alias_version": revision_row.version,
            "revision": _revision_to_dict(_revision_row_to_domain(revision_row)),
        }

    case = _case_row_to_domain(row, list_merge_evidence(db, plan_version, case_id))
    now = datetime.now(timezone.utc)
    try:
        approved = identity_merges.approve(case, actor_id, now, reason)
        revisions = _domain_revisions(db, plan_version)
        links = identity_merges.build_alias_links(revisions)
        identity_merges.ensure_linkable(
            links, case.merged_student_id, case.survivor_student_id
        )
        version = max((r.version for r in revisions), default=0) + 1
        revision = identity_merges.merge_revision(
            approved, version=version, actor_id=actor_id, now=now, reason=reason
        )
    except identity_merges.DomainError as exc:
        db.rollback()
        raise MergeConflictError(str(exc)) from exc

    insert_alias_revision(
        db,
        plan_version=plan_version,
        version=revision.version,
        case_id=revision.case_id,
        action=revision.action.value,
        alias_student_id=revision.alias_student_id,
        canonical_student_id=revision.canonical_student_id,
        actor_id=revision.actor_id,
        reason=revision.reason,
        created_at=revision.created_at,
    )
    ok = update_merge_case_row(
        db,
        plan_version=plan_version,
        case_id=case_id,
        expected_version=case.version,
        state=approved.state.value,
        version=approved.version,
        audit=[_audit_entry_to_dict(a) for a in approved.audit],
        updated_at=now,
    )
    if not ok:
        db.rollback()
        raise MergeConflictError("案件已被并发修改，请重试")
    db.commit()
    return {
        "case": _case_to_dict(approved),
        "alias_version": revision.version,
        "revision": _revision_to_dict(revision),
    }


def approve_merge_case(
    db: Session, *, plan_version: str, case_id: str, actor_id: str, reason: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    for _attempt in range(3):
        try:
            return _approve_merge_once(
                db,
                plan_version=plan_version,
                case_id=case_id,
                actor_id=actor_id,
                reason=reason,
            )
        except IntegrityError:
            # 并发批准抢占了同一别名版本号，回滚后重试。
            db.rollback()
    raise MergeConflictError("并发写入冲突，请稍后重试")


def _revoke_merge_once(
    db: Session, *, plan_version: str, case_id: str, actor_id: str, reason: str
) -> dict[str, Any]:
    row = _require_case_row(db, plan_version, case_id)
    if row.state == identity_merges.State.REVOKED.value:
        # 幂等：重复撤销返回首次生成的反向版本。
        revision_row = get_alias_revision_for_case(
            db, plan_version, case_id, identity_merges.RevisionAction.UNMERGE.value
        )
        assert revision_row is not None
        case = _case_row_to_domain(
            row, list_merge_evidence(db, plan_version, case_id)
        )
        return {
            "case": _case_to_dict(case),
            "alias_version": revision_row.version,
            "revision": _revision_to_dict(_revision_row_to_domain(revision_row)),
        }

    case = _case_row_to_domain(row, list_merge_evidence(db, plan_version, case_id))
    now = datetime.now(timezone.utc)
    try:
        revoked = identity_merges.revoke(case, actor_id, now, reason)
        revisions = _domain_revisions(db, plan_version)
        version = max((r.version for r in revisions), default=0) + 1
        reversal = identity_merges.reversal_revision(
            revoked, version=version, actor_id=actor_id, now=now, reason=reason
        )
    except identity_merges.DomainError as exc:
        db.rollback()
        raise MergeConflictError(str(exc)) from exc

    insert_alias_revision(
        db,
        plan_version=plan_version,
        version=reversal.version,
        case_id=reversal.case_id,
        action=reversal.action.value,
        alias_student_id=reversal.alias_student_id,
        canonical_student_id=reversal.canonical_student_id,
        actor_id=reversal.actor_id,
        reason=reversal.reason,
        created_at=reversal.created_at,
    )
    ok = update_merge_case_row(
        db,
        plan_version=plan_version,
        case_id=case_id,
        expected_version=case.version,
        state=revoked.state.value,
        version=revoked.version,
        audit=[_audit_entry_to_dict(a) for a in revoked.audit],
        updated_at=now,
    )
    if not ok:
        db.rollback()
        raise MergeConflictError("案件已被并发修改，请重试")
    db.commit()
    return {
        "case": _case_to_dict(revoked),
        "alias_version": reversal.version,
        "revision": _revision_to_dict(reversal),
    }


def revoke_merge_case(
    db: Session, *, plan_version: str, case_id: str, actor_id: str, reason: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    for _attempt in range(3):
        try:
            return _revoke_merge_once(
                db,
                plan_version=plan_version,
                case_id=case_id,
                actor_id=actor_id,
                reason=reason,
            )
        except IntegrityError:
            db.rollback()
    raise MergeConflictError("并发写入冲突，请稍后重试")


def alias_registry(db: Session, plan_version: str) -> dict[str, Any]:
    """当前生效的别名映射与完整版本历史。"""
    _require_plan(db, plan_version)
    revisions = _domain_revisions(db, plan_version)
    current = max((r.version for r in revisions), default=0)
    links = identity_merges.build_alias_links(revisions)
    resolved = identity_merges.resolve_links(links)
    return {
        "plan_version": plan_version,
        "current_version": current,
        "aliases": [
            {
                "alias_student_id": alias,
                "canonical_student_id": resolved[alias],
                "linked_student_id": links[alias],
            }
            for alias in sorted(links)
        ],
        "revisions": [_revision_to_dict(r) for r in revisions],
    }
