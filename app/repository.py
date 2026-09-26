"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import AliasVersion, MergeCase, MergeEvidence
from .models import Event as EventModel
from .models import Freeze, Plan


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


def list_freezes(db: Session, plan_version: str) -> list[Freeze]:
    stmt = (
        select(Freeze)
        .where(Freeze.plan_version == plan_version)
        .order_by(Freeze.freeze_id)
    )
    return list(db.execute(stmt).scalars().all())


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
    identity_version: int | None = None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
        identity_version=identity_version,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


def insert_merge_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    survivor_id: str,
    merged_id: str,
    reason: str,
    created_by: str,
) -> MergeCase | None:
    """登记合并案件；同 plan 下 case_id 冲突时返回 None。"""
    stmt = sqlite_insert(MergeCase).values(
        plan_version=plan_version,
        case_id=case_id,
        survivor_id=survivor_id,
        merged_id=merged_id,
        state="OPENED",
        reason=reason,
        created_by=created_by,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "case_id"]
    ).returning(MergeCase.case_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(MergeCase, (plan_version, case_id))
    return None


def get_merge_case(
    db: Session, plan_version: str, case_id: str
) -> MergeCase | None:
    return db.get(MergeCase, (plan_version, case_id))


def list_merge_cases(db: Session, plan_version: str) -> list[MergeCase]:
    stmt = (
        select(MergeCase)
        .where(MergeCase.plan_version == plan_version)
        .order_by(MergeCase.case_id)
    )
    return list(db.execute(stmt).scalars().all())


def insert_evidence(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    evidence_type: str,
    reference: str,
    detail: dict[str, Any],
    submitted_by: str,
) -> MergeEvidence:
    row = MergeEvidence(
        plan_version=plan_version,
        case_id=case_id,
        evidence_type=evidence_type,
        reference=reference,
        detail=detail,
        submitted_by=submitted_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_evidence(
    db: Session, plan_version: str, case_id: str
) -> list[MergeEvidence]:
    stmt = (
        select(MergeEvidence)
        .where(MergeEvidence.plan_version == plan_version)
        .where(MergeEvidence.case_id == case_id)
        .order_by(MergeEvidence.id)
    )
    return list(db.execute(stmt).scalars().all())


def count_evidence(db: Session, plan_version: str, case_id: str) -> int:
    stmt = (
        select(func.count())
        .select_from(MergeEvidence)
        .where(MergeEvidence.plan_version == plan_version)
        .where(MergeEvidence.case_id == case_id)
    )
    return int(db.execute(stmt).scalar_one())


def stage_alias_version(
    db: Session,
    *,
    plan_version: str,
    seq: int,
    case_id: str,
    action: str,
    survivor_id: str,
    merged_id: str,
    actor_id: str,
    reason: str,
) -> AliasVersion:
    """把别名版本行加入当前事务（不提交）。

    批准/撤销需要与案件状态在同一事务内提交，由服务层统一 commit；
    (plan_version, seq) 主键保证并发下只有一个写入者成功。
    """
    row = AliasVersion(
        plan_version=plan_version,
        seq=seq,
        case_id=case_id,
        action=action,
        survivor_id=survivor_id,
        merged_id=merged_id,
        actor_id=actor_id,
        reason=reason,
    )
    db.add(row)
    return row


def list_alias_versions(db: Session, plan_version: str) -> list[AliasVersion]:
    stmt = (
        select(AliasVersion)
        .where(AliasVersion.plan_version == plan_version)
        .order_by(AliasVersion.seq)
    )
    return list(db.execute(stmt).scalars().all())


def latest_alias_seq(db: Session, plan_version: str) -> int:
    stmt = (
        select(AliasVersion.seq)
        .where(AliasVersion.plan_version == plan_version)
        .order_by(AliasVersion.seq.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none() or 0
