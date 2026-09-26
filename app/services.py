"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .core.identity import Action, AliasEntry, IdentityError
from .core.identity import effective_map, resolve, validate_merge
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .models import MergeCase
from .repository import (
    count_evidence,
    get_freeze,
    get_merge_case,
    get_plan,
    insert_events,
    insert_evidence,
    insert_freeze,
    insert_merge_case,
    latest_alias_seq,
    list_alias_versions,
    list_evidence,
    list_freezes,
    list_merge_cases,
    load_events,
    load_events_up_to,
    max_event_id,
    stage_alias_version,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class CaseNotFoundError(Exception):
    pass


class CaseConflictError(Exception):
    pass


class CaseStateError(Exception):
    pass


class MergeConflictError(Exception):
    """并发写入别名版本冲突。"""


class MergeValidationError(Exception):
    """合并违反身份图约束（自并、重复映射、循环、撤销时映射已变）。"""


class EvidenceRequiredError(Exception):
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


def _alias_entries(db: Session, plan_version: str) -> list[AliasEntry]:
    return [
        AliasEntry(
            seq=row.seq,
            case_id=row.case_id,
            action=Action(row.action),
            survivor_id=row.survivor_id,
            merged_id=row.merged_id,
            actor_id=row.actor_id,
            reason=row.reason,
        )
        for row in list_alias_versions(db, plan_version)
    ]


def _resolve_identity_seq(
    db: Session, plan_version: str, identity_version: int | None
) -> int:
    """把请求的身份版本固定到已签发的版本范围内（0 表示尚无合并）。"""
    latest = latest_alias_seq(db, plan_version)
    if identity_version is None:
        return latest
    return max(0, min(identity_version, latest))


def _alias_map_at(
    db: Session, plan_version: str, identity_version: int | None
) -> tuple[dict[str, str], int]:
    seq = _resolve_identity_seq(db, plan_version, identity_version)
    return effective_map(_alias_entries(db, plan_version), up_to_seq=seq), seq


def current_snapshot(
    db: Session, plan_version: str, identity_version: int | None = None
) -> Snapshot:
    plan = _require_plan(db, plan_version)
    alias_map, seq = _alias_map_at(db, plan_version, identity_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        alias_map=alias_map,
        identity_version=seq,
    )


def student_progress(
    db: Session,
    plan_version: str,
    student_id: str,
    identity_version: int | None = None,
) -> dict[str, Any] | None:
    alias_map, _ = _alias_map_at(db, plan_version, identity_version)
    snap = current_snapshot(db, plan_version, identity_version)
    # 允许用任意历史学号查询：先解析到该版本下的规范身份。
    canonical_id = resolve(alias_map, student_id)
    return explain_student(snap, canonical_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    identity_version = latest_alias_seq(db, plan_version)
    alias_map = effective_map(
        _alias_entries(db, plan_version), up_to_seq=identity_version
    )
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
        alias_map=alias_map,
        identity_version=identity_version,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
        identity_version=identity_version,
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


# ---------------------------------------------------------------------------
# 身份合并案件：证据核验 -> 影响预览 -> 批准（建立别名版本）-> 撤销（反向版本）
# ---------------------------------------------------------------------------


class CaseState(StrEnum):
    OPENED = "OPENED"
    APPROVED = "APPROVED"
    REVOKED = "REVOKED"


def _require_case(db: Session, plan_version: str, case_id: str) -> MergeCase:
    case = get_merge_case(db, plan_version, case_id)
    if case is None:
        raise CaseNotFoundError(
            f"merge case '{case_id}' for plan '{plan_version}' does not exist"
        )
    return case


def _case_to_dict(db: Session, case: MergeCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "plan_version": case.plan_version,
        "survivor_id": case.survivor_id,
        "merged_id": case.merged_id,
        "state": case.state,
        "reason": case.reason,
        "created_by": case.created_by,
        "approved_by": case.approved_by,
        "approved_version": case.approved_version,
        "revoked_by": case.revoked_by,
        "revoked_version": case.revoked_version,
        "created_at": case.created_at,
        "updated_at": case.updated_at,
        "evidence": [
            {
                "evidence_id": ev.id,
                "evidence_type": ev.evidence_type,
                "reference": ev.reference,
                "detail": dict(ev.detail or {}),
                "submitted_by": ev.submitted_by,
                "created_at": ev.created_at,
            }
            for ev in list_evidence(db, case.plan_version, case.case_id)
        ],
    }


def open_merge_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    survivor_id: str,
    merged_id: str,
    reason: str,
    created_by: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    if survivor_id == merged_id:
        raise MergeValidationError("不能将身份与自身合并")
    row = insert_merge_case(
        db,
        plan_version=plan_version,
        case_id=case_id,
        survivor_id=survivor_id,
        merged_id=merged_id,
        reason=reason,
        created_by=created_by,
    )
    if row is None:
        raise CaseConflictError(
            f"merge case '{case_id}' for plan '{plan_version}' already exists"
        )
    return _case_to_dict(db, row)


def get_merge_case_detail(
    db: Session, plan_version: str, case_id: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    return _case_to_dict(db, _require_case(db, plan_version, case_id))


def list_merge_case_details(db: Session, plan_version: str) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    return [_case_to_dict(db, case) for case in list_merge_cases(db, plan_version)]


def add_evidence(
    db: Session,
    plan_version: str,
    case_id: str,
    *,
    evidence_type: str,
    reference: str,
    detail: dict[str, Any],
    submitted_by: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    case = _require_case(db, plan_version, case_id)
    if case.state != CaseState.OPENED.value:
        raise CaseStateError("仅打开状态的案件可以补充证据")
    insert_evidence(
        db,
        plan_version=plan_version,
        case_id=case_id,
        evidence_type=evidence_type,
        reference=reference,
        detail=detail,
        submitted_by=submitted_by,
    )
    return _case_to_dict(db, case)


_SUMMARY_FIELDS = (
    "student_id",
    "confirmed_seconds",
    "pending_seconds",
    "adjustment_seconds",
    "total_seconds",
    "lesson_units",
    "pending_lesson_units",
    "meets_requirement",
    "source_student_ids",
)


def _progress_summary(student: dict[str, Any] | None) -> dict[str, Any] | None:
    if student is None:
        return None
    return {key: student.get(key) for key in _SUMMARY_FIELDS}


def preview_merge(
    db: Session, plan_version: str, case_id: str
) -> dict[str, Any]:
    """影响预览：按当前身份版本模拟合并效果，不产生任何写入。"""
    plan = _require_plan(db, plan_version)
    case = _require_case(db, plan_version, case_id)
    entries = _alias_entries(db, plan_version)
    current_seq = entries[-1].seq if entries else 0
    mapping = effective_map(entries)

    applicable = True
    rejection_reason: str | None = None
    try:
        validate_merge(mapping, case.merged_id, case.survivor_id)
    except IdentityError as exc:
        applicable = False
        rejection_reason = str(exc)

    events = load_events(db, plan_version)

    def _snap(alias_map: dict[str, str], seq: int) -> Snapshot:
        return build_snapshot(
            events,
            plan_version=plan_version,
            timezone_name=plan.iana_timezone,
            required_seconds=plan.required_seconds,
            alias_map=alias_map,
            identity_version=seq,
        )

    before_snap = _snap(mapping, current_seq)
    before = {
        "survivor": _progress_summary(
            explain_student(before_snap, resolve(mapping, case.survivor_id))
        ),
        "merged": _progress_summary(
            explain_student(before_snap, resolve(mapping, case.merged_id))
        ),
    }

    after: dict[str, Any] | None = None
    events_reattributed = 0
    if applicable:
        projected = dict(mapping)
        projected[case.merged_id] = case.survivor_id
        after_snap = _snap(projected, current_seq + 1)
        after = _progress_summary(
            explain_student(after_snap, resolve(projected, case.survivor_id))
        )
        events_reattributed = sum(
            1
            for e in events
            if resolve(mapping, e.student_id) != resolve(projected, e.student_id)
        )

    return {
        "case_id": case.case_id,
        "plan_version": plan_version,
        "state": case.state,
        "survivor_id": case.survivor_id,
        "merged_id": case.merged_id,
        "identity_version": current_seq,
        "projected_version": current_seq + 1,
        "applicable": applicable,
        "rejection_reason": rejection_reason,
        "before": before,
        "after": after,
        "events_reattributed": events_reattributed,
        # 已签发的冻结固定在各自的身份版本上，合并不会改写它们。
        "freezes": [
            {
                "freeze_id": f.freeze_id,
                "identity_version": f.identity_version,
                "immutable": True,
            }
            for f in list_freezes(db, plan_version)
        ],
    }


def _is_lock_contention(exc: OperationalError) -> bool:
    return "locked" in str(exc).lower()


def approve_merge(
    db: Session,
    plan_version: str,
    case_id: str,
    *,
    actor_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """批准案件：校验证据与合并图后追加一个 merge 别名版本。

    校验与版本号取自同一次读取，(plan_version, seq) 主键串行化并发
    提交；冲突时重读最新映射并重新校验（例如两个相反方向的合并同时
    提交时，只有一个能成功）。
    """
    _require_plan(db, plan_version)
    for _attempt in range(4):
        case = _require_case(db, plan_version, case_id)
        if case.state != CaseState.OPENED.value:
            raise CaseStateError(f"案件状态为 {case.state}，不能批准")
        if count_evidence(db, plan_version, case_id) == 0:
            raise EvidenceRequiredError("批准前至少需要一条归属证据")
        entries = _alias_entries(db, plan_version)
        mapping = effective_map(entries)
        try:
            validate_merge(mapping, case.merged_id, case.survivor_id)
        except IdentityError as exc:
            raise MergeValidationError(str(exc)) from exc
        seq = (entries[-1].seq if entries else 0) + 1
        stage_alias_version(
            db,
            plan_version=plan_version,
            seq=seq,
            case_id=case_id,
            action=Action.MERGE.value,
            survivor_id=case.survivor_id,
            merged_id=case.merged_id,
            actor_id=actor_id,
            reason=reason or case.reason,
        )
        case.state = CaseState.APPROVED.value
        case.approved_by = actor_id
        case.approved_version = seq
        case.updated_at = datetime.now(timezone.utc)
        try:
            db.commit()
            return _case_to_dict(db, case)
        except IntegrityError:
            db.rollback()
        except OperationalError as exc:
            db.rollback()
            if not _is_lock_contention(exc):
                raise
    raise MergeConflictError("身份版本并发写入冲突，请重试")


def revoke_merge(
    db: Session,
    plan_version: str,
    case_id: str,
    *,
    actor_id: str,
    reason: str,
) -> dict[str, Any]:
    """撤销案件：追加 unmerge 反向版本，历史版本与已签发冻结保持不变。"""
    _require_plan(db, plan_version)
    for _attempt in range(4):
        case = _require_case(db, plan_version, case_id)
        if case.state != CaseState.APPROVED.value:
            raise CaseStateError("仅已批准的案件可以撤销")
        entries = _alias_entries(db, plan_version)
        mapping = effective_map(entries)
        if mapping.get(case.merged_id) != case.survivor_id:
            raise MergeValidationError(
                "该案件的别名映射已被其他版本变更，无法按原样撤销"
            )
        seq = (entries[-1].seq if entries else 0) + 1
        stage_alias_version(
            db,
            plan_version=plan_version,
            seq=seq,
            case_id=case_id,
            action=Action.UNMERGE.value,
            survivor_id=case.survivor_id,
            merged_id=case.merged_id,
            actor_id=actor_id,
            reason=reason,
        )
        case.state = CaseState.REVOKED.value
        case.revoked_by = actor_id
        case.revoked_version = seq
        case.updated_at = datetime.now(timezone.utc)
        try:
            db.commit()
            return _case_to_dict(db, case)
        except IntegrityError:
            db.rollback()
        except OperationalError as exc:
            db.rollback()
            if not _is_lock_contention(exc):
                raise
    raise MergeConflictError("身份版本并发写入冲突，请重试")


def list_alias_version_dicts(
    db: Session, plan_version: str
) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    return [
        {
            "seq": row.seq,
            "case_id": row.case_id,
            "action": row.action,
            "survivor_id": row.survivor_id,
            "merged_id": row.merged_id,
            "actor_id": row.actor_id,
            "reason": row.reason,
            "created_at": row.created_at,
        }
        for row in list_alias_versions(db, plan_version)
    ]


def alias_map_view(
    db: Session, plan_version: str, identity_version: int | None
) -> dict[str, Any]:
    """指定身份版本下的有效别名映射（默认最新版本）。"""
    _require_plan(db, plan_version)
    mapping, seq = _alias_map_at(db, plan_version, identity_version)
    return {
        "plan_version": plan_version,
        "identity_version": seq,
        "aliases": mapping,
    }
