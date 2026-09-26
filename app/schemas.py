"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    student_id: str = ""
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    student_id: str = ""
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    source_student_ids: list[str] = []
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    alias_version: int | None = None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


class MergeCaseOpenIn(BaseModel):
    case_id: str = Field(..., min_length=1, max_length=128)
    survivor_student_id: str = Field(..., min_length=1, max_length=128)
    merged_student_id: str = Field(..., min_length=1, max_length=128)
    actor_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field("", max_length=512)


class MergeEvidenceIn(BaseModel):
    evidence_id: str = Field(..., min_length=1, max_length=128)
    kind: str = Field(..., min_length=1, max_length=64)
    reference: str = Field(..., min_length=1, max_length=256)
    actor_id: str = Field(..., min_length=1, max_length=128)


class MergeActionIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)


class MergeEvidenceOut(BaseModel):
    evidence_id: str
    kind: str
    reference: str
    submitted_by: str
    submitted_at: str


class MergeAuditOut(BaseModel):
    sequence: int
    action: str
    actor_id: str
    occurred_at: str
    before: str
    after: str
    reason: str
    fingerprint: str


class MergeCaseOut(BaseModel):
    case_id: str
    plan_version: str
    survivor_student_id: str
    merged_student_id: str
    state: str
    reason: str
    created_by: str
    version: int
    created_at: str
    updated_at: str
    evidence: list[MergeEvidenceOut]
    audit: list[MergeAuditOut]


class MergeActionOut(BaseModel):
    case: MergeCaseOut
    alias_version: int
    revision: dict[str, Any]


class MergeImpactOut(BaseModel):
    case_id: str
    plan_version: str
    state: str
    survivor_student_id: str
    merged_student_id: str
    current_alias_version: int
    evidence_count: int
    approvable: bool
    blockers: list[str]
    before: dict[str, Any]
    after: dict[str, Any]


class AliasRegistryOut(BaseModel):
    plan_version: str
    current_version: int
    aliases: list[dict[str, Any]]
    revisions: list[dict[str, Any]]
