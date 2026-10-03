"""端到端 API 测试：需要可用的 PostgreSQL（在 Compose 环境内运行）。

运行方式：docker compose exec app pytest tests/test_api.py
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}
SHIFT = {
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}
E_MAINT = {  # 保养 10:00-11:00
    "device_id": "CNC-01",
    "source_id": "MES-1",
    "version": 1,
    "start": "2026-10-01T10:00:00+08:00",
    "end": "2026-10-01T11:00:00+08:00",
    "reason": "maintenance",
}
E_FAIL = {  # 故障 10:30-12:00
    "device_id": "CNC-01",
    "source_id": "PLC-9",
    "version": 1,
    "start": "2026-10-01T10:30:00+08:00",
    "end": "2026-10-01T12:00:00+08:00",
    "reason": "failure",
}


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _make_shift(client) -> int:
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=SHIFT)
    assert r.status_code == 201
    return r.json()["id"]


# ---------------------------------------------------------------- 校验类


def test_unknown_device_shift_rejected(client):
    r = client.post("/shifts", json=SHIFT)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_unknown_device_event_rejected(client):
    r = client.post("/events", json=E_MAINT)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_invalid_intervals_rejected(client):
    client.post("/devices", json=DEVICE)
    bad = [
        {**SHIFT, "start": SHIFT["end"]},  # start == end
        {**SHIFT, "start": "2026-10-01T17:00:00+08:00"},  # start > end
        {**SHIFT, "start": "2026-10-01T08:00:30+08:00"},  # 未对齐整分钟
        {**SHIFT, "start": "2026-10-01T08:00:00"},  # 缺时区
    ]
    for payload in bad:
        r = client.post("/shifts", json=payload)
        assert r.status_code == 422, payload
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR"
    # 事件同样校验
    r = client.post("/events", json={**E_MAINT, "end": "2026-10-01T10:00:45+08:00"})
    assert r.status_code == 422
    r = client.post("/events", json={**E_MAINT, "reason": "unknown"})
    assert r.status_code == 422


def test_shift_overlap_rejected_but_adjacent_allowed(client):
    _make_shift(client)
    # 重叠 → 409
    r = client.post(
        "/shifts",
        json={**SHIFT, "start": "2026-10-01T15:00:00+08:00", "end": "2026-10-01T20:00:00+08:00"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SHIFT_OVERLAP"
    # 半开区间：端点相接不算重叠
    r = client.post(
        "/shifts",
        json={**SHIFT, "start": "2026-10-01T16:00:00+08:00", "end": "2026-10-01T23:00:00+08:00"},
    )
    assert r.status_code == 201
    # 其他设备不受限
    client.post("/devices", json={"id": "CNC-02", "name": "二号机"})
    r = client.post("/shifts", json={**SHIFT, "device_id": "CNC-02"})
    assert r.status_code == 201


# ---------------------------------------------------------------- 版本链与幂等


def test_event_version_chain(client):
    _make_shift(client)
    # 首版本必须从 1 开始
    r = client.post("/events", json={**E_MAINT, "version": 2})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_GAP"

    assert client.post("/events", json=E_MAINT).status_code == 201

    # 同来源同版本同内容 → 幂等
    r = client.post("/events", json=E_MAINT)
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["recomputed"] == []

    # 同版本异内容 → 409
    r = client.post("/events", json={**E_MAINT, "end": "2026-10-01T11:30:00+08:00"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_CONFLICT"

    # 跳号 → 409
    r = client.post("/events", json={**E_MAINT, "version": 3})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_GAP"

    # 正确的下一版本 → 201
    r = client.post("/events", json={**E_MAINT, "version": 2, "end": "2026-10-01T11:30:00+08:00"})
    assert r.status_code == 201
    assert r.json()["idempotent"] is False


# ---------------------------------------------------------------- 结算 / 更正 / 修订


def test_settle_and_late_correction_flow(client):
    shift_id = _make_shift(client)
    assert client.post("/events", json=E_MAINT).status_code == 201
    assert client.post("/events", json=E_FAIL).status_code == 201

    # 结算：保养 60（10:00-11:00），故障 60（11:00-12:00，10:30-11:00 被保养覆盖）
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.status_code == 200
    res = r.json()["result"]
    assert r.json()["settled"] is True
    assert res["revision"] == 1
    assert res["totals"] == {"maintenance": 60, "failure": 60, "pending": 0}
    assert res["delta"] == {"maintenance": 60, "failure": 60, "pending": 0}
    assert {u["source_id"] for u in res["event_versions"]} == {"MES-1", "PLC-9"}

    # 重复结算 → 幂等返回最新修订
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.json()["settled"] is False
    assert r.json()["result"]["revision"] == 1

    # 迟到更正：故障改为 10:30-11:30（v2）→ 故障只剩 11:00-11:30 的 30 分钟
    r = client.post("/events", json={**E_FAIL, "version": 2, "end": "2026-10-01T11:30:00+08:00"})
    assert r.status_code == 201
    rec = r.json()["recomputed"]
    assert len(rec) == 1
    assert rec[0]["shift_id"] == shift_id
    assert rec[0]["revision"] == 2
    assert rec[0]["totals"] == {"maintenance": 60, "failure": 30, "pending": 0}
    assert rec[0]["delta"] == {"maintenance": 0, "failure": -30, "pending": 0}

    # 最新结果 = 修订 2；依据版本更新为 PLC-9 v2
    r = client.get(f"/shifts/{shift_id}/result")
    body = r.json()
    assert body["revision"] == 2
    assert body["latest_revision"] == 2
    assert body["totals"]["failure"] == 30
    versions = {u["source_id"]: u["version"] for u in body["event_versions"]}
    assert versions == {"MES-1": 1, "PLC-9": 2}

    # 指定修订 1 → 原结果保留
    r = client.get(f"/shifts/{shift_id}/result", params={"revision": 1})
    body = r.json()
    assert body["revision"] == 1
    assert body["totals"]["failure"] == 60
    assert body["latest_revision"] == 2

    # 不存在的修订 → 404
    r = client.get(f"/shifts/{shift_id}/result", params={"revision": 99})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "REVISION_NOT_FOUND"

    # 修订列表
    r = client.get(f"/shifts/{shift_id}/revisions")
    assert [x["revision"] for x in r.json()] == [1, 2]
    assert r.json()[1]["trigger"] == "correction"


def test_correction_without_change_creates_no_revision(client):
    shift_id = _make_shift(client)
    client.post("/events", json=E_MAINT)
    client.post(f"/shifts/{shift_id}/settle")
    # 同内容但递增版本号：作为新版本接受，但归因结果不变 → 不产生空修订
    r = client.post("/events", json={**E_MAINT, "version": 2})
    assert r.status_code == 201
    assert r.json()["recomputed"] == []
    r = client.get(f"/shifts/{shift_id}/revisions")
    assert [x["revision"] for x in r.json()] == [1]


def test_pending_priority_and_result_query(client):
    shift_id = _make_shift(client)
    # 待判 09:00-10:30；故障 10:00-11:00 覆盖其后半段
    p = {
        "device_id": "CNC-01",
        "source_id": "OP-1",
        "version": 1,
        "start": "2026-10-01T09:00:00+08:00",
        "end": "2026-10-01T10:30:00+08:00",
        "reason": "pending",
    }
    f = {
        "device_id": "CNC-01",
        "source_id": "PLC-9",
        "version": 1,
        "start": "2026-10-01T10:00:00+08:00",
        "end": "2026-10-01T11:00:00+08:00",
        "reason": "failure",
    }
    client.post("/events", json=p)
    client.post("/events", json=f)
    r = client.post(f"/shifts/{shift_id}/settle")
    totals = r.json()["result"]["totals"]
    assert totals == {"maintenance": 0, "failure": 60, "pending": 60}


def test_unsettled_shift_result_404(client):
    shift_id = _make_shift(client)
    r = client.get(f"/shifts/{shift_id}/result")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RESULT_NOT_FOUND"
    r = client.get("/shifts/99999/result")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SHIFT_NOT_FOUND"
