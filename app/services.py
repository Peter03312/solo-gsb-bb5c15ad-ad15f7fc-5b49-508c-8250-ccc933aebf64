"""业务逻辑：设备、班次、事件版本链、结算与迟到更正重算。"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import attribution
from .attribution import EventView, RulingView
from .models import (
    ChangeCounter,
    Device,
    EventSource,
    EventVersion,
    ManualAdjudication,
    Shift,
    ShiftResult,
)
from .schemas import (
    AdjudicationIn,
    DeviceCreate,
    EventBatchIn,
    EventIn,
    ShiftCreate,
)


class ApiError(HTTPException):
    """统一错误格式：{"detail": {"code": ..., "message": ...}}"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(
            status_code=status_code, detail={"code": code, "message": message}
        )


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _content_hash(start: datetime, end: datetime, reason: str) -> str:
    raw = f"{start.isoformat()}|{end.isoformat()}|{reason}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get_device_or_404(db: Session, device_id: str) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise ApiError(404, "DEVICE_NOT_FOUND", f"unknown device: {device_id!r}")
    return device


# ---------------------------------------------------------------- 设备


def create_device(db: Session, payload: DeviceCreate) -> tuple[Device, bool]:
    existing = db.get(Device, payload.id)
    if existing is not None:
        return existing, False
    device = Device(id=payload.id, name=payload.name)
    db.add(device)
    try:
        db.commit()
    except IntegrityError:  # 并发重复创建
        db.rollback()
        return db.get(Device, payload.id), False
    return device, True


def list_devices(db: Session) -> list[Device]:
    return list(db.execute(select(Device).order_by(Device.id)).scalars().all())


# ---------------------------------------------------------------- 班次


def create_shift(db: Session, payload: ShiftCreate) -> Shift:
    get_device_or_404(db, payload.device_id)
    start, end = _utc(payload.start), _utc(payload.end)
    conflict = (
        db.execute(
            select(Shift).where(
                Shift.device_id == payload.device_id,
                Shift.start_ts < end,
                Shift.end_ts > start,
            )
        )
        .scalars()
        .first()
    )
    if conflict is not None:
        raise ApiError(
            409,
            "SHIFT_OVERLAP",
            f"shift overlaps existing shift id={conflict.id} "
            f"[{conflict.start_ts.isoformat()}, {conflict.end_ts.isoformat()}) "
            f"on device {payload.device_id!r}",
        )
    shift = Shift(device_id=payload.device_id, start_ts=start, end_ts=end)
    db.add(shift)
    try:
        db.commit()
    except IntegrityError:  # 排他约束兜底（并发创建）
        db.rollback()
        raise ApiError(
            409, "SHIFT_OVERLAP", "shift overlaps an existing shift on this device"
        )
    db.refresh(shift)
    return shift


def get_shift_or_404(db: Session, shift_id: int) -> Shift:
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    return shift


# ---------------------------------------------------------------- 归因计算


def _allocate_seq(db: Session) -> int:
    """分配全局递增的变更序号。

    单例计数器行在写事务内 UPDATE ... RETURNING 递增，行锁持有至提交：
    序号顺序 == 提交顺序，同步游标因此不会越过尚未可见的较早变更；
    序号与修订在同一事务写入，同时提交或同时回滚。
    """
    db.execute(
        pg_insert(ChangeCounter).values(id=1, last_seq=0).on_conflict_do_nothing()
    )
    return db.execute(
        update(ChangeCounter)
        .where(ChangeCounter.id == 1)
        .values(last_seq=ChangeCounter.last_seq + 1)
        .returning(ChangeCounter.last_seq)
    ).scalar_one()


def _current_event_views(
    db: Session, device_id: str, start: datetime, end: datetime
) -> list[EventView]:
    """设备当前生效事件（每个来源取最新版本）中与窗口 [start, end) 重叠的部分。"""
    stmt = (
        select(EventVersion)
        .join(
            EventSource,
            and_(
                EventVersion.device_id == EventSource.device_id,
                EventVersion.source_id == EventSource.source_id,
                EventVersion.version == EventSource.last_version,
            ),
        )
        .where(
            EventVersion.device_id == device_id,
            EventVersion.start_ts < end,
            EventVersion.end_ts > start,
        )
    )
    rows = db.execute(stmt).scalars().all()
    return [
        EventView(
            source_id=r.source_id,
            version=r.version,
            reason=r.reason,
            start=r.start_ts,
            end=r.end_ts,
        )
        for r in rows
    ]


def _latest_revision(db: Session, shift_id: int) -> ShiftResult | None:
    return (
        db.execute(
            select(ShiftResult)
            .where(ShiftResult.shift_id == shift_id)
            .order_by(ShiftResult.revision.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _compute(db: Session, shift: Shift) -> tuple[dict, list[dict]]:
    events = _current_event_views(db, shift.device_id, shift.start_ts, shift.end_ts)
    rulings = _active_ruling_views(db, shift.id)
    return attribution.attribute_with_rulings(
        shift.start_ts, shift.end_ts, events, rulings
    )


def _active_ruling_views(db: Session, shift_id: int) -> list[RulingView]:
    """班次当前生效（未撤销）的现场人工裁定；id 即生效顺序。"""
    rows = (
        db.execute(
            select(ManualAdjudication)
            .where(
                ManualAdjudication.shift_id == shift_id,
                ManualAdjudication.status == "active",
            )
            .order_by(ManualAdjudication.id)
        )
        .scalars()
        .all()
    )
    return [
        RulingView(
            ruling_no=r.ruling_no,
            reason=r.reason,
            start=r.start_ts,
            end=r.end_ts,
            seq=r.id,
        )
        for r in rows
    ]


def _new_revision(
    db: Session,
    shift: Shift,
    totals: dict,
    used: list[dict],
    trigger: str,
    base: ShiftResult | None,
) -> ShiftResult:
    if base is None:
        revision, bm, bf, bp = 1, 0, 0, 0
    else:
        revision = base.revision + 1
        bm, bf, bp = (
            base.maintenance_minutes,
            base.failure_minutes,
            base.pending_minutes,
        )
    rev = ShiftResult(
        shift_id=shift.id,
        revision=revision,
        seq=_allocate_seq(db),
        maintenance_minutes=totals["maintenance"],
        failure_minutes=totals["failure"],
        pending_minutes=totals["pending"],
        d_maintenance=totals["maintenance"] - bm,
        d_failure=totals["failure"] - bf,
        d_pending=totals["pending"] - bp,
        event_versions=used,
        trigger=trigger,
    )
    db.add(rev)
    return rev


# ---------------------------------------------------------------- 结算


def settle_shift(db: Session, shift_id: int) -> tuple[Shift, ShiftResult, bool]:
    """结算班次：计算并写入修订 1；已结算则幂等返回最新修订。"""
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    latest = _latest_revision(db, shift_id)
    if latest is not None:
        return shift, latest, False
    totals, used = _compute(db, shift)
    rev = _new_revision(db, shift, totals, used, trigger="settle", base=None)
    db.commit()
    db.refresh(rev)
    return shift, rev, True


# ---------------------------------------------------------------- 事件上报


def ingest_event(
    db: Session, payload: EventIn
) -> tuple[EventVersion, bool, list[tuple[Shift, ShiftResult]]]:
    """上报事件版本。

    返回 (事件版本, 是否幂等命中, 因更正而重算的 [(班次, 新修订)])。
    """
    get_device_or_404(db, payload.device_id)
    start, end = _utc(payload.start), _utc(payload.end)
    digest = _content_hash(start, end, payload.reason)

    # 确保版本链游标存在并加行锁，串行化同一来源的并发上报
    db.execute(
        pg_insert(EventSource)
        .values(
            device_id=payload.device_id, source_id=payload.source_id, last_version=0
        )
        .on_conflict_do_nothing()
    )
    src = db.execute(
        select(EventSource)
        .where(
            EventSource.device_id == payload.device_id,
            EventSource.source_id == payload.source_id,
        )
        .with_for_update()
    ).scalar_one()

    if payload.version <= src.last_version:
        existing = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == payload.version,
            )
        ).scalar_one()
        if existing.content_hash == digest:
            db.commit()
            return existing, True, []  # 同来源同版本同内容：幂等返回
        raise ApiError(
            409,
            "VERSION_CONFLICT",
            f"version {payload.version} of source {payload.source_id!r} "
            "already exists with different content",
        )
    if payload.version > src.last_version + 1:
        raise ApiError(
            409,
            "VERSION_GAP",
            f"source {payload.source_id!r} expects version "
            f"{src.last_version + 1}, got {payload.version}",
        )

    prev = None
    if src.last_version > 0:
        prev = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == src.last_version,
            )
        ).scalar_one()

    ev = EventVersion(
        device_id=payload.device_id,
        source_id=payload.source_id,
        version=payload.version,
        start_ts=start,
        end_ts=end,
        reason=payload.reason,
        content_hash=digest,
    )
    db.add(ev)
    src.last_version = payload.version
    src.last_content_hash = digest
    db.flush()

    intervals = [(start, end)]
    if prev is not None:
        intervals.append((prev.start_ts, prev.end_ts))
    recomputed = _recompute_affected_shifts(db, payload.device_id, intervals)
    db.commit()
    return ev, False, recomputed


def ingest_event_batch(
    db: Session, payload: EventBatchIn
) -> tuple[list[dict], list[tuple[Shift, ShiftResult]]]:
    """断网补传：同一设备的非空事件列表，整批核对、整批应用（全或无）。

    返回 (逐项回执 [{"source_id", "version", "idempotent"}], 重算的 [(班次, 新修订)])。

    - 按列表顺序核对每个来源的版本链：已存在的同版本同内容项为幂等重试；
      同版本异内容 / 跳号 / 未知设备 / 任一项非法 → 整批失败，
      不留下事件、修订或变更（单事务，失败即整体回滚）；
    - 全部核对通过后一次性应用所有新版本，仅以批前和批后各来源最新版本的
      区间并集确定受影响的已结算班次，再按最终状态重算；
      每班次最多产生一次修订和一条变更，分钟数未变则都不产生。
    """
    get_device_or_404(db, payload.device_id)

    # 涉及的来源行：不存在则补建；按 source_id 固定顺序加行锁，
    # 与单条上报及其他并发批次相互串行化，维持连续版本链
    source_ids = sorted({item.source_id for item in payload.events})
    for sid in source_ids:
        db.execute(
            pg_insert(EventSource)
            .values(device_id=payload.device_id, source_id=sid, last_version=0)
            .on_conflict_do_nothing()
        )
    sources = (
        db.execute(
            select(EventSource)
            .where(
                EventSource.device_id == payload.device_id,
                EventSource.source_id.in_(source_ids),
            )
            .order_by(EventSource.source_id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    src_by_id = {s.source_id: s for s in sources}
    expected = {s.source_id: s.last_version for s in sources}

    # 按列表顺序逐项核对；全部通过前不写入任何数据
    pending: dict[tuple[str, int], str] = {}  # 本批已接受新版本的 (来源, 版本) -> 内容哈希
    new_items: list[tuple] = []  # (item, start_utc, end_utc, digest)
    acks: list[dict] = []
    for item in payload.events:
        start, end = _utc(item.start), _utc(item.end)
        digest = _content_hash(start, end, item.reason)
        exp = expected[item.source_id]
        if item.version <= exp:
            # 已存在（库中或本批已接受）的同版本：同内容可重试，异内容整批失败
            existing = pending.get((item.source_id, item.version))
            if existing is None:
                existing = db.execute(
                    select(EventVersion.content_hash).where(
                        EventVersion.device_id == payload.device_id,
                        EventVersion.source_id == item.source_id,
                        EventVersion.version == item.version,
                    )
                ).scalar_one()
            if existing != digest:
                raise ApiError(
                    409,
                    "VERSION_CONFLICT",
                    f"version {item.version} of source {item.source_id!r} "
                    "already exists with different content; batch rejected",
                )
            acks.append(
                {
                    "source_id": item.source_id,
                    "version": item.version,
                    "idempotent": True,
                }
            )
        elif item.version == exp + 1:
            # 连续下一版本：接受为新版本，推进该来源的版本链
            expected[item.source_id] = item.version
            pending[(item.source_id, item.version)] = digest
            new_items.append((item, start, end, digest))
            acks.append(
                {
                    "source_id": item.source_id,
                    "version": item.version,
                    "idempotent": False,
                }
            )
        else:
            raise ApiError(
                409,
                "VERSION_GAP",
                f"source {item.source_id!r} expects version {exp + 1}, "
                f"got {item.version}; batch rejected",
            )

    recomputed: list[tuple[Shift, ShiftResult]] = []
    if new_items:
        # 应用全部新版本；同一来源取批内最后一个新版本推进版本链游标
        last_new: dict[str, tuple] = {}
        for item, start, end, digest in new_items:
            db.add(
                EventVersion(
                    device_id=payload.device_id,
                    source_id=item.source_id,
                    version=item.version,
                    start_ts=start,
                    end_ts=end,
                    reason=item.reason,
                    content_hash=digest,
                )
            )
            last_new[item.source_id] = (item, start, end, digest)

        # 受影响区间 = 各来源（批前最新版本区间 ∪ 批后最新版本区间）的并集
        intervals: list[tuple[datetime, datetime]] = []
        for sid, (item, start, end, digest) in last_new.items():
            src = src_by_id[sid]
            if src.last_version > 0:
                prev_start, prev_end = db.execute(
                    select(EventVersion.start_ts, EventVersion.end_ts).where(
                        EventVersion.device_id == payload.device_id,
                        EventVersion.source_id == sid,
                        EventVersion.version == src.last_version,
                    )
                ).one()
                intervals.append((prev_start, prev_end))
            intervals.append((start, end))
            src.last_version = item.version
            src.last_content_hash = digest
        db.flush()

        recomputed = _recompute_affected_shifts(db, payload.device_id, intervals)

    db.commit()
    return acks, recomputed


def _recompute_affected_shifts(
    db: Session,
    device_id: str,
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[Shift, ShiftResult]]:
    """迟到更正：按事件时间（相关来源批前/批后版本区间的并集）找出受影响的
    已结算班次，用全部来源的最新版本与当前生效裁定重算；结果有变化时追加
    新修订并记录差额。每个班次在此至多重算一次（至多一个新修订）。
    """
    if not intervals:
        return []
    shifts = (
        db.execute(
            select(Shift)
            .where(
                Shift.device_id == device_id,
                or_(
                    *[
                        and_(Shift.start_ts < end, Shift.end_ts > start)
                        for start, end in intervals
                    ]
                ),
            )
            .order_by(Shift.id)  # 固定加锁顺序，降低并发更正相互死锁的概率
            .with_for_update()
        )
        .scalars()
        .all()
    )

    recomputed: list[tuple[Shift, ShiftResult]] = []
    for shift in shifts:
        latest = _latest_revision(db, shift.id)
        if latest is None:
            continue  # 尚未结算：结算时自然会使用最新事件版本
        totals, used = _compute(db, shift)
        if (
            totals["maintenance"] == latest.maintenance_minutes
            and totals["failure"] == latest.failure_minutes
            and totals["pending"] == latest.pending_minutes
        ):
            continue  # 结果未变化，不产生空修订
        rev = _new_revision(db, shift, totals, used, trigger="correction", base=latest)
        db.flush()
        recomputed.append((shift, rev))
    return recomputed


# ---------------------------------------------------------------- 现场人工裁定


def _ruling_hash(start: datetime, end: datetime, reason: str, note: str) -> str:
    raw = f"{start.isoformat()}|{end.isoformat()}|{reason}|{note}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _get_locked_settled_shift(db: Session, shift_id: int) -> Shift:
    """取出班次行（FOR UPDATE 串行化该班次的所有写操作）并要求已结算。"""
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    if _latest_revision(db, shift_id) is None:
        raise ApiError(
            409,
            "SHIFT_NOT_SETTLED",
            f"shift id={shift_id} has not been settled; "
            "manual adjudication is only allowed after settlement",
        )
    return shift


def _recompute_after_ruling(
    db: Session, shift: Shift, trigger: str
) -> ShiftResult | None:
    """裁定/撤销后重算班次：仅当各原因分钟数变化时追加新修订并记录差额。"""
    latest = _latest_revision(db, shift.id)
    totals, used = _compute(db, shift)
    if (
        totals["maintenance"] == latest.maintenance_minutes
        and totals["failure"] == latest.failure_minutes
        and totals["pending"] == latest.pending_minutes
    ):
        return None  # 分钟数无变化：无修订、无变更，历史修订保持不变
    rev = _new_revision(db, shift, totals, used, trigger=trigger, base=latest)
    db.flush()
    return rev


def submit_adjudication(
    db: Session, shift_id: int, payload: AdjudicationIn
) -> tuple[Shift, ManualAdjudication, bool, ShiftResult | None, int]:
    """为已结算班次提交现场人工裁定。

    返回 (班次, 裁定, 是否幂等命中, 新修订或 None, 当前最新修订号)。
    - 同单号同内容重试：幂等返回，不产生修订；
    - 同单号异内容（含已撤销单号复用）：409 RULING_CONFLICT；
    - 区间越出班次 / 班次未结算 / 未知班次：分别明确报错。
    """
    shift = _get_locked_settled_shift(db, shift_id)
    start, end = _utc(payload.start), _utc(payload.end)
    if start < shift.start_ts or end > shift.end_ts:
        raise ApiError(
            422,
            "RULING_OUT_OF_RANGE",
            f"ruling [{start.isoformat()}, {end.isoformat()}) is not within "
            f"shift [{shift.start_ts.isoformat()}, {shift.end_ts.isoformat()})",
        )
    note = payload.note or ""
    digest = _ruling_hash(start, end, payload.reason, note)

    existing = db.execute(
        select(ManualAdjudication).where(
            ManualAdjudication.shift_id == shift_id,
            ManualAdjudication.ruling_no == payload.ruling_no,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content_hash == digest:
            db.commit()  # 同单号同内容（含已撤销后再提交同内容）：幂等
            return shift, existing, True, None, _latest_revision(db, shift_id).revision
        raise ApiError(
            409,
            "RULING_CONFLICT",
            f"ruling_no {payload.ruling_no!r} already exists on shift "
            f"{shift_id} with different content",
        )

    ruling = ManualAdjudication(
        shift_id=shift_id,
        ruling_no=payload.ruling_no,
        start_ts=start,
        end_ts=end,
        reason=payload.reason,
        note=note,
        content_hash=digest,
        status="active",
    )
    db.add(ruling)
    try:
        db.flush()  # 班次行锁内插入，id 顺序即裁定生效顺序；唯一约束兜底并发竞争
    except IntegrityError:
        db.rollback()
        # 并发同单号提交：重新判定幂等 / 冲突
        shift2 = _get_locked_settled_shift(db, shift_id)
        other = db.execute(
            select(ManualAdjudication).where(
                ManualAdjudication.shift_id == shift_id,
                ManualAdjudication.ruling_no == payload.ruling_no,
            )
        ).scalar_one()
        if other.content_hash != digest:
            raise ApiError(
                409,
                "RULING_CONFLICT",
                f"ruling_no {payload.ruling_no!r} already exists on shift "
                f"{shift_id} with different content",
            )
        return (
            shift2,
            other,
            True,
            None,
            _latest_revision(db, shift_id).revision,
        )

    rev = _recompute_after_ruling(db, shift, trigger="adjudication")
    db.commit()
    db.refresh(ruling)
    latest_rev = _latest_revision(db, shift_id)
    return shift, ruling, False, rev, latest_rev.revision


def revoke_adjudication(
    db: Session, shift_id: int, ruling_no: str
) -> tuple[Shift, ManualAdjudication, bool, ShiftResult | None, int]:
    """按单号撤销班次的现场人工裁定。

    返回 (班次, 裁定, 是否幂等命中, 新修订或 None, 当前最新修订号)。
    - 已撤销再撤销：幂等返回，不产生修订；
    - 未知班次 / 未知单号：分别 404；班次未结算不允许出现裁定，同样明确拒绝。
    撤销生效后重叠分钟回退到此前仍有效的裁定；无裁定时按当前最新事件版本计算。
    """
    shift = _get_locked_settled_shift(db, shift_id)
    ruling = db.execute(
        select(ManualAdjudication).where(
            ManualAdjudication.shift_id == shift_id,
            ManualAdjudication.ruling_no == ruling_no,
        )
    ).scalar_one_or_none()
    if ruling is None:
        raise ApiError(
            404,
            "RULING_NOT_FOUND",
            f"unknown ruling_no {ruling_no!r} on shift {shift_id}",
        )
    if ruling.status == "revoked":
        db.commit()  # 重复撤销幂等
        return shift, ruling, True, None, _latest_revision(db, shift_id).revision

    ruling.status = "revoked"
    ruling.revoked_at = datetime.now(timezone.utc)
    db.flush()
    rev = _recompute_after_ruling(db, shift, trigger="adjudication_revoke")
    db.commit()
    db.refresh(ruling)
    latest_rev = _latest_revision(db, shift_id)
    return shift, ruling, False, rev, latest_rev.revision


def list_adjudications(db: Session, shift_id: int) -> tuple[Shift, list]:
    """列出班次的全部现场人工裁定（含已撤销），按生效顺序排列。"""
    shift = get_shift_or_404(db, shift_id)
    rows = (
        db.execute(
            select(ManualAdjudication)
            .where(ManualAdjudication.shift_id == shift_id)
            .order_by(ManualAdjudication.id)
        )
        .scalars()
        .all()
    )
    return shift, list(rows)


# ---------------------------------------------------------------- 结果查询


def get_result(
    db: Session, shift_id: int, revision: int | None
) -> tuple[Shift, ShiftResult, int]:
    """返回 (班次, 指定/最新修订, 最新修订号)。"""
    shift = get_shift_or_404(db, shift_id)
    latest = _latest_revision(db, shift_id)
    if latest is None:
        raise ApiError(
            404, "RESULT_NOT_FOUND", f"shift id={shift_id} has not been settled"
        )
    if revision is None:
        return shift, latest, latest.revision
    rev = db.execute(
        select(ShiftResult).where(
            ShiftResult.shift_id == shift_id, ShiftResult.revision == revision
        )
    ).scalar_one_or_none()
    if rev is None:
        raise ApiError(
            404, "REVISION_NOT_FOUND", f"shift id={shift_id} has no revision {revision}"
        )
    return shift, rev, latest.revision


def list_revisions(db: Session, shift_id: int) -> tuple[Shift, list[ShiftResult]]:
    shift = get_shift_or_404(db, shift_id)
    revs = (
        db.execute(
            select(ShiftResult)
            .where(ShiftResult.shift_id == shift_id)
            .order_by(ShiftResult.revision)
        )
        .scalars()
        .all()
    )
    return shift, list(revs)


# ---------------------------------------------------------------- 变更同步

#: GET /changes 允许的页大小上限
MAX_PAGE_SIZE = 500


def list_changes(
    db: Session, cursor: int, limit: int
) -> tuple[list[tuple[ShiftResult, Shift]], int]:
    """按全局递增游标分页读取结算/更正变更，返回 ([(修订, 班次)], 下一游标)。

    - 返回 seq > cursor 的变更，按 seq 升序，最多 limit 条；
    - 空页保持原游标（客户端已追平，可原地轮询）；
    - 游标超过当前最大序号视为非法（客户端进度与服务端不一致），
      明确拒绝且不影响任何同步进度。
    """
    if cursor > 0:
        max_seq = db.execute(select(func.max(ShiftResult.seq))).scalar() or 0
        if cursor > max_seq:
            raise ApiError(
                422,
                "INVALID_CURSOR",
                f"cursor {cursor} is beyond the latest change seq {max_seq}; "
                "restart sync from a smaller cursor (0 replays from the beginning)",
            )
    rows = db.execute(
        select(ShiftResult, Shift)
        .join(Shift, Shift.id == ShiftResult.shift_id)
        .where(ShiftResult.seq > cursor)
        .order_by(ShiftResult.seq)
        .limit(limit)
    ).all()
    page = [(rev, shift) for rev, shift in rows]
    next_cursor = page[-1][0].seq if page else cursor
    return page, next_cursor


# ---------------------------------------------------------------- 班次归因快照

#: GET /snapshots/shifts 允许的页大小上限
SNAPSHOT_MAX_PAGE_SIZE = 500

#: 续页游标载荷格式版本
_CURSOR_VERSION = 1


def _encode_snapshot_cursor(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    # 附短摘要防止客户端伪造/篡改游标
    tag = hashlib.sha256(raw).digest()[:8]
    return base64.urlsafe_b64encode(raw + b"#" + tag).decode("ascii")


def _decode_snapshot_cursor(cursor: str) -> dict:
    """解码并校验续页游标；结构非法或被篡改一律按 INVALID_SNAPSHOT_CURSOR 拒绝。"""
    invalid = ApiError(
        422,
        "INVALID_SNAPSHOT_CURSOR",
        "malformed or tampered snapshot cursor; restart the snapshot from page 1",
    )
    try:
        raw_b64 = cursor.encode("ascii")
        if any(ch not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=" for ch in raw_b64):
            raise ValueError
        blob = base64.urlsafe_b64decode(raw_b64)
        raw, tag = blob.rsplit(b"#", 1)
        if hashlib.sha256(raw).digest()[:8] != tag:
            raise ValueError
        token = json.loads(raw)
    except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        raise invalid
    required = {"v", "device_id", "start", "end", "seq", "after_shift_id"}
    if not isinstance(token, dict) or set(token) != required:
        raise invalid
    if token["v"] != _CURSOR_VERSION or not isinstance(token["device_id"], str):
        raise invalid
    if isinstance(token["seq"], bool) or not isinstance(token["seq"], int) or token["seq"] < 1:
        raise invalid
    if (
        isinstance(token["after_shift_id"], bool)
        or not isinstance(token["after_shift_id"], int)
        or token["after_shift_id"] < 0
    ):
        raise invalid
    try:
        ts_start = datetime.fromisoformat(token["start"])
        ts_end = datetime.fromisoformat(token["end"])
    except ValueError:
        raise invalid
    if ts_start.tzinfo is None or ts_end.tzinfo is None:
        raise invalid
    return token


def get_shift_snapshot(
    db: Session,
    *,
    device_id: str,
    start: datetime,
    end: datetime,
    seq: int | None,
    cursor: str | None,
    limit: int,
) -> tuple[int, list[tuple[Shift, ShiftResult]], str | None]:
    """按固定变更序号读取班次归因快照（首次接入/重建索引），稳定按键翻页。

    返回 (快照序号, [(班次, 该序号下最后修订)], 续页游标)。

    - 只列与半开窗口 [start, end) 相交、截至快照序号已结算（存在 seq <= 快照序号
      的修订）的班次；每班次取序号不大于快照序号的最后修订；
    - 序号未指定时，以"首次请求读取时"已提交的最大变更序号为准（每页回传该序号）；
    - 续页游标绑定设备/窗口/快照序号与已翻过的最后班次 id：与筛选条件或序号不匹配
      时明确报错；修订只追加不修改，同一快照后续页不受新结算/更正/裁定影响；
    - 指定序号超过当前已提交最大序号时 422 SNAPSHOT_SEQ_OUT_OF_RANGE；
    - 空快照（含快照序号 0）返回空页，next_cursor 为 null。
    """
    start_u, end_u = _utc(start), _utc(end)
    start_iso, end_iso = start_u.isoformat(), end_u.isoformat()
    after_shift_id = 0

    if cursor is not None:
        token = _decode_snapshot_cursor(cursor)
        if (
            token["device_id"] != device_id
            or token["start"] != start_iso
            or token["end"] != end_iso
            or (seq is not None and seq != token["seq"])
        ):
            raise ApiError(
                422,
                "INVALID_SNAPSHOT_CURSOR",
                "snapshot cursor does not match the requested device, time window "
                "or snapshot seq; restart the snapshot from page 1",
            )
        snapshot_seq = token["seq"]
        after_shift_id = token["after_shift_id"]

    get_device_or_404(db, device_id)

    if cursor is None:
        max_seq = db.execute(select(func.max(ShiftResult.seq))).scalar() or 0
        if seq is not None:
            if seq > max_seq:
                raise ApiError(
                    422,
                    "SNAPSHOT_SEQ_OUT_OF_RANGE",
                    f"snapshot seq {seq} is beyond the latest committed seq "
                    f"{max_seq}; omit seq to snapshot the current maximum",
                )
            snapshot_seq = seq
        else:
            snapshot_seq = max_seq

    # 每班次取 seq <= 快照序号的最后修订（revision 在同班次内随 seq 单调递增）
    last_rev = (
        select(
            ShiftResult.shift_id.label("shift_id"),
            func.max(ShiftResult.revision).label("rev_no"),
        )
        .where(ShiftResult.seq <= snapshot_seq)
        .group_by(ShiftResult.shift_id)
    ).subquery()
    rows = db.execute(
        select(Shift, ShiftResult)
        .join(Shift, Shift.id == ShiftResult.shift_id)
        .join(
            last_rev,
            and_(
                ShiftResult.shift_id == last_rev.c.shift_id,
                ShiftResult.revision == last_rev.c.rev_no,
            ),
        )
        .where(
            Shift.device_id == device_id,
            Shift.start_ts < end_u,  # 半开区间相交
            Shift.end_ts > start_u,
            Shift.id > after_shift_id,  # 按键（班次 id）稳定翻页
        )
        .order_by(Shift.id)
        .limit(limit + 1)
    ).all()
    page = [(shift, rev) for shift, rev in rows[:limit]]

    next_cursor: str | None = None
    if len(rows) > limit and page:
        last_shift_id = page[-1][0].id
        next_cursor = _encode_snapshot_cursor(
            {
                "v": _CURSOR_VERSION,
                "device_id": device_id,
                "start": start_iso,
                "end": end_iso,
                "seq": snapshot_seq,
                "after_shift_id": last_shift_id,
            }
        )
    return snapshot_seq, page, next_cursor
