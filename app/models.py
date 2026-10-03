"""SQLAlchemy 数据模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Shift(Base):
    """班次：半开区间 [start_ts, end_ts)，同设备不得重叠（由排他约束兜底）。"""

    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id"), nullable=False, index=True
    )
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("end_ts > start_ts", name="ck_shifts_valid_range"),
    )


class EventSource(Base):
    """每个 (设备, 来源) 的版本链游标，用于幂等校验与连续版本控制。"""

    __tablename__ = "event_sources"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_content_hash: Mapped[str | None] = mapped_column(String(64))


class EventVersion(Base):
    """停机事件的某个版本；同一来源的最新版本为当前生效事件。"""

    __tablename__ = "event_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("device_id", "source_id", "version", name="uq_event_version"),
        CheckConstraint("end_ts > start_ts", name="ck_events_valid_range"),
        CheckConstraint(
            "reason IN ('maintenance','failure','pending')", name="ck_events_reason"
        ),
    )


class ChangeCounter(Base):
    """变更序号计数器（单例行 id=1）。

    分配序号时在写事务内对该行 UPDATE ... RETURNING 递增，行锁持有至提交，
    因此序号顺序 == 提交顺序：seq 较小的变更不会晚于 seq 较大的变更可见，
    下游同步游标据此不会越过尚未提交的较早变更。
    """

    __tablename__ = "change_counter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 恒为 1
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ShiftResult(Base):
    """班次结算结果的一个修订。

    结算后原修订永久保留；迟到更正 / 现场人工裁定 / 撤销裁定触发重算时追加新修订
    并记录与上一修订的差额。
    seq 是全局递增的变更序号（提交顺序），下游按游标同步即读此表 ——
    修订与其变更记录是同一行，天然同事务提交/回滚，且内容不可变。
    """

    __tablename__ = "shift_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # 全局递增变更序号（提交顺序），作为下游同步游标
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    maintenance_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # 与上一修订的差额（首修订相对 0）
    d_maintenance: Mapped[int] = mapped_column(Integer, nullable=False)
    d_failure: Mapped[int] = mapped_column(Integer, nullable=False)
    d_pending: Mapped[int] = mapped_column(Integer, nullable=False)
    # 本次计算所依据的事件版本：[{"source_id", "version", "reason"}, ...]
    event_versions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # settle | correction | adjudication | adjudication_revoke
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("shift_id", "revision", name="uq_shift_result_revision"),
        UniqueConstraint("seq", name="uq_shift_results_seq"),
    )


class ManualAdjudication(Base):
    """已结算班次的现场人工裁定。

    - ruling_no 为调用方提供的唯一裁定单号（班次内唯一，建议全局唯一）；
    - 裁定分钟覆盖自动事件归因，重叠分钟按生效顺序（自增 id，即落库顺序）
      最新的未撤销裁定为准；撤销后回退到此前仍有效的裁定或事件归因；
    - 撤销通过将 status 置为 revoked 完成（留痕、支持重复撤销幂等），
      行本身不删除、历史修订不可改写。
    """

    __tablename__ = "manual_adjudications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id"), nullable=False, index=True
    )
    ruling_no: Mapped[str] = mapped_column(String(64), nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # active（生效中） | revoked（已撤销）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("shift_id", "ruling_no", name="uq_adjudication_no"),
        CheckConstraint("end_ts > start_ts", name="ck_adjudications_valid_range"),
        CheckConstraint(
            "reason IN ('maintenance','failure','pending')",
            name="ck_adjudications_reason",
        ),
        CheckConstraint(
            "status IN ('active','revoked')", name="ck_adjudications_status"
        ),
    )
