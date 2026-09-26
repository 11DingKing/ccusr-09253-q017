"""身份合并的纯领域逻辑：可撤销别名映射、链式解析与循环校验。

别名映射以追加式版本记录表达：每个版本是一次 ``merge``（建立
``merged_id -> survivor_id`` 边）或 ``unmerge``（反向撤销该边）。
任意版本的有效映射由按序重放版本记录得到，因此历史查询可以固定在
指定版本上，而已签发的冻结快照不受后续合并或撤销影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping


class Action(StrEnum):
    MERGE = "merge"
    UNMERGE = "unmerge"


class IdentityError(ValueError):
    """身份图约束冲突（自并、重复映射、循环）。"""


@dataclass(frozen=True)
class AliasEntry:
    """一条不可变的别名版本记录。"""

    seq: int
    case_id: str
    action: Action
    survivor_id: str
    merged_id: str
    actor_id: str
    reason: str


def effective_map(
    entries: Iterable[AliasEntry], up_to_seq: int | None = None
) -> dict[str, str]:
    """按版本顺序重放，得到指定版本下有效的 ``merged -> survivor`` 映射。"""
    mapping: dict[str, str] = {}
    for entry in sorted(entries, key=lambda e: e.seq):
        if up_to_seq is not None and entry.seq > up_to_seq:
            continue
        if entry.action == Action.MERGE:
            mapping[entry.merged_id] = entry.survivor_id
        else:
            mapping.pop(entry.merged_id, None)
    return mapping


def resolve(mapping: Mapping[str, str], student_id: str) -> str:
    """沿合并链解析到当前规范身份。

    写入路径会拒绝循环；这里仍用访问集合做防御性截断，确保即使
    数据被外部破坏，重放也能确定性地终止。
    """
    seen: set[str] = set()
    current = student_id
    while current in mapping and current not in seen:
        seen.add(current)
        current = mapping[current]
    return current


def validate_merge(
    mapping: Mapping[str, str], merged_id: str, survivor_id: str
) -> None:
    """校验新增 ``merged_id -> survivor_id`` 边是否合法。

    - 不允许身份与自身合并；
    - 不允许 ``merged_id`` 已存在有效出边（须先撤销再重新合并）；
    - 不允许成环：``survivor_id`` 的解析链终点不能是 ``merged_id``。
    """
    if merged_id == survivor_id:
        raise IdentityError("不能将身份与自身合并")
    if merged_id in mapping:
        raise IdentityError(
            f"身份 '{merged_id}' 已存在有效别名映射，请先撤销再重新合并"
        )
    if resolve(mapping, survivor_id) == merged_id:
        raise IdentityError(
            f"合并 '{merged_id}' -> '{survivor_id}' 会形成循环，已拒绝"
        )
