"""变更同步（GET /changes）端到端测试：需要可用的 PostgreSQL（在 Compose 环境内运行）。

运行方式：docker compose exec app pytest tests/test_changes.py
"""

import threading

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
SHIFT_A = {  # 早班 08:00-16:00
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}
SHIFT_B = {  # 晚班 16:00-24:00（与早班端点相接，不重叠）
    "device_id": "CNC-01",
    "start": "2026-10-01T16:00:00+08:00",
    "end": "2026-10-02T00:00:00+08:00",
}
E_FAIL = {  # 故障 10:00-18:00，横跨两个班次
    "device_id": "CNC-01",
    "source_id": "PLC-9",
    "version": 1,
    "start": "2026-10-01T10:00:00+08:00",
    "end": "2026-10-01T18:00:00+08:00",
    "reason": "failure",
}
E_MAINT = {  # 保养 10:00-11:00
    "device_id": "CNC-01",
    "source_id": "MES-1",
    "version": 1,
    "start": "2026-10-01T10:00:00+08:00",
    "end": "2026-10-01T11:00:00+08:00",
    "reason": "maintenance",
}


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _make_two_shifts(client) -> tuple[int, int]:
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=SHIFT_A)
    assert r.status_code == 201
    a = r.json()["id"]
    r = client.post("/shifts", json=SHIFT_B)
    assert r.status_code == 201
    return a, r.json()["id"]


def _settle_four_changes(client) -> tuple[int, int]:
    """构造 4 条变更：结算 A(seq1)、结算 B(seq2)、一次更正同时改写 A(seq3)、B(seq4)。"""
    shift_a, shift_b = _make_two_shifts(client)
    assert client.post("/events", json=E_FAIL).status_code == 201
    assert client.post(f"/shifts/{shift_a}/settle").status_code == 200
    assert client.post(f"/shifts/{shift_b}/settle").status_code == 200
    # 更正为 12:00-20:00：A 变少（360→240），B 变多（120→240）
    r = client.post(
        "/events",
        json={
            **E_FAIL,
            "version": 2,
            "start": "2026-10-01T12:00:00+08:00",
            "end": "2026-10-01T20:00:00+08:00",
        },
    )
    assert r.status_code == 201
    assert len(r.json()["recomputed"]) == 2
    return shift_a, shift_b


# ---------------------------------------------------------------- 回放与分页


def test_changes_replay_from_start(client):
    shift_a, shift_b = _settle_four_changes(client)

    # 缺省 cursor=0：从头回放全部既有修订
    r = client.get("/changes")
    assert r.status_code == 200
    body = r.json()
    assert body["next_cursor"] == 4
    assert [c["seq"] for c in body["changes"]] == [1, 2, 3, 4]

    c1, c2, c3, c4 = body["changes"]
    # 每条返回班次、修订号、各原因分钟数、差额和触发类型
    assert c1["shift_id"] == shift_a and c1["revision"] == 1
    assert c1["trigger"] == "settle"
    assert c1["totals"] == {"maintenance": 0, "failure": 360, "pending": 0}
    assert c1["delta"] == {"maintenance": 0, "failure": 360, "pending": 0}
    assert c1["device_id"] == "CNC-01"
    assert c2["shift_id"] == shift_b and c2["revision"] == 1
    assert c2["totals"]["failure"] == 120
    # 更正：A 减少 120，B 增加 120
    assert c3["shift_id"] == shift_a and c3["revision"] == 2
    assert c3["trigger"] == "correction"
    assert c3["totals"]["failure"] == 240
    assert c3["delta"] == {"maintenance": 0, "failure": -120, "pending": 0}
    assert c4["shift_id"] == shift_b and c4["revision"] == 2
    assert c4["trigger"] == "correction"
    assert c4["delta"] == {"maintenance": 0, "failure": 120, "pending": 0}


def test_changes_pagination_and_empty_page_keeps_cursor(client):
    _settle_four_changes(client)

    seen, cursor = [], 0
    for _ in range(4):  # 每页 1 条，逐页消费
        r = client.get("/changes", params={"cursor": cursor, "limit": 1})
        assert r.status_code == 200
        body = r.json()
        assert len(body["changes"]) == 1
        seen.extend(c["seq"] for c in body["changes"])
        cursor = body["next_cursor"]
    assert seen == [1, 2, 3, 4]
    assert cursor == 4

    # 已追平：空页保持原游标
    r = client.get("/changes", params={"cursor": cursor, "limit": 1})
    assert r.json() == {"changes": [], "next_cursor": 4}

    # 新变更出现后，用原游标继续读到
    r = client.post("/events", json=E_MAINT)  # 保养 10:00-11:00 落在班次 A，重算产生 seq 5
    assert r.status_code == 201
    assert len(r.json()["recomputed"]) == 1
    r = client.get("/changes", params={"cursor": cursor})
    body = r.json()
    assert [c["seq"] for c in body["changes"]] == [5]
    assert body["next_cursor"] == 5


def test_changes_empty_feed(client):
    r = client.get("/changes")
    assert r.status_code == 200
    assert r.json() == {"changes": [], "next_cursor": 0}


def test_changes_reread_returns_identical_content(client):
    _settle_four_changes(client)
    first = client.get("/changes", params={"cursor": 0, "limit": 3}).json()
    second = client.get("/changes", params={"cursor": 0, "limit": 3}).json()
    assert first == second  # 已返回条目不可变，重读内容不变


# ---------------------------------------------------------------- 不产生变更的情形


def test_no_change_entry_when_result_unchanged(client):
    shift_a, _ = _make_two_shifts(client)
    client.post("/events", json=E_MAINT)
    client.post(f"/shifts/{shift_a}/settle")  # seq 1

    # 幂等重复结算 / 幂等重复上报 / 结果未变化的更正：均不产生变更
    assert client.post(f"/shifts/{shift_a}/settle").json()["settled"] is False
    assert client.post("/events", json=E_MAINT).json()["idempotent"] is True
    r = client.post("/events", json={**E_MAINT, "version": 2})  # 同内容递增版本
    assert r.status_code == 201 and r.json()["recomputed"] == []

    body = client.get("/changes").json()
    assert [c["seq"] for c in body["changes"]] == [1]
    assert body["next_cursor"] == 1


# ---------------------------------------------------------------- 非法参数


def test_invalid_cursor_and_limit_rejected(client):
    shift_a, _ = _make_two_shifts(client)
    client.post(f"/shifts/{shift_a}/settle")  # 当前最大 seq = 1

    # 负数 / 非整数游标、超限或非法页大小 → 422 VALIDATION_ERROR
    for params in (
        {"cursor": -1},
        {"cursor": "abc"},
        {"limit": 0},
        {"limit": -3},
        {"limit": 501},
        {"limit": "abc"},
    ):
        r = client.get("/changes", params=params)
        assert r.status_code == 422, params
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR"

    # 游标超过当前最大序号 → 422 INVALID_CURSOR
    r = client.get("/changes", params={"cursor": 2})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CURSOR"

    # 拒绝不影响同步进度：合法游标仍读到一致数据
    body = client.get("/changes", params={"cursor": 0}).json()
    assert [c["seq"] for c in body["changes"]] == [1]
    # 游标等于最大序号：合法，空页保持原游标
    assert client.get("/changes", params={"cursor": 1}).json() == {
        "changes": [],
        "next_cursor": 1,
    }


def test_cursor_beyond_max_on_empty_feed_rejected(client):
    r = client.get("/changes", params={"cursor": 1})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CURSOR"


# ---------------------------------------------------------------- 并发一致性


def test_seq_contiguous_under_concurrent_settles(client):
    """并发结算：变更序号连续无缺口，游标不会越过尚未可见的较早变更。"""
    assert client.post("/devices", json=DEVICE).status_code == 201
    shift_ids = []
    for day in range(1, 9):  # 8 个互不重叠的班次
        r = client.post(
            "/shifts",
            json={
                "device_id": "CNC-01",
                "start": f"2026-10-{day:02d}T08:00:00+08:00",
                "end": f"2026-10-{day:02d}T16:00:00+08:00",
            },
        )
        assert r.status_code == 201
        shift_ids.append(r.json()["id"])

    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def worker(ids):
        try:
            barrier.wait(timeout=10)
            for sid in ids:
                r = client.post(f"/shifts/{sid}/settle")
                assert r.status_code == 200, r.text
        except Exception as exc:  # noqa: BLE001 - 汇集到主线程断言
            errors.append(exc)

    chunks = [shift_ids[i::4] for i in range(4)]
    threads = [threading.Thread(target=worker, args=(chunk,)) for chunk in chunks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    body = client.get("/changes", params={"cursor": 0, "limit": 500}).json()
    assert [c["seq"] for c in body["changes"]] == list(range(1, 9))  # 连续无缺口
    assert body["next_cursor"] == 8
    assert sorted(c["shift_id"] for c in body["changes"]) == sorted(shift_ids)
    assert all(c["revision"] == 1 and c["trigger"] == "settle" for c in body["changes"])
