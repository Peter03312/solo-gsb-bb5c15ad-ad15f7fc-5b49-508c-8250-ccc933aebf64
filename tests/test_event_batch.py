"""断网补传（POST /events/batch）端到端测试：需要可用的 PostgreSQL（在 Compose 环境内运行）。

运行方式：docker compose exec app pytest tests/test_event_batch.py
"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "manual_adjudications",
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}
SHIFT = {  # 早班 08:00-16:00
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}
SHIFT_B = {  # 晚班 16:00-24:00（端点相接，不重叠）
    "device_id": "CNC-01",
    "start": "2026-10-01T16:00:00+08:00",
    "end": "2026-10-02T00:00:00+08:00",
}


def ev(source, version, start, end, reason):
    return {
        "source_id": source,
        "version": version,
        "start": start,
        "end": end,
        "reason": reason,
    }


# 保养 10:00-11:00（MES-1 v1）
E_MAINT_V1 = ev("MES-1", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "maintenance")


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _make_shift(client, shift=SHIFT) -> int:
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=shift)
    assert r.status_code == 201
    return r.json()["id"]


def _batch(client, device_id, events):
    return client.post("/events/batch", json={"device_id": device_id, "events": events})


def _changes(client):
    return client.get("/changes", params={"cursor": 0, "limit": 500}).json()


# ---------------------------------------------------------------- 基本流程


def test_batch_ingest_and_recompute(client):
    shift_id = _make_shift(client)
    # 单条上报保养 10:00-11:00 并结算
    assert client.post("/events", json={"device_id": "CNC-01", **E_MAINT_V1}).status_code == 201
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.json()["result"]["totals"] == {"maintenance": 60, "failure": 0, "pending": 0}

    # 断网恢复补传：MES-1 v2（保养延长到 11:30）、PLC-9 v1（故障 11:00-12:00）、
    # 以及断网期间重复缓存的 MES-1 v1（已存在同内容 → 幂等项）
    r = _batch(
        client,
        "CNC-01",
        [
            ev("MES-1", 2, "2026-10-01T10:00:00+08:00", "2026-10-01T11:30:00+08:00", "maintenance"),
            ev("PLC-9", 1, "2026-10-01T11:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure"),
            E_MAINT_V1,
        ],
    )
    assert r.status_code == 201
    body = r.json()
    assert body["device_id"] == "CNC-01"
    assert [(i["source_id"], i["version"], i["idempotent"]) for i in body["items"]] == [
        ("MES-1", 2, False),
        ("PLC-9", 1, False),
        ("MES-1", 1, True),
    ]
    # 最终状态：保养 10:00-11:30 = 90（11:00-11:30 保养优先），故障 11:30-12:00 = 30
    assert len(body["recomputed"]) == 1
    rec = body["recomputed"][0]
    assert rec["shift_id"] == shift_id and rec["revision"] == 2
    assert rec["totals"] == {"maintenance": 90, "failure": 30, "pending": 0}
    assert rec["delta"] == {"maintenance": 30, "failure": 30, "pending": 0}

    # 班次只追加了一个修订；变更流新增一条 correction
    revs = client.get(f"/shifts/{shift_id}/revisions").json()
    assert [x["revision"] for x in revs] == [1, 2]
    changes = _changes(client)["changes"]
    assert [c["seq"] for c in changes] == [1, 2]
    assert changes[-1]["trigger"] == "correction"
    assert changes[-1]["revision"] == 2

    # 整批原样重发 → 全部幂等，整体 200，无新修订、无新变更
    r = _batch(
        client,
        "CNC-01",
        [
            ev("MES-1", 2, "2026-10-01T10:00:00+08:00", "2026-10-01T11:30:00+08:00", "maintenance"),
            ev("PLC-9", 1, "2026-10-01T11:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure"),
            E_MAINT_V1,
        ],
    )
    assert r.status_code == 200
    assert all(i["idempotent"] for i in r.json()["items"])
    assert r.json()["recomputed"] == []
    assert len(client.get(f"/shifts/{shift_id}/revisions").json()) == 2
    assert len(_changes(client)["changes"]) == 2

    # 版本链继续：单条接口可接着上报 MES-1 v3
    r = client.post(
        "/events",
        json={
            "device_id": "CNC-01",
            **ev("MES-1", 3, "2026-10-01T10:00:00+08:00", "2026-10-01T11:30:00+08:00", "maintenance"),
        },
    )
    assert r.status_code == 201
    assert r.json()["recomputed"] == []  # 内容未变，不产生修订


def test_batch_multiple_new_versions_one_revision_per_shift(client):
    """同一来源批内连续多个新版本：以最终状态重算，每班次只产生一次修订。"""
    shift_id = _make_shift(client)
    assert client.post("/events", json={"device_id": "CNC-01", **E_MAINT_V1}).status_code == 201
    client.post(f"/shifts/{shift_id}/settle")  # 保养 60

    r = _batch(
        client,
        "CNC-01",
        [
            ev("MES-1", 2, "2026-10-01T10:00:00+08:00", "2026-10-01T12:00:00+08:00", "maintenance"),
            ev("MES-1", 3, "2026-10-01T10:00:00+08:00", "2026-10-01T11:30:00+08:00", "maintenance"),
        ],
    )
    assert r.status_code == 201
    assert [i["idempotent"] for i in r.json()["items"]] == [False, False]
    # 按最终版本 v3（10:00-11:30 = 90）一次性重算：只有修订 2，没有中间态修订
    rec = r.json()["recomputed"]
    assert len(rec) == 1
    assert rec[0]["revision"] == 2
    assert rec[0]["totals"]["maintenance"] == 90
    assert rec[0]["delta"]["maintenance"] == 30
    assert [x["revision"] for x in client.get(f"/shifts/{shift_id}/revisions").json()] == [1, 2]
    assert len(_changes(client)["changes"]) == 2  # settle + 一次 correction


def test_batch_no_revision_when_minutes_unchanged(client):
    """批次全部为新版本但最终分钟数未变：接受版本但不产生修订与变更。"""
    shift_id = _make_shift(client)
    assert client.post("/events", json={"device_id": "CNC-01", **E_MAINT_V1}).status_code == 201
    client.post(f"/shifts/{shift_id}/settle")

    r = _batch(client, "CNC-01", [dict(E_MAINT_V1, version=2)])  # 同内容递增版本
    assert r.status_code == 201
    assert r.json()["items"][0]["idempotent"] is False
    assert r.json()["recomputed"] == []
    assert [x["revision"] for x in client.get(f"/shifts/{shift_id}/revisions").json()] == [1]
    assert len(_changes(client)["changes"]) == 1


def test_batch_affected_shifts_use_pre_post_union(client):
    """受影响班次 = 各来源（批前最新 ∪ 批后最新）区间并集；跨班次来源两侧都重算。"""
    assert client.post("/devices", json=DEVICE).status_code == 201
    ids = []
    for s in (SHIFT, SHIFT_B):
        r = client.post("/shifts", json=s)
        assert r.status_code == 201
        ids.append(r.json()["id"])
    shift_a, shift_b = ids
    # 故障 10:00-11:00 落在早班；两班各自结算
    assert client.post(
        "/events",
        json={"device_id": "CNC-01", **ev("PLC-9", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "failure")},
    ).status_code == 201
    client.post(f"/shifts/{shift_a}/settle")  # failure 60
    client.post(f"/shifts/{shift_b}/settle")  # 0

    # 补传 v2：改到 18:00-19:00（晚班）。批前区间（早班）∪ 批后区间（晚班）→ 两班都重算
    r = _batch(
        client,
        "CNC-01",
        [ev("PLC-9", 2, "2026-10-01T18:00:00+08:00", "2026-10-01T19:00:00+08:00", "failure")],
    )
    assert r.status_code == 201
    rec = {x["shift_id"]: x for x in r.json()["recomputed"]}
    assert set(rec) == {shift_a, shift_b}
    assert rec[shift_a]["totals"]["failure"] == 0
    assert rec[shift_a]["delta"]["failure"] == -60
    assert rec[shift_b]["totals"]["failure"] == 60
    assert rec[shift_b]["delta"]["failure"] == 60


def test_batch_respects_active_adjudications(client):
    """批次重算遵守生效中的现场裁定：裁定分钟不被事件改动。"""
    shift_id = _make_shift(client)
    assert client.post(
        "/events",
        json={"device_id": "CNC-01", **ev("PLC-9", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "failure")},
    ).status_code == 201
    client.post(f"/shifts/{shift_id}/settle")  # failure 60
    # 裁定 10:00-10:30 为保养
    r = client.post(
        f"/shifts/{shift_id}/adjudications",
        json={
            "ruling_no": "R-001",
            "start": "2026-10-01T10:00:00+08:00",
            "end": "2026-10-01T10:30:00+08:00",
            "reason": "maintenance",
            "note": "现场确认为计划保养",
        },
    )
    assert r.status_code == 201

    # 补传 v2：故障延长到 12:00 → 裁定分钟仍归保养，其余按事件
    r = _batch(
        client,
        "CNC-01",
        [ev("PLC-9", 2, "2026-10-01T10:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure")],
    )
    assert r.status_code == 201
    rec = r.json()["recomputed"]
    assert len(rec) == 1
    assert rec[0]["totals"] == {"maintenance": 30, "failure": 90, "pending": 0}
    assert rec[0]["delta"] == {"maintenance": 0, "failure": 60, "pending": 0}


# ---------------------------------------------------------------- 整批失败（全或无）


def test_batch_conflict_rejects_whole_batch(client):
    """任一项同版本异内容 → 整批 409，不留下事件、修订或变更。"""
    shift_id = _make_shift(client)
    assert client.post("/events", json={"device_id": "CNC-01", **E_MAINT_V1}).status_code == 201
    client.post(f"/shifts/{shift_id}/settle")

    # 批次中 PLC-9 v1 本身合法，但 MES-1 v1 内容不同 → 整批失败
    r = _batch(
        client,
        "CNC-01",
        [
            ev("PLC-9", 1, "2026-10-01T11:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure"),
            ev("MES-1", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:30:00+08:00", "maintenance"),
        ],
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_CONFLICT"

    # 不留痕迹：PLC-9 未落库（可重新作为 v1 上报）、无新修订、无新变更
    r = client.post(
        "/events",
        json={"device_id": "CNC-01", **ev("PLC-9", 1, "2026-10-01T11:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure")},
    )
    assert r.status_code == 201  # 若批次留下过 PLC-9 v1，这里会是幂等 200 或冲突
    assert [x["revision"] for x in client.get(f"/shifts/{shift_id}/revisions").json()] == [1, 2]
    changes = _changes(client)["changes"]
    assert [c["trigger"] for c in changes] == ["settle", "correction"]  # 批次未产生变更


def test_batch_gap_rejects_whole_batch(client):
    """跳号 → 整批 409，版本链不推进。"""
    _make_shift(client)
    assert client.post("/events", json={"device_id": "CNC-01", **E_MAINT_V1}).status_code == 201

    r = _batch(
        client,
        "CNC-01",
        [
            ev("MES-1", 3, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "maintenance"),
            ev("PLC-9", 1, "2026-10-01T11:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure"),
        ],
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_GAP"

    # 版本链未推进：MES-1 v2 仍可正常上报；PLC-9 未落库
    r = client.post(
        "/events",
        json={"device_id": "CNC-01", **ev("MES-1", 2, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "maintenance")},
    )
    assert r.status_code == 201
    r = client.post(
        "/events",
        json={"device_id": "CNC-01", **ev("PLC-9", 1, "2026-10-01T11:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure")},
    )
    assert r.status_code == 201


def test_batch_internal_gap_and_duplicate_rejected(client):
    """批内顺序核对：先 v3 后 v2 视为跳号；批内同版本异内容视为冲突。"""
    _make_shift(client)
    r = _batch(
        client,
        "CNC-01",
        [
            ev("MES-1", 3, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "maintenance"),
            ev("MES-1", 2, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "maintenance"),
        ],
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_GAP"

    r = _batch(
        client,
        "CNC-01",
        [
            ev("MES-1", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "maintenance"),
            ev("MES-1", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T12:00:00+08:00", "maintenance"),
        ],
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_CONFLICT"

    # 批内同版本同内容重复：第二项幂等，整批接受
    r = _batch(
        client,
        "CNC-01",
        [E_MAINT_V1, E_MAINT_V1],
    )
    assert r.status_code == 201
    assert [i["idempotent"] for i in r.json()["items"]] == [False, True]


def test_batch_unknown_device_rejected(client):
    r = _batch(client, "NOPE", [E_MAINT_V1])
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_batch_validation_errors(client):
    _make_shift(client)
    # 空列表
    r = _batch(client, "CNC-01", [])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"
    # 任一项非法：缺时区 / 秒非 00 / 原因非法 / start>=end / version<1
    bad_items = [
        {**E_MAINT_V1, "start": "2026-10-01T10:00:00"},
        {**E_MAINT_V1, "start": "2026-10-01T10:00:30+08:00"},
        {**E_MAINT_V1, "reason": "unknown"},
        {**E_MAINT_V1, "end": E_MAINT_V1["start"]},
        {**E_MAINT_V1, "version": 0},
    ]
    for item in bad_items:
        r = _batch(client, "CNC-01", [item])
        assert r.status_code == 422, item
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR"
    # 校验失败同样不留痕迹：合法 v1 仍可上报
    assert client.post("/events", json={"device_id": "CNC-01", **E_MAINT_V1}).status_code == 201


# ---------------------------------------------------------------- 并发一致性


def test_concurrent_identical_batches_stay_consistent(client):
    """并发提交相同批次：行锁串行化，恰好一个创建、其余幂等，版本链连续。"""
    _make_shift(client)
    assert client.post("/events", json={"device_id": "CNC-01", **E_MAINT_V1}).status_code == 201
    payload = {
        "device_id": "CNC-01",
        "events": [
            ev("MES-1", 2, "2026-10-01T10:00:00+08:00", "2026-10-01T11:30:00+08:00", "maintenance"),
            ev("PLC-9", 1, "2026-10-01T11:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure"),
        ],
    }

    barrier = threading.Barrier(4)
    results: list = []
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait(timeout=10)
            r = client.post("/events/batch", json=payload)
            assert r.status_code in (200, 201), r.text
            results.append(r)
        except Exception as exc:  # noqa: BLE001 - 汇集到主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sum(1 for r in results if r.status_code == 201) == 1  # 恰好一个实际创建
    assert sum(1 for r in results if r.status_code == 200) == 3  # 其余整体幂等

    # 版本链连续：v3 可继续；v2 重复提交幂等
    r = client.post(
        "/events",
        json={"device_id": "CNC-01", **ev("MES-1", 3, "2026-10-01T10:00:00+08:00", "2026-10-01T11:30:00+08:00", "maintenance")},
    )
    assert r.status_code == 201


def test_concurrent_batch_and_single_keep_chain_contiguous(client):
    """并发批次与单条上报：同一来源串行化，连续版本链无缺口、无重复。"""
    _make_shift(client)
    assert client.post("/events", json={"device_id": "CNC-01", **E_MAINT_V1}).status_code == 201
    single = {
        "device_id": "CNC-01",
        **ev("MES-1", 2, "2026-10-01T10:00:00+08:00", "2026-10-01T11:30:00+08:00", "maintenance"),
    }
    batch = {"device_id": "CNC-01", "events": [dict(E_MAINT_V1, version=2, end="2026-10-01T11:30:00+08:00")]}

    barrier = threading.Barrier(2)
    results: dict = {}
    errors: list[Exception] = []

    def post_single():
        try:
            barrier.wait(timeout=10)
            results["single"] = client.post("/events", json=single)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def post_batch():
        try:
            barrier.wait(timeout=10)
            results["batch"] = client.post("/events/batch", json=batch)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1, t2 = threading.Thread(target=post_single), threading.Thread(target=post_batch)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors
    # 同内容同版本：一个创建（201）、一个幂等（200），版本链恰好推进到 2
    assert sorted(r.status_code for r in results.values()) == [200, 201]
    r = client.post(
        "/events",
        json={"device_id": "CNC-01", **ev("MES-1", 3, "2026-10-01T10:00:00+08:00", "2026-10-01T11:30:00+08:00", "maintenance")},
    )
    assert r.status_code == 201  # 链上无缺口、无重复，v3 顺利衔接
