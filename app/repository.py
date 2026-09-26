"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import (
    Freeze,
    IdentityAliasRevision,
    IdentityMergeCase,
    IdentityMergeEvidence,
    Plan,
)


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


# --- 身份合并：案件、证据与别名版本 -------------------------------------
# 以下函数只 flush 不 commit，事务边界由服务层控制，
# 以保证“案件状态 + 别名版本”在同一事务内落库。


def insert_merge_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    survivor_student_id: str,
    merged_student_id: str,
    state: str,
    reason: str,
    created_by: str,
    version: int,
    audit: list[dict[str, Any]],
    created_at: datetime,
    updated_at: datetime,
) -> bool:
    """写入新案件；已存在同名案件时返回 False。"""
    stmt = sqlite_insert(IdentityMergeCase).values(
        plan_version=plan_version,
        case_id=case_id,
        survivor_student_id=survivor_student_id,
        merged_student_id=merged_student_id,
        state=state,
        reason=reason,
        created_by=created_by,
        version=version,
        audit=audit,
        created_at=created_at,
        updated_at=updated_at,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "case_id"]
    ).returning(IdentityMergeCase.case_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.flush()
    return inserted is not None


def get_merge_case_row(
    db: Session, plan_version: str, case_id: str
) -> IdentityMergeCase | None:
    return db.get(IdentityMergeCase, (plan_version, case_id))


def list_merge_case_rows(
    db: Session, plan_version: str
) -> list[IdentityMergeCase]:
    stmt = (
        select(IdentityMergeCase)
        .where(IdentityMergeCase.plan_version == plan_version)
        .order_by(IdentityMergeCase.case_id)
    )
    return list(db.execute(stmt).scalars().all())


def update_merge_case_row(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    expected_version: int,
    state: str,
    version: int,
    audit: list[dict[str, Any]],
    updated_at: datetime,
) -> bool:
    """按乐观版本号更新案件；版本不匹配（并发修改）时返回 False。"""
    stmt = (
        update(IdentityMergeCase)
        .where(IdentityMergeCase.plan_version == plan_version)
        .where(IdentityMergeCase.case_id == case_id)
        .where(IdentityMergeCase.version == expected_version)
        .values(state=state, version=version, audit=audit, updated_at=updated_at)
    )
    result = db.execute(stmt)
    db.flush()
    return result.rowcount == 1


def insert_merge_evidence(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    evidence_id: str,
    kind: str,
    reference: str,
    submitted_by: str,
    created_at: datetime,
) -> bool:
    stmt = sqlite_insert(IdentityMergeEvidence).values(
        plan_version=plan_version,
        case_id=case_id,
        evidence_id=evidence_id,
        kind=kind,
        reference=reference,
        submitted_by=submitted_by,
        created_at=created_at,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "case_id", "evidence_id"]
    ).returning(IdentityMergeEvidence.id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.flush()
    return inserted is not None


def list_merge_evidence(
    db: Session, plan_version: str, case_id: str
) -> list[IdentityMergeEvidence]:
    stmt = (
        select(IdentityMergeEvidence)
        .where(IdentityMergeEvidence.plan_version == plan_version)
        .where(IdentityMergeEvidence.case_id == case_id)
        .order_by(IdentityMergeEvidence.id)
    )
    return list(db.execute(stmt).scalars().all())


def insert_alias_revision(
    db: Session,
    *,
    plan_version: str,
    version: int,
    case_id: str,
    action: str,
    alias_student_id: str,
    canonical_student_id: str,
    actor_id: str,
    reason: str,
    created_at: datetime,
) -> None:
    row = IdentityAliasRevision(
        plan_version=plan_version,
        version=version,
        case_id=case_id,
        action=action,
        alias_student_id=alias_student_id,
        canonical_student_id=canonical_student_id,
        actor_id=actor_id,
        reason=reason,
        created_at=created_at,
    )
    db.add(row)
    db.flush()


def list_alias_revisions(
    db: Session, plan_version: str
) -> list[IdentityAliasRevision]:
    stmt = (
        select(IdentityAliasRevision)
        .where(IdentityAliasRevision.plan_version == plan_version)
        .order_by(IdentityAliasRevision.version)
    )
    return list(db.execute(stmt).scalars().all())


def max_alias_version(db: Session, plan_version: str) -> int:
    stmt = select(func.max(IdentityAliasRevision.version)).where(
        IdentityAliasRevision.plan_version == plan_version
    )
    return db.execute(stmt).scalar_one_or_none() or 0


def get_alias_revision_for_case(
    db: Session, plan_version: str, case_id: str, action: str
) -> IdentityAliasRevision | None:
    stmt = (
        select(IdentityAliasRevision)
        .where(IdentityAliasRevision.plan_version == plan_version)
        .where(IdentityAliasRevision.case_id == case_id)
        .where(IdentityAliasRevision.action == action)
        .order_by(IdentityAliasRevision.version)
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()
