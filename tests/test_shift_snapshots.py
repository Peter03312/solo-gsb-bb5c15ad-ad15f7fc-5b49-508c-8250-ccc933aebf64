"""班次归因快照（GET /devices/{id}/shift-snapshots）端到端测试。

覆盖：固定序号取截至该序号最后修订、窗口相交（半开）、只含已结算班次、
按班次标识稳定翻页、快照不可变（后续更正/裁定/新结算不改后续页）、
seq 缺省取当前已提交最大序号、seq 可作为 /changes 起始游标、空快照空页、
序号超范围/游标不匹配或伪造/非法窗口与页大小/未知设备等错误。

运行方式：docker compose exec app pytest tests/test_shift_snapshots.py
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
OTHER_DEVICE = {"id": "CNC-02", "name": "加工中心2号"}


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _shift(day: int, sh: int = 8, eh: int = 16, device="CNC-01") -> dict:
    return {
        "device_id": device,
        "start": f"2026-10-{day:02d}T{sh:02d}:00:00+08:00",
        "end": f"2026-10-{day:02d}T{eh:02d}:00:00+08:00",
    }


def _event(source: str, version: int, day: int, sh: int, eh: int, reason: str) -> dict:
    return {
        "device_id": "CNC-01",
        "source_id": source,
        "version": version,
        "start": f"2026-10-{day:02d}T{sh:02d}:00:00+08:00",
        "end": f"2026-10-{day:02d}T{eh:02d}:00:00+08:00",
        "reason": reason,
    }


def _settle(client, shift_id) -> None:
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.status_code == 200, r.text


def _setup(client) -> tuple[int, int, int]:
    """构造三个班次（10/1、10/2、10/3），班次1、2 已结算，班次3 未结算。"""
    assert client.post("/devices", json=DEVICE).status_code == 201
    ids = []
    for day in (1, 2, 3):
        r = client.post("/shifts", json=_shift(day))
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    s1, s2, s3 = ids
    # 班次1：故障 10:00-12:00（120 分钟），结算（seq=1）
    assert client.post("/events", json=_event("PLC-9", 1, 1, 10, 12, "failure")).status_code == 201
    _settle(client, s1)
    # 班次2：保养 09:00-10:00（60 分钟），结算（seq=2）
    assert client.post("/events", json=_event("MES-2", 1, 2, 9, 10, "maintenance")).status_code == 201
    _settle(client, s2)
    return s1, s2, s3


def _snapshot(client, **params):
    return client.get("/devices/CNC-01/shift-snapshots", params=params)


# ---------------------------------------------------------------- 基本语义


def test_snapshot_defaults_to_latest_committed_seq(client):
    s1, s2, s3 = _setup(client)  # 当前最大 seq = 2

    r = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["snapshot_seq"] == 2
    assert body["next_cursor"] is None  # 2 条 < 默认页大小，末页
    assert [e["shift_id"] for e in body["shifts"]] == [s1, s2]
    assert s3 not in [e["shift_id"] for e in body["shifts"]]  # 未结算班次不列出

    e1, e2 = body["shifts"]
    assert e1["revision"] == 1
    assert e1["totals"] == {"maintenance": 0, "failure": 120, "pending": 0}
    assert e1["start"].endswith(("+00:00", "Z"))  # 输出为带时区时间（UTC）
    assert e2["revision"] == 1
    assert e2["totals"] == {"maintenance": 60, "failure": 0, "pending": 0}


def test_snapshot_pinned_to_seq_takes_last_revision_at_or_before_seq(client):
    s1, s2, _ = _setup(client)
    # 迟到更正班次1：故障缩短为 10:00-11:00 → seq=3（60 分钟）
    r = client.post("/events", json=_event("PLC-9", 2, 1, 10, 11, "failure"))
    assert r.status_code == 201
    assert [x["shift_id"] for x in r.json()["recomputed"]] == [s1]
    # 裁定再改班次1 10:00-11:00 为待判 → seq=4
    r = client.post(
        f"/shifts/{s1}/adjudications",
        json={
            "ruling_no": "R-1",
            "start": "2026-10-01T10:00:00+08:00",
            "end": "2026-10-01T11:00:00+08:00",
            "reason": "pending",
            "note": "待复核",
        },
    )
    assert r.status_code == 201 and r.json()["revision"] == 3

    # seq=2 的快照：班次1 仍是修订1（120 故障），看不到 seq=3/4
    body = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
        seq=2,
    ).json()
    assert body["snapshot_seq"] == 2
    (e1,) = [e for e in body["shifts"] if e["shift_id"] == s1]
    assert e1["revision"] == 1
    assert e1["totals"] == {"maintenance": 0, "failure": 120, "pending": 0}

    # seq=3 的快照：班次1 为修订2（60 故障）
    body = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
        seq=3,
    ).json()
    (e1,) = [e for e in body["shifts"] if e["shift_id"] == s1]
    assert e1["revision"] == 2
    assert e1["totals"] == {"maintenance": 0, "failure": 60, "pending": 0}

    # seq=4（最新）的快照：班次1 为修订3（60 待判）
    body = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
        seq=4,
    ).json()
    (e1,) = [e for e in body["shifts"] if e["shift_id"] == s1]
    assert e1["revision"] == 3
    assert e1["totals"] == {"maintenance": 0, "failure": 0, "pending": 60}


def test_snapshot_half_open_window_intersection(client):
    s1, s2, s3 = _setup(client)
    # 窗口 [10/1 16:00, 10/3 00:00)：与班次1 仅端点相接 → 不包含；与班次2 相交
    body = _snapshot(
        client,
        start="2026-10-01T16:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
    ).json()
    assert [e["shift_id"] for e in body["shifts"]] == [s2]

    # 只与班次1 相交（裁剪式相交：窗口落在班次内部）
    body = _snapshot(
        client,
        start="2026-10-01T09:00:00+08:00",
        end="2026-10-01T09:30:00+08:00",
    ).json()
    assert [e["shift_id"] for e in body["shifts"]] == [s1]

    # 完全不相交：空页
    body = _snapshot(
        client,
        start="2026-11-01T00:00:00+08:00",
        end="2026-11-02T00:00:00+08:00",
    ).json()
    assert body["shifts"] == []
    assert body["next_cursor"] is None
    assert body["snapshot_seq"] == 2


def test_snapshot_filters_device(client):
    s1, _, _ = _setup(client)
    assert client.post("/devices", json=OTHER_DEVICE).status_code == 201
    r = client.post("/shifts", json=_shift(1, device="CNC-02"))
    other_id = r.json()["id"]
    _settle(client, other_id)

    body = client.get(
        "/devices/CNC-02/shift-snapshots",
        params={
            "start": "2026-10-01T00:00:00+08:00",
            "end": "2026-10-03T00:00:00+08:00",
        },
    ).json()
    assert [e["shift_id"] for e in body["shifts"]] == [other_id]
    assert body["device_id"] == "CNC-02"


def test_empty_snapshot_when_no_committed_changes(client):
    assert client.post("/devices", json=DEVICE).status_code == 201
    # 有班次但未结算：截至 seq=0 无任何已结算班次 → 空快照
    r = client.post("/shifts", json=_shift(1))
    assert r.status_code == 201
    body = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
    ).json()
    assert body == {
        "device_id": "CNC-01",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-03T00:00:00+08:00",
        "snapshot_seq": 0,
        "shifts": [],
        "next_cursor": None,
    }


# ---------------------------------------------------------------- 翻页与快照不可变


def test_snapshot_pagination_stable_by_shift_id(client):
    s1, s2, _ = _setup(client)
    # 每页 1 条，按班次 id 升序翻完
    r = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
        limit=1,
    )
    page1 = r.json()
    assert [e["shift_id"] for e in page1["shifts"]] == [s1]
    assert page1["snapshot_seq"] == 2
    assert page1["next_cursor"] is not None

    r = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
        limit=1,
        cursor=page1["next_cursor"],
    )
    page2 = r.json()
    assert [e["shift_id"] for e in page2["shifts"]] == [s2]
    assert page2["snapshot_seq"] == 2  # 每页回传同一快照序号
    assert page2["next_cursor"] is None  # 末页

    # 重读第一页内容完全一致（不可变）
    again = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
        limit=1,
    ).json()
    assert again["shifts"] == page1["shifts"]


def test_snapshot_pages_immutable_after_later_changes(client):
    s1, s2, s3 = _setup(client)
    # 固定 seq=2 的快照，取首页（limit=1），保存续页游标
    page1 = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-05T00:00:00+08:00",
        seq=2,
        limit=1,
    ).json()
    assert [e["shift_id"] for e in page1["shifts"]] == [s1]
    cursor = page1["next_cursor"]

    # 之后发生：班次1 更正（seq=3）、结算班次3（seq=4）、新班次（10/4）并结算（seq=5）
    assert (
        client.post("/events", json=_event("PLC-9", 2, 1, 10, 11, "failure")).status_code
        == 201
    )
    _settle(client, s3)
    r = client.post("/shifts", json=_shift(4))
    s4 = r.json()["id"]
    _settle(client, s4)

    # 用旧游标续页：仍是 seq=2 的视图 —— 班次2、然后末页；
    # 班次1 的修订不变、班次3/4（seq ≥ 4/5）不出现在 seq=2 快照中
    seen = [e["shift_id"] for e in page1["shifts"]]
    while cursor is not None:
        body = _snapshot(
            client,
            start="2026-10-01T00:00:00+08:00",
            end="2026-10-05T00:00:00+08:00",
            seq=2,
            limit=1,
            cursor=cursor,
        ).json()
        assert body["snapshot_seq"] == 2
        seen.extend(e["shift_id"] for e in body["shifts"])
        # 每条都是截至 seq=2 的最后修订
        for e in body["shifts"]:
            assert e["revision"] == 1
        cursor = body["next_cursor"]
    assert seen == [s1, s2]


def test_default_seq_snapshot_remains_pinned_via_cursor(client):
    s1, s2, _ = _setup(client)  # max seq=2
    page1 = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
        limit=1,
    ).json()
    assert page1["snapshot_seq"] == 2

    # 首次请求未指定 seq（已固定为 2）；此后新结算 seq=3
    assert (
        client.post("/events", json=_event("PLC-9", 2, 1, 10, 11, "failure")).status_code
        == 201
    )
    # 续页即使不显式传 seq，游标仍把视图固定在 seq=2
    page2 = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
        limit=1,
        cursor=page1["next_cursor"],
    ).json()
    assert page2["snapshot_seq"] == 2
    assert [e["shift_id"] for e in page2["shifts"]] == [s2]
    assert page2["shifts"][0]["revision"] == 1
    assert page2["next_cursor"] is None

    # 而重新发起不带 seq 的首页请求：快照序号推进到当前最大 3
    fresh = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
    ).json()
    assert fresh["snapshot_seq"] == 3
    e1 = next(e for e in fresh["shifts"] if e["shift_id"] == s1)
    assert e1["revision"] == 2


# ---------------------------------------------------------------- 与变更流衔接


def test_snapshot_seq_usable_as_changes_cursor(client):
    s1, s2, _ = _setup(client)  # seq=1 结算班次1，seq=2 结算班次2
    body = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
    ).json()
    snapshot_seq = body["snapshot_seq"]

    # 快照之后：班次1 更正产生 seq=3
    assert (
        client.post("/events", json=_event("PLC-9", 2, 1, 10, 11, "failure")).status_code
        == 201
    )
    # 以快照序号为 /changes 起始游标：只读到 seq > 快照序号 的增量
    changes = client.get("/changes", params={"cursor": snapshot_seq}).json()
    assert [c["seq"] for c in changes["changes"]] == [3]
    assert changes["changes"][0]["shift_id"] == s1
    assert changes["changes"][0]["trigger"] == "correction"


# ---------------------------------------------------------------- 错误


def test_snapshot_seq_beyond_committed_range_rejected(client):
    _setup(client)  # max seq=2
    r = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
        seq=3,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_SNAPSHOT_SEQ"

    # 空库（无设备无变更）指定 seq=1 同样超范围
    with engine.begin() as conn:
        for t in TABLES:
            conn.execute(text(f"TRUNCATE {t} CASCADE"))
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
        seq=1,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_SNAPSHOT_SEQ"
    # seq=0 在空库上合法（空快照）
    assert (
        _snapshot(
            client,
            start="2026-10-01T00:00:00+08:00",
            end="2026-10-03T00:00:00+08:00",
            seq=0,
        ).status_code
        == 200
    )


def test_snapshot_cursor_mismatch_rejected(client):
    s1, s2, _ = _setup(client)
    page1 = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
        seq=2,
        limit=1,
    ).json()
    cursor = page1["next_cursor"]

    def _with_cursor(**extra):
        params = {
            "start": "2026-10-01T00:00:00+08:00",
            "end": "2026-10-03T00:00:00+08:00",
            "seq": 2,
            "cursor": cursor,
        }
        params.update(extra)
        return _snapshot(client, **params)

    # 序号不匹配
    r = _with_cursor(seq=1)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "INVALID_CURSOR"

    # 窗口不匹配（终点不同）
    r = _with_cursor(seq=2, end="2026-10-04T00:00:00+08:00")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "INVALID_CURSOR"
    # 窗口起点不同
    r = _with_cursor(seq=2, start="2026-10-01T01:00:00+08:00")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "INVALID_CURSOR"

    # 游标属于其他设备
    assert client.post("/devices", json=OTHER_DEVICE).status_code == 201
    r = client.get(
        "/devices/CNC-02/shift-snapshots",
        params={
            "start": "2026-10-01T00:00:00+08:00",
            "end": "2026-10-03T00:00:00+08:00",
            "seq": 2,
            "cursor": cursor,
        },
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "INVALID_CURSOR"

    # 伪造 / 损坏 / 空串游标
    for bad in (cursor[:-3] + "AAA", "not-a-cursor", "....", ""):
        r = _snapshot(
            client,
            start="2026-10-01T00:00:00+08:00",
            end="2026-10-03T00:00:00+08:00",
            cursor=bad,
        )
        assert r.status_code == 422, (bad, r.status_code)
        assert r.json()["detail"]["code"] == "INVALID_CURSOR", bad

    # 游标被正确拒绝后，不带游标重读仍正常
    assert (
        _snapshot(
            client,
            start="2026-10-01T00:00:00+08:00",
            end="2026-10-03T00:00:00+08:00",
            seq=2,
        ).status_code
        == 200
    )


def test_snapshot_invalid_window_and_limit(client):
    _setup(client)
    base = {"end": "2026-10-03T00:00:00+08:00"}
    # 缺时区 / 未对齐整分钟 / start ≥ end / 非时间格式 → 422 VALIDATION_ERROR
    for params in (
        {"start": "2026-10-01T00:00:00", "end": "2026-10-03T00:00:00+08:00"},
        {"start": "2026-10-01T00:00:30+08:00", "end": "2026-10-03T00:00:00+08:00"},
        {"start": "2026-10-03T00:00:00+08:00", "end": "2026-10-03T00:00:00+08:00"},
        {"start": "2026-10-04T00:00:00+08:00", "end": "2026-10-03T00:00:00+08:00"},
        {"start": "not-a-time", "end": "2026-10-03T00:00:00+08:00"},
        {**base, "limit": 0},
        {**base, "limit": -1},
        {**base, "limit": 501},
        {**base, "limit": "abc"},
        {**base, "seq": -1},
    ):
        params.setdefault("start", "2026-10-01T00:00:00+08:00")
        params.setdefault("end", "2026-10-03T00:00:00+08:00")
        r = _snapshot(client, **params)
        assert r.status_code == 422, params
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR", params


def test_snapshot_unknown_device_404(client):
    r = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_snapshot_window_timezone_equivalence(client):
    """同一绝对时刻用不同时区表达等价（游标按 UTC 归一化比对）。"""
    s1, _, _ = _setup(client)
    page = _snapshot(
        client,
        start="2026-10-01T00:00:00+08:00",  # = 2026-09-30T16:00:00Z
        end="2026-10-03T00:00:00+08:00",
        limit=1,
    ).json()
    assert [e["shift_id"] for e in page["shifts"]] == [s1]
    # 用等价的 UTC 表达续页：窗口判定一致，游标匹配
    r = _snapshot(
        client,
        start="2026-09-30T16:00:00+00:00",
        end="2026-10-02T16:00:00+00:00",
        limit=1,
        cursor=page["next_cursor"],
    )
    assert r.status_code == 200, r.text
