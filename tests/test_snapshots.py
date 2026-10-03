"""班次归因快照（GET /snapshots/shifts）端到端测试：需要可用的 PostgreSQL。

运行方式：docker compose exec app pytest tests/test_snapshots.py
"""

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
SHIFT_A = {
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}
SHIFT_B = {
    "device_id": "CNC-01",
    "start": "2026-10-01T16:00:00+08:00",
    "end": "2026-10-02T00:00:00+08:00",
}
SHIFT_C = {  # 另一天，与全日窗口相交、与仅覆盖 10-01 的窗口不相交
    "device_id": "CNC-01",
    "start": "2026-10-02T08:00:00+08:00",
    "end": "2026-10-02T16:00:00+08:00",
}
DAY_WINDOW = {
    "start": "2026-10-01T00:00:00+08:00",
    "end": "2026-10-02T00:00:00+08:00",
}
FULL_WINDOW = {
    "start": "2026-10-01T00:00:00+08:00",
    "end": "2026-10-03T00:00:00+08:00",
}


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _shift(client, shift: dict) -> int:
    r = client.post("/shifts", json=shift)
    assert r.status_code == 201
    return r.json()["id"]


def _failure_event(client, *, version: int, hh0: int, hh1: int, source: str = "PLC-9"):
    r = client.post(
        "/events",
        json={
            "device_id": "CNC-01",
            "source_id": source,
            "version": version,
            "start": f"2026-10-01T{hh0:02d}:00:00+08:00",
            "end": f"2026-10-01T{hh1:02d}:00:00+08:00",
            "reason": "failure",
        },
    )
    assert r.status_code in (200, 201)


def _seed(client, settle_c: bool = True) -> dict:
    """settle A(seq1)、settle B(seq2)、更正(seq3 改 A / seq4 改 B)，可选 settle C(seq5)。"""
    client.post("/devices", json=DEVICE)
    a, b, cc = _shift(client, SHIFT_A), _shift(client, SHIFT_B), _shift(client, SHIFT_C)
    _failure_event(client, version=1, hh0=10, hh1=18)  # 横跨 A、B
    assert client.post(f"/shifts/{a}/settle").status_code == 200
    assert client.post(f"/shifts/{b}/settle").status_code == 200
    _failure_event(client, version=2, hh0=10, hh1=12)  # 仅覆盖 A
    if settle_c:
        assert client.post(f"/shifts/{cc}/settle").status_code == 200
    return {"a": a, "b": b, "c": cc}


def _snapshot(client, **params):
    return client.get("/snapshots/shifts", params=params)


# ---------------------------------------------------------------- 基本语义


def test_snapshot_default_seq_is_latest_committed(client):
    ids = _seed(client)
    r = _snapshot(client, device_id="CNC-01", **FULL_WINDOW)
    assert r.status_code == 200
    body = r.json()
    assert body["snapshot_seq"] == 5  # 未指定 seq：首次请求时已提交的最大序号
    by_shift = {e["shift_id"]: e for e in body["shifts"]}
    # 截至 seq5 的最后修订：A rev2（seq3，failure=120），B rev2（seq4，failure=0），C rev1（seq5）
    assert by_shift[ids["a"]]["revision"] == 2
    assert by_shift[ids["a"]]["seq"] == 3
    assert by_shift[ids["a"]]["totals"] == {"maintenance": 0, "failure": 120, "pending": 0}
    assert by_shift[ids["b"]]["revision"] == 2
    assert by_shift[ids["b"]]["seq"] == 4
    assert by_shift[ids["b"]]["totals"] == {"maintenance": 0, "failure": 0, "pending": 0}
    assert by_shift[ids["c"]]["revision"] == 1
    assert by_shift[ids["c"]]["seq"] == 5
    # 按班次标识升序排列
    assert [e["shift_id"] for e in body["shifts"]] == sorted(by_shift)
    # 返回字段
    entry = by_shift[ids["a"]]
    assert entry["device_id"] == "CNC-01"
    assert entry["shift_start"] == "2026-10-01T00:00:00Z"
    assert entry["shift_end"] == "2026-10-01T08:00:00Z"


def test_snapshot_explicit_seq_pins_last_revision_at_that_seq(client):
    ids = _seed(client)
    # seq=1：只有 A 的修订 1（failure=360：10:00-16:00）
    body = _snapshot(client, device_id="CNC-01", seq=1, **FULL_WINDOW).json()
    assert body["snapshot_seq"] == 1
    entries = body["shifts"]
    assert [e["shift_id"] for e in entries] == [ids["a"]]
    assert entries[0]["revision"] == 1 and entries[0]["seq"] == 1
    assert entries[0]["totals"] == {"maintenance": 0, "failure": 360, "pending": 0}

    # seq=2：A rev1 与 B rev1（B failure=120：16:00-18:00）
    body = _snapshot(client, device_id="CNC-01", seq=2, **FULL_WINDOW).json()
    by_shift = {e["shift_id"]: e for e in body["shifts"]}
    assert by_shift[ids["a"]]["revision"] == 1
    assert by_shift[ids["b"]]["revision"] == 1
    assert by_shift[ids["b"]]["totals"] == {"maintenance": 0, "failure": 120, "pending": 0}
    assert ids["c"] not in by_shift  # C 在 seq5 才结算

    # seq=3/4：C 仍不可见；seq=5 才出现
    assert len(_snapshot(client, device_id="CNC-01", seq=4, **FULL_WINDOW).json()["shifts"]) == 2
    body = _snapshot(client, device_id="CNC-01", seq=5, **FULL_WINDOW).json()
    assert len(body["shifts"]) == 3


def test_only_settled_and_intersecting_shifts_listed(client):
    ids = _seed(client, settle_c=False)  # C 未结算
    body = _snapshot(client, device_id="CNC-01", **FULL_WINDOW).json()
    listed = {e["shift_id"] for e in body["shifts"]}
    assert listed == {ids["a"], ids["b"]}  # 未结算的 C 不列出

    # 窗口仅覆盖 10-01 当天：C（10-02）不相交，不列出
    body = _snapshot(client, device_id="CNC-01", **DAY_WINDOW).json()
    assert {e["shift_id"] for e in body["shifts"]} == {ids["a"], ids["b"]}

    # 窗口仅与 A 相交
    body = _snapshot(
        client,
        device_id="CNC-01",
        start="2026-10-01T08:00:00+08:00",
        end="2026-10-01T16:00:00+08:00",
    ).json()
    assert [e["shift_id"] for e in body["shifts"]] == [ids["a"]]

    # 端点相接不算相交：窗口终点 == 班次起点
    body = _snapshot(
        client,
        device_id="CNC-01",
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-01T08:00:00+08:00",
    ).json()
    assert body["shifts"] == []

    # 其他设备的班次不混入
    assert client.post("/devices", json={"id": "OTHER"}).status_code in (200, 201)
    client.post(
        "/shifts",
        json={
            "device_id": "OTHER",
            "start": "2026-10-01T08:00:00+08:00",
            "end": "2026-10-01T16:00:00+08:00",
        },
    )
    body = _snapshot(client, device_id="CNC-01", **FULL_WINDOW).json()
    assert all(e["device_id"] == "CNC-01" for e in body["shifts"])


def test_snapshot_zero_is_empty_page(client):
    _seed(client)
    body = _snapshot(client, device_id="CNC-01", seq=0, **FULL_WINDOW).json()
    assert body == {"snapshot_seq": 0, "shifts": [], "next_cursor": None}


def test_empty_system_snapshot_is_empty_page(client):
    assert client.post("/devices", json=DEVICE).status_code == 201
    # 未指定 seq 且尚无任何变更：空快照，序号为 0
    body = _snapshot(client, device_id="CNC-01", **FULL_WINDOW).json()
    assert body == {"snapshot_seq": 0, "shifts": [], "next_cursor": None}


# ---------------------------------------------------------------- 稳定翻页


def test_keyset_pagination_by_shift_id(client):
    ids = _seed(client)
    seen, cursor = [], None
    for _ in range(5):
        params = {"device_id": "CNC-01", "limit": 1, **FULL_WINDOW}
        if cursor is not None:
            params["cursor"] = cursor
        body = _snapshot(client, **params).json()
        assert body["snapshot_seq"] == 5  # 每页都回传同一快照序号
        seen.extend(e["shift_id"] for e in body["shifts"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted((ids["a"], ids["b"], ids["c"]))


def test_cursor_after_last_page_returns_empty(client):
    _seed(client)
    first = _snapshot(client, device_id="CNC-01", limit=100, **FULL_WINDOW).json()
    assert len(first["shifts"]) == 3 and first["next_cursor"] is None

    # 每页 2 条：第 1 页返回游标；第 2 页仅 1 条且 next_cursor 为 null
    p1 = _snapshot(client, device_id="CNC-01", limit=2, **FULL_WINDOW).json()
    assert len(p1["shifts"]) == 2 and p1["next_cursor"] is not None
    # 续页可回传 seq（须与游标一致），也可省略 seq 直接用游标
    p2 = _snapshot(
        client, device_id="CNC-01", seq=5, limit=2, cursor=p1["next_cursor"], **FULL_WINDOW
    ).json()
    assert len(p2["shifts"]) == 1 and p2["next_cursor"] is None

    # 重读同一游标内容一致（快照条目不可变）
    tail = _snapshot(
        client, device_id="CNC-01", seq=5, limit=2, cursor=p1["next_cursor"], **FULL_WINDOW
    ).json()
    assert tail == p2

def test_snapshot_pages_stable_against_later_writes(client):
    ids = _seed(client)
    # 快照固定在 seq5（显式 seq）
    first = _snapshot(client, device_id="CNC-01", seq=5, limit=1, **FULL_WINDOW).json()
    assert len(first["shifts"]) == 1
    cursor = first["next_cursor"]

    # 后续：新结算/更正/裁定不断产生 seq>5 的变更
    _failure_event(client, version=3, hh0=10, hh1=11)  # 更正 A → seq6
    r = client.post(
        f"/shifts/{ids['a']}/adjudications",
        json={
            "ruling_no": "R-1",
            "start": "2026-10-01T10:00:00+08:00",
            "end": "2026-10-01T11:00:00+08:00",
            "reason": "pending",
            "note": "改判",
        },
    )
    assert r.status_code == 201  # → seq7
    d = _shift(
        client,
        {
            "device_id": "CNC-01",
            "start": "2026-10-03T08:00:00+08:00",
            "end": "2026-10-03T16:00:00+08:00",
        },
    )
    assert client.post(f"/shifts/{d}/settle").status_code == 200  # → seq8

    # 同一快照（seq5 + 游标）的后续页：仍是 seq5 视角，新增更正/裁定/班次不可见
    p2 = _snapshot(client, device_id="CNC-01", seq=5, limit=1, cursor=cursor, **FULL_WINDOW).json()
    assert p2["snapshot_seq"] == 5
    assert [e["shift_id"] for e in p2["shifts"]] == [ids["b"]]
    assert p2["shifts"][0]["revision"] == 2 and p2["shifts"][0]["seq"] == 4
    p3 = _snapshot(
        client, device_id="CNC-01", seq=5, limit=1, cursor=p2["next_cursor"], **FULL_WINDOW
    ).json()
    assert [e["shift_id"] for e in p3["shifts"]] == [ids["c"]]
    assert p3["next_cursor"] is None

    # 无游标重读 seq5 全量快照：内容与翻页合并结果一致且不受新写入影响
    whole = _snapshot(client, device_id="CNC-01", seq=5, limit=500, **FULL_WINDOW).json()
    assert [e["shift_id"] for e in whole["shifts"]] == [ids["a"], ids["b"], ids["c"]]
    assert whole["shifts"][0]["totals"] == {
        "maintenance": 0,
        "failure": 120,
        "pending": 0,
    }  # 仍是 seq5 视角

    # 默认（不带 seq）的新快照则取最新序号，能看到新修订；
    # D 班次在 10-03 08:00，与 FULL_WINDOW（终于 10-03 00:00）不相交，故不出现
    latest = _snapshot(client, device_id="CNC-01", limit=500, **FULL_WINDOW).json()
    assert latest["snapshot_seq"] == 8
    assert [e["shift_id"] for e in latest["shifts"]] == [ids["a"], ids["b"], ids["c"]]
    a_entry = next(e for e in latest["shifts"] if e["shift_id"] == ids["a"])
    assert a_entry["revision"] == 4  # settle, correction, correction, adjudication
    assert a_entry["totals"] == {"maintenance": 0, "failure": 0, "pending": 60}

    # 扩大窗口后，同一最新快照能看到新结算的 D
    wider = _snapshot(
        client,
        device_id="CNC-01",
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-04T00:00:00+08:00",
    ).json()
    assert wider["snapshot_seq"] == 8
    assert [e["shift_id"] for e in wider["shifts"]] == [ids["a"], ids["b"], ids["c"], d]


def test_implicit_snapshot_seq_is_frozen_per_request_pages(client):
    """未指定 seq 时，快照序号由首次请求确定；后续页用游标携带，不随新写入漂移。"""
    _seed(client)
    first = _snapshot(client, device_id="CNC-01", limit=1, **FULL_WINDOW).json()
    snapshot_seq = first["snapshot_seq"]
    cursor = first["next_cursor"]

    _failure_event(client, version=3, hh0=10, hh1=11)  # 新变更 → seq6
    p2 = _snapshot(client, device_id="CNC-01", limit=1, cursor=cursor, **FULL_WINDOW).json()
    # 游标绑定 seq5：第二页 snapshot_seq 仍是 5，且看不到 seq6 的新修订
    assert p2["snapshot_seq"] == snapshot_seq == 5
    assert p2["shifts"][0]["seq"] <= 5


# ---------------------------------------------------------------- 游标绑定校验


@pytest.mark.parametrize(
    "override",
    [
        {"device_id": "OTHER-DEVICE"},
        {"start": "2026-09-30T00:00:00+08:00"},
        {"end": "2026-10-04T00:00:00+08:00"},
        {"seq": 1},
    ],
)
def test_cursor_mismatch_rejected(client, override):
    _seed(client)  # 三班次已结算，limit=1 必有续页游标
    cursor = _snapshot(client, device_id="CNC-01", limit=1, **FULL_WINDOW).json()["next_cursor"]
    assert cursor is not None
    if override.get("device_id") == "OTHER-DEVICE":
        client.post("/devices", json={"id": "OTHER-DEVICE", "name": ""})
    params = {"device_id": "CNC-01", "limit": 1, **FULL_WINDOW, "cursor": cursor, **override}
    r = _snapshot(client, **params)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_SNAPSHOT_CURSOR"


@pytest.mark.parametrize("bad_cursor", ["garbage", "!!!", "bm90anNvbg"])
def test_malformed_cursor_rejected(client, bad_cursor):
    client.post("/devices", json=DEVICE)
    r = _snapshot(client, device_id="CNC-01", cursor=bad_cursor, **FULL_WINDOW)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_SNAPSHOT_CURSOR"


def test_empty_cursor_treated_as_first_page(client):
    client.post("/devices", json=DEVICE)
    r = _snapshot(client, device_id="CNC-01", cursor="", **FULL_WINDOW)
    assert r.status_code == 200
    assert r.json() == {"snapshot_seq": 0, "shifts": [], "next_cursor": None}


def test_tampered_cursor_rejected(client):
    _seed(client)  # limit=1 需要 >1 个已结算班次才有续页游标
    import base64
    import json

    cursor = _snapshot(client, device_id="CNC-01", limit=1, **FULL_WINDOW).json()["next_cursor"]
    assert cursor is not None
    blob = base64.urlsafe_b64decode(cursor)
    raw, tag = blob.rsplit(b"#", 1)
    token = json.loads(raw)
    token["after_shift_id"] = token["after_shift_id"] + 1000
    tampered_raw = json.dumps(token, separators=(",", ":"), sort_keys=True).encode()
    tampered = base64.urlsafe_b64encode(tampered_raw + b"#" + tag).decode()
    r = _snapshot(client, device_id="CNC-01", cursor=tampered, **FULL_WINDOW)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_SNAPSHOT_CURSOR"


# ---------------------------------------------------------------- 参数错误


def test_seq_beyond_committed_range_rejected(client):
    _seed(client)
    r = _snapshot(client, device_id="CNC-01", seq=6, **FULL_WINDOW)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "SNAPSHOT_SEQ_OUT_OF_RANGE"

    # 新变更把最大序号推进到 6 后，seq=6 合法（错误是相对"当前已提交范围"的）
    _failure_event(client, version=3, hh0=10, hh1=11)
    body = _snapshot(client, device_id="CNC-01", seq=6, **FULL_WINDOW).json()
    assert body["snapshot_seq"] == 6
    assert body["shifts"] and all(e["seq"] <= 6 for e in body["shifts"])


def test_unknown_device_404(client):
    r = _snapshot(client, device_id="NOPE", **FULL_WINDOW)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2026-10-01T00:00:00", "end": "2026-10-02T00:00:00+08:00"},  # 缺时区
        {"start": "2026-10-01T00:00:30+08:00", "end": "2026-10-02T00:00:00+08:00"},  # 秒非 00
        {"start": "2026-10-02T00:00:00+08:00", "end": "2026-10-01T00:00:00+08:00"},  # start>=end
        {"start": "2026-10-01T00:00:00+08:00", "end": "2026-10-01T00:00:00+08:00"},  # 等长窗口
        {"limit": 0},
        {"limit": 501},
        {"limit": -1},
        {"seq": -1},
        {"device_id": ""},
    ],
)
def test_invalid_params_rejected(client, params):
    client.post("/devices", json=DEVICE)
    base = {"device_id": "CNC-01", **FULL_WINDOW}
    r = _snapshot(client, **{**base, **params})
    assert r.status_code == 422, params
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------- 与变更流衔接


def test_snapshot_seq_is_valid_changes_cursor(client):
    """快照序号可直接作为 GET /changes 的起始游标继续增量同步。"""
    _seed(client)  # 当前最大 seq=5
    snap = _snapshot(client, device_id="CNC-01", seq=3, limit=500, **FULL_WINDOW).json()
    assert snap["snapshot_seq"] == 3

    # 以 snapshot_seq 为变更流游标：只读到 seq > 3 的变更（seq4 B 的更正、seq5 C 的结算）
    feed = client.get("/changes", params={"cursor": 3, "limit": 500}).json()
    assert [c["seq"] for c in feed["changes"]] == [4, 5]
    assert feed["next_cursor"] == 5

    # 之后新结算的班次会出现在变更流，但不改变 seq=3 的快照
    d = _shift(
        client,
        {
            "device_id": "CNC-01",
            "start": "2026-10-03T08:00:00+08:00",
            "end": "2026-10-03T16:00:00+08:00",
        },
    )
    client.post(f"/shifts/{d}/settle")
    feed2 = client.get("/changes", params={"cursor": 5, "limit": 500}).json()
    assert [c["seq"] for c in feed2["changes"]] == [6]
    snap_again = _snapshot(client, device_id="CNC-01", seq=3, limit=500, **FULL_WINDOW).json()
    assert snap_again == snap
