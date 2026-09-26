"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    AliasMapOut,
    AliasVersionOut,
    DecisionIn,
    DiffOut,
    EventBatchIn,
    EvidenceIn,
    FreezeIn,
    ImportResult,
    MergeCaseIn,
    MergeCaseOut,
    MergePreviewOut,
    PlanIn,
    PlanOut,
    RevokeIn,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


def _identity_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, (services.PlanNotFoundError, services.CaseNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(
        exc,
        (
            services.CaseConflictError,
            services.CaseStateError,
            services.MergeConflictError,
        ),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (services.MergeValidationError, services.EvidenceRequiredError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


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
    identity_version: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version, identity_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str,
    student_id: str,
    identity_version: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.student_progress(
            db, plan_version, student_id, identity_version
        )
    except services.PlanNotFoundError as exc:
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


# ---------------------------------------------------------------------------
# 身份合并案件：登记、证据、影响预览、批准、撤销与别名版本查询
# ---------------------------------------------------------------------------


@router.post(
    "/plans/{plan_version}/identity/cases",
    response_model=MergeCaseOut,
    status_code=status.HTTP_201_CREATED,
)
def open_case(
    plan_version: str, body: MergeCaseIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.open_merge_case(
            db,
            plan_version=plan_version,
            case_id=body.case_id,
            survivor_id=body.survivor_id,
            merged_id=body.merged_id,
            reason=body.reason,
            created_by=body.created_by,
        )
    except (
        services.PlanNotFoundError,
        services.CaseConflictError,
        services.MergeValidationError,
    ) as exc:
        raise _identity_errors(exc) from exc


@router.get(
    "/plans/{plan_version}/identity/cases",
    response_model=list[MergeCaseOut],
)
def list_cases(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.list_merge_case_details(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/identity/cases/{case_id}",
    response_model=MergeCaseOut,
)
def get_case(
    plan_version: str, case_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.get_merge_case_detail(db, plan_version, case_id)
    except (services.PlanNotFoundError, services.CaseNotFoundError) as exc:
        raise _identity_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/identity/cases/{case_id}/evidence",
    response_model=MergeCaseOut,
    status_code=status.HTTP_201_CREATED,
)
def post_evidence(
    plan_version: str, case_id: str, body: EvidenceIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.add_evidence(
            db,
            plan_version,
            case_id,
            evidence_type=body.evidence_type,
            reference=body.reference,
            detail=body.detail,
            submitted_by=body.submitted_by,
        )
    except (
        services.PlanNotFoundError,
        services.CaseNotFoundError,
        services.CaseStateError,
    ) as exc:
        raise _identity_errors(exc) from exc


@router.get(
    "/plans/{plan_version}/identity/cases/{case_id}/preview",
    response_model=MergePreviewOut,
)
def preview_case(
    plan_version: str, case_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.preview_merge(db, plan_version, case_id)
    except (services.PlanNotFoundError, services.CaseNotFoundError) as exc:
        raise _identity_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/identity/cases/{case_id}/approve",
    response_model=MergeCaseOut,
)
def approve_case(
    plan_version: str, case_id: str, body: DecisionIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.approve_merge(
            db,
            plan_version,
            case_id,
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except (
        services.PlanNotFoundError,
        services.CaseNotFoundError,
        services.CaseStateError,
        services.MergeConflictError,
        services.MergeValidationError,
        services.EvidenceRequiredError,
    ) as exc:
        raise _identity_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/identity/cases/{case_id}/revoke",
    response_model=MergeCaseOut,
)
def revoke_case(
    plan_version: str, case_id: str, body: RevokeIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.revoke_merge(
            db,
            plan_version,
            case_id,
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except (
        services.PlanNotFoundError,
        services.CaseNotFoundError,
        services.CaseStateError,
        services.MergeConflictError,
        services.MergeValidationError,
    ) as exc:
        raise _identity_errors(exc) from exc


@router.get(
    "/plans/{plan_version}/identity/versions",
    response_model=list[AliasVersionOut],
)
def list_identity_versions(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.list_alias_version_dicts(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/identity/versions/{seq}",
    response_model=AliasMapOut,
)
def get_identity_version(
    plan_version: str, seq: int, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.alias_map_view(db, plan_version, seq)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
