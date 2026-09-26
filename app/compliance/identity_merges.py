"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Iterable, Mapping


class State(StrEnum):
    OPENED = "opened"
    APPROVED = "approved"
    REVOKED = "revoked"


ALLOWED_TRANSITIONS: Mapping[State, frozenset[State]] = {
    State.OPENED: frozenset({State.APPROVED}),
    State.APPROVED: frozenset({State.REVOKED}),
    State.REVOKED: frozenset({}),
}


class RevisionAction(StrEnum):
    MERGE = "merge"
    UNMERGE = "unmerge"


class DomainError(ValueError):
    """封装领域状态与业务约束。"""


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    action: str
    actor_id: str
    occurred_at: datetime
    before: str
    after: str
    reason: str
    fingerprint: str


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    kind: str
    reference: str
    submitted_by: str
    submitted_at: datetime


@dataclass(frozen=True)
class MergeCase:
    case_id: str
    plan_version: str
    survivor_student_id: str
    merged_student_id: str
    state: State
    reason: str
    created_by: str
    version: int
    created_at: datetime
    updated_at: datetime
    evidence: tuple[Evidence, ...] = ()
    audit: tuple[AuditEntry, ...] = ()

    @property
    def identifier(self) -> str:
        return self.case_id


@dataclass(frozen=True)
class AliasRevision:
    """别名注册表中的一个版本；撤销通过追加反向版本实现。"""

    version: int
    case_id: str
    action: RevisionAction
    alias_student_id: str
    canonical_student_id: str
    actor_id: str
    reason: str
    created_at: datetime


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise DomainError("时间必须包含时区")
    return value.astimezone(UTC)


def _fingerprint(identifier: str, version: int, action: str, actor: str, reason: str) -> str:
    raw = f"{identifier}|{version}|{action}|{actor}|{reason}".encode("utf-8")
    return sha256(raw).hexdigest()


def _require_text(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise DomainError(message)
    return normalized


def _append_audit(
    record: MergeCase,
    *,
    action: str,
    actor_id: str,
    instant: datetime,
    before: str,
    after: str,
    reason: str,
    next_version: int,
) -> AuditEntry:
    return AuditEntry(
        sequence=len(record.audit) + 1,
        action=action,
        actor_id=actor_id,
        occurred_at=instant,
        before=before,
        after=after,
        reason=reason,
        fingerprint=_fingerprint(record.identifier, next_version, action, actor_id, reason),
    )


def open_case(
    *,
    case_id: str,
    plan_version: str,
    survivor_student_id: str,
    merged_student_id: str,
    actor_id: str,
    now: datetime,
    reason: str = "",
) -> MergeCase:
    """开立身份合并案件：survivor 保留主身份，merged 成为可撤销别名。"""
    case_id = _require_text(case_id, "案件、方案与学员标识不能为空")
    plan_version = _require_text(plan_version, "案件、方案与学员标识不能为空")
    survivor = _require_text(survivor_student_id, "案件、方案与学员标识不能为空")
    merged = _require_text(merged_student_id, "案件、方案与学员标识不能为空")
    actor = _require_text(actor_id, "案件、方案与学员标识不能为空")
    if survivor == merged:
        raise DomainError("同一学员无需合并")
    instant = _utc(now)
    case = MergeCase(
        case_id=case_id,
        plan_version=plan_version,
        survivor_student_id=survivor,
        merged_student_id=merged,
        state=State.OPENED,
        reason=reason.strip(),
        created_by=actor,
        version=1,
        created_at=instant,
        updated_at=instant,
    )
    entry = _append_audit(
        case,
        action="open",
        actor_id=actor,
        instant=instant,
        before="",
        after=State.OPENED.value,
        reason=case.reason,
        next_version=1,
    )
    return replace(case, audit=(entry,))


def add_evidence(
    record: MergeCase,
    *,
    evidence_id: str,
    kind: str,
    reference: str,
    actor_id: str,
    now: datetime,
) -> MergeCase:
    """补充归属证据；只有进行中的案件可以补充。"""
    if record.state != State.OPENED:
        raise DomainError("仅进行中的案件可以补充证据")
    evidence_id = _require_text(evidence_id, "证据编号、类型与出处不能为空")
    kind = _require_text(kind, "证据编号、类型与出处不能为空")
    reference = _require_text(reference, "证据编号、类型与出处不能为空")
    actor = _require_text(actor_id, "状态变更必须记录操作人和原因")
    if any(item.evidence_id == evidence_id for item in record.evidence):
        raise DomainError("证据编号已存在")
    instant = _utc(now)
    item = Evidence(
        evidence_id=evidence_id,
        kind=kind,
        reference=reference,
        submitted_by=actor,
        submitted_at=instant,
    )
    next_version = record.version + 1
    entry = _append_audit(
        record,
        action="evidence",
        actor_id=actor,
        instant=instant,
        before=str(len(record.evidence)),
        after=str(len(record.evidence) + 1),
        reason=f"{kind}:{evidence_id}",
        next_version=next_version,
    )
    return replace(
        record,
        evidence=record.evidence + (item,),
        version=next_version,
        updated_at=instant,
        audit=record.audit + (entry,),
    )


def _transition(
    record: MergeCase, target: State, actor_id: str, now: datetime, reason: str
) -> MergeCase:
    instant = _utc(now)
    actor = _require_text(actor_id, "状态变更必须记录操作人和原因")
    note = _require_text(reason, "状态变更必须记录操作人和原因")
    if target == record.state:
        return record
    if target not in ALLOWED_TRANSITIONS.get(record.state, frozenset()):
        raise DomainError(f"不允许从 {record.state} 变更到 {target}")
    next_version = record.version + 1
    entry = _append_audit(
        record,
        action="transition",
        actor_id=actor,
        instant=instant,
        before=record.state.value,
        after=target.value,
        reason=note,
        next_version=next_version,
    )
    return replace(
        record,
        state=target,
        version=next_version,
        updated_at=instant,
        audit=record.audit + (entry,),
    )


def approve(record: MergeCase, actor_id: str, now: datetime, reason: str) -> MergeCase:
    """验证归属证据后批准案件，别名映射随新版本生效。"""
    if record.state == State.APPROVED:
        return record
    if not record.evidence:
        raise DomainError("缺少归属证据，不能批准合并")
    return _transition(record, State.APPROVED, actor_id, now, reason)


def revoke(record: MergeCase, actor_id: str, now: datetime, reason: str) -> MergeCase:
    """撤销已批准的合并；注册表追加反向版本，历史版本保持不变。"""
    if record.state == State.REVOKED:
        return record
    return _transition(record, State.REVOKED, actor_id, now, reason)


def merge_revision(
    record: MergeCase, *, version: int, actor_id: str, now: datetime, reason: str
) -> AliasRevision:
    if record.state != State.APPROVED:
        raise DomainError("仅已批准的案件可以生成合并版本")
    if version < 1:
        raise DomainError("版本号必须大于零")
    return AliasRevision(
        version=version,
        case_id=record.case_id,
        action=RevisionAction.MERGE,
        alias_student_id=record.merged_student_id,
        canonical_student_id=record.survivor_student_id,
        actor_id=_require_text(actor_id, "状态变更必须记录操作人和原因"),
        reason=reason.strip(),
        created_at=_utc(now),
    )


def reversal_revision(
    record: MergeCase, *, version: int, actor_id: str, now: datetime, reason: str
) -> AliasRevision:
    """误合并的反向版本：撤销同一别名链接，不改写已签发的版本。"""
    if record.state != State.REVOKED:
        raise DomainError("仅已撤销的案件可以生成反向版本")
    if version < 1:
        raise DomainError("版本号必须大于零")
    return AliasRevision(
        version=version,
        case_id=record.case_id,
        action=RevisionAction.UNMERGE,
        alias_student_id=record.merged_student_id,
        canonical_student_id=record.survivor_student_id,
        actor_id=_require_text(actor_id, "状态变更必须记录操作人和原因"),
        reason=reason.strip(),
        created_at=_utc(now),
    )


def build_alias_links(
    revisions: Iterable[AliasRevision], *, up_to_version: int | None = None
) -> dict[str, str]:
    """按版本折叠出直接别名链接（alias -> 直接 canonical）。"""
    links: dict[str, str] = {}
    for revision in sorted(revisions, key=lambda r: r.version):
        if up_to_version is not None and revision.version > up_to_version:
            continue
        if revision.action == RevisionAction.MERGE:
            links[revision.alias_student_id] = revision.canonical_student_id
        else:
            links.pop(revision.alias_student_id, None)
    return links


def resolve_root(links: Mapping[str, str], student_id: str) -> str:
    """沿合并链解析到根身份；发现历史数据成环时拒绝。"""
    seen: set[str] = set()
    current = student_id
    while current in links:
        if current in seen:
            raise DomainError("别名映射存在循环")
        seen.add(current)
        current = links[current]
    return current


def resolve_links(links: Mapping[str, str]) -> dict[str, str]:
    """把每个别名解析到合并链末端，供重放聚合使用。"""
    return {alias: resolve_root(links, alias) for alias in links}


def would_cycle(
    links: Mapping[str, str], alias_student_id: str, canonical_student_id: str
) -> bool:
    """若新增 alias -> canonical 链接会形成环（含自合并）则返回 True。"""
    if alias_student_id == canonical_student_id:
        return True
    seen = {alias_student_id}
    current = canonical_student_id
    while current in links:
        nxt = links[current]
        if nxt in seen:
            return True
        seen.add(nxt)
        current = nxt
    return False


def ensure_linkable(
    links: Mapping[str, str], alias_student_id: str, canonical_student_id: str
) -> None:
    """批准前校验：别名未被占用且不会成环。"""
    if alias_student_id in links:
        raise DomainError("该学员已存在生效的别名映射，需先撤销")
    if would_cycle(links, alias_student_id, canonical_student_id):
        raise DomainError("别名映射会形成循环")


def verify_audit(record: MergeCase) -> bool:
    expected = 1
    seen: set[str] = set()
    for entry in record.audit:
        if entry.sequence != expected or entry.fingerprint in seen:
            return False
        seen.add(entry.fingerprint)
        expected += 1
    return True


def latest(records: Iterable[MergeCase]) -> dict[str, MergeCase]:
    result: dict[str, MergeCase] = {}
    for record in records:
        current = result.get(record.identifier)
        if current is None or (record.version, record.updated_at) > (
            current.version,
            current.updated_at,
        ):
            result[record.identifier] = record
    return result


def summarize(records: Iterable[MergeCase]) -> dict[str, int]:
    counts = {state.value: 0 for state in State}
    counts["invalid_audit"] = 0
    for record in latest(records).values():
        counts[record.state.value] += 1
        if not verify_audit(record):
            counts["invalid_audit"] += 1
    return counts
