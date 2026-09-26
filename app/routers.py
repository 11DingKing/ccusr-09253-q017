"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    AliasRegistryOut,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    MergeActionIn,
    MergeActionOut,
    MergeCaseOpenIn,
    MergeCaseOut,
    MergeEvidenceIn,
    MergeImpactOut,
    PlanIn,
    PlanOut,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


def _merge_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (services.MergeCaseNotFoundError, services.AliasVersionNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(
    plan_version: str,
    alias_version: int | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version, alias_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.AliasVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str,
    student_id: str,
    alias_version: int | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.student_progress(
            db, plan_version, student_id, alias_version
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.AliasVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- 身份合并：案件、证据、影响预览、批准与撤销 ----------------------------


@router.post(
    "/plans/{plan_version}/identity-merges",
    response_model=MergeCaseOut,
    status_code=status.HTTP_201_CREATED,
)
def open_identity_merge(
    plan_version: str, body: MergeCaseOpenIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.open_merge_case(
            db,
            plan_version=plan_version,
            case_id=body.case_id,
            survivor_student_id=body.survivor_student_id,
            merged_student_id=body.merged_student_id,
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MergeConflictError as exc:
        raise _merge_error(exc) from exc


@router.get(
    "/plans/{plan_version}/identity-merges",
    response_model=list[MergeCaseOut],
)
def list_identity_merges(
    plan_version: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.list_merge_cases(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/identity-merges/{case_id}",
    response_model=MergeCaseOut,
)
def get_identity_merge(
    plan_version: str, case_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        case = services.get_merge_case(db, plan_version, case_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if case is None:
        raise HTTPException(status_code=404, detail="merge case not found")
    return case


@router.post(
    "/plans/{plan_version}/identity-merges/{case_id}/evidence",
    response_model=MergeCaseOut,
    status_code=status.HTTP_201_CREATED,
)
def post_merge_evidence(
    plan_version: str,
    case_id: str,
    body: MergeEvidenceIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.add_merge_evidence(
            db,
            plan_version=plan_version,
            case_id=case_id,
            evidence_id=body.evidence_id,
            kind=body.kind,
            reference=body.reference,
            actor_id=body.actor_id,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (services.MergeCaseNotFoundError, services.MergeConflictError) as exc:
        raise _merge_error(exc) from exc


@router.get(
    "/plans/{plan_version}/identity-merges/{case_id}/impact",
    response_model=MergeImpactOut,
)
def get_merge_impact(
    plan_version: str, case_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.preview_merge_impact(db, plan_version, case_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.MergeCaseNotFoundError as exc:
        raise _merge_error(exc) from exc


@router.post(
    "/plans/{plan_version}/identity-merges/{case_id}/approve",
    response_model=MergeActionOut,
)
def approve_identity_merge(
    plan_version: str,
    case_id: str,
    body: MergeActionIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.approve_merge_case(
            db,
            plan_version=plan_version,
            case_id=case_id,
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (services.MergeCaseNotFoundError, services.MergeConflictError) as exc:
        raise _merge_error(exc) from exc


@router.post(
    "/plans/{plan_version}/identity-merges/{case_id}/revoke",
    response_model=MergeActionOut,
)
def revoke_identity_merge(
    plan_version: str,
    case_id: str,
    body: MergeActionIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.revoke_merge_case(
            db,
            plan_version=plan_version,
            case_id=case_id,
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (services.MergeCaseNotFoundError, services.MergeConflictError) as exc:
        raise _merge_error(exc) from exc


@router.get(
    "/plans/{plan_version}/identity-aliases",
    response_model=AliasRegistryOut,
)
def get_alias_registry(
    plan_version: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.alias_registry(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
