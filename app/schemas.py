"""Pydantic 请求/响应模型。

时间戳规则：必须带时区且精确到整分钟（秒与微秒为 0），否则 422。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# 保养 / 故障 / 待判
Reason = Literal["maintenance", "failure", "pending"]


def _check_ts(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError(
            "timestamp must include a timezone offset, e.g. 2026-10-01T08:00:00+08:00"
        )
    if v.second != 0 or v.microsecond != 0:
        raise ValueError("timestamp must be aligned to a whole minute (seconds must be 00)")
    return v


class _Interval(BaseModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return _check_ts(v)

    @model_validator(mode="after")
    def _range(self):
        if not self.start < self.end:
            raise ValueError("invalid interval: start must be earlier than end")
        return self


class DeviceCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64, examples=["CNC-01"])
    name: str = Field(default="", max_length=200)


class DeviceOut(BaseModel):
    id: str
    name: str


class ShiftCreate(_Interval):
    device_id: str = Field(min_length=1, max_length=64)


class ShiftOut(BaseModel):
    id: int
    device_id: str
    start: datetime
    end: datetime


class EventIn(_Interval):
    device_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    reason: Reason


class EventBatchItem(_Interval):
    """断网补传的单个事件版本：字段与校验同单条上报（设备在批次级给出）。"""

    source_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    reason: Reason


class EventBatchIn(BaseModel):
    """现场网关断网恢复后的一次性补传：同一设备的非空事件列表。"""

    device_id: str = Field(min_length=1, max_length=64)
    events: list[EventBatchItem] = Field(min_length=1)


class ReasonMinutes(BaseModel):
    maintenance: int
    failure: int
    pending: int


class EventVersionRef(BaseModel):
    source_id: str
    version: int
    reason: str


class RecomputedShift(BaseModel):
    """迟到更正触发重算后，某个受影响班次的新修订摘要。"""

    shift_id: int
    revision: int
    totals: ReasonMinutes
    delta: ReasonMinutes


class EventAccepted(BaseModel):
    device_id: str
    source_id: str
    version: int
    idempotent: bool  # True 表示同来源同版本同内容的重复提交，未产生新数据
    recomputed: list[RecomputedShift] = []


class EventBatchItemAck(BaseModel):
    """批量补传的逐项回执：idempotent=True 表示同版本同内容的已存在项（重试）。"""

    source_id: str
    version: int
    idempotent: bool


class EventBatchAccepted(BaseModel):
    """批量补传回执：逐项标明新建/幂等，并列出实际修订的班次及差额。"""

    device_id: str
    items: list[EventBatchItemAck]
    recomputed: list[RecomputedShift] = []


class ResultOut(BaseModel):
    shift_id: int
    device_id: str
    shift_start: datetime
    shift_end: datetime
    revision: int
    latest_revision: int
    totals: ReasonMinutes
    delta: ReasonMinutes  # 本修订相对上一修订的差额
    event_versions: list[EventVersionRef]
    trigger: str  # settle | correction
    created_at: datetime


class SettleResponse(BaseModel):
    settled: bool  # False 表示此前已结算，本次为幂等返回最新修订
    result: ResultOut


class ChangeEntry(BaseModel):
    """一次结算/更正/裁定产生的变更记录（与班次修订同一行存储，内容不可变）。"""

    seq: int  # 全局递增变更序号（提交顺序），作为同步游标
    shift_id: int
    device_id: str
    shift_start: datetime
    shift_end: datetime
    revision: int
    totals: ReasonMinutes
    delta: ReasonMinutes  # 本修订相对上一修订的差额
    event_versions: list[EventVersionRef]
    trigger: str  # settle | correction | adjudication | adjudication_revoke
    created_at: datetime


class ChangePage(BaseModel):
    """一页变更；next_cursor 供下一页使用，空页保持原游标。"""

    changes: list[ChangeEntry]
    next_cursor: int


# ---------------------------------------------------------------- 现场人工裁定


class AdjudicationIn(_Interval):
    """已结算班次的现场人工裁定：班次内整分钟半开区间 + 原因 + 说明。"""

    ruling_no: str = Field(
        min_length=1,
        max_length=64,
        description="唯一裁定单号（班次内唯一，建议全局唯一），用于幂等与撤销",
        examples=["R-20261001-001"],
    )
    reason: Reason
    note: str = Field(default="", max_length=500, description="说明（可选）")


class AdjudicationOut(BaseModel):
    """一条现场人工裁定的当前状态（撤销留痕）。"""

    ruling_no: str
    start: datetime
    end: datetime
    reason: str
    note: str
    status: str  # active | revoked
    created_at: datetime
    revoked_at: datetime | None = None


class RevisionRef(BaseModel):
    """操作产生的班次修订摘要；未产生修订时该对象为空（response 中为 null）。"""

    revision: int
    seq: int
    totals: ReasonMinutes
    delta: ReasonMinutes
    trigger: str


class AdjudicationAck(BaseModel):
    """提交/撤销裁定的统一回执：单号、状态、修订号（无修订时明确标示）。"""

    ruling_no: str
    status: str  # active | revoked
    idempotent: bool  # True 表示同单号重试/重复撤销，未产生任何修订
    revision: int | None  # 本次操作产生的修订号；无修订为 null
    latest_revision: int  # 班次当前最新修订号
    revision_detail: RevisionRef | None = None  # 本次修订摘要；无修订为 null
