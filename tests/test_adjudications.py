"""现场人工裁定端到端测试：需要可用的 PostgreSQL（在 Compose 环境内运行）。

运行方式：docker compose exec app pytest tests/test_adjudications.py
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

plus8 = timezone(timedelta(hours=8))


def dt(*args) -> datetime:
    return datetime(*args, tzinfo=plus8)

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
SHIFT = {
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}
E_MAINT = {  # 保养 10:00-12:00
    "device_id": "CNC-01",
    "source_id": "MES-1",
    "version": 1,
    "start": "2026-10-01T10:00:00+08:00",
    "end": "2026-10-01T12:00:00+08:00",
    "reason": "maintenance",
}
E_FAIL = {  # 故障 11:00-13:00（与保养重叠的 11-12 点归保养）
    "device_id": "CNC-01",
    "source_id": "PLC-9",
    "version": 1,
    "start": "2026-10-01T11:00:00+08:00",
    "end": "2026-10-01T13:00:00+08:00",
    "reason": "failure",
}


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _settled_shift(client, with_events=True) -> int:
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=SHIFT)
    assert r.status_code == 201
    sid = r.json()["id"]
    if with_events:
        assert client.post("/events", json=E_MAINT).status_code == 201
        assert client.post("/events", json=E_FAIL).status_code == 201
    assert client.post(f"/shifts/{sid}/settle").status_code == 200
    return sid


def _ruling(no: str, reason: str, start: str, end: str, note: str = "现场核对") -> dict:
    return {"ruling_no": no, "start": start, "end": end, "reason": reason, "note": note}


# ---------------------------------------------------------------- 前置校验


def test_adjudication_requires_settled_shift(client):
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=SHIFT)
    sid = r.json()["id"]
    body = _ruling("R1", "failure", "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00")
    r = client.post(f"/shifts/{sid}/adjudications", json=body)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SHIFT_NOT_SETTLED"


def test_adjudication_unknown_shift_and_ruling(client):
    body = _ruling("R1", "failure", "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00")
    r = client.post("/shifts/99999/adjudications", json=body)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SHIFT_NOT_FOUND"

    sid = _settled_shift(client)
    r = client.post(f"/shifts/{sid}/adjudications/NOPE/revoke")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RULING_NOT_FOUND"


def test_adjudication_validation_errors(client):
    sid = _settled_shift(client)
    base = "2026-10-01T10:00:00+08:00"
    cases = [
        _ruling("R1", "failure", "2026-10-01T10:00:30+08:00", "2026-10-01T11:00:00+08:00"),  # 未对齐整分钟
        _ruling("R1", "failure", "2026-10-01T10:00:00", "2026-10-01T11:00:00+08:00"),  # 缺时区
        _ruling("R1", "failure", "2026-10-01T11:00:00+08:00", "2026-10-01T10:00:00+08:00"),  # 反向区间
        _ruling("R1", "unknown", base, "2026-10-01T11:00:00+08:00"),  # 非法原因
        {**_ruling("R1", "failure", base, "2026-10-01T11:00:00+08:00"), "ruling_no": ""},  # 空单号
    ]
    for body in cases:
        r = client.post(f"/shifts/{sid}/adjudications", json=body)
        assert r.status_code == 422, body
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_adjudication_out_of_range(client):
    sid = _settled_shift(client)
    # 起点早于班次开始
    body = _ruling("R1", "failure", "2026-10-01T07:30:00+08:00", "2026-10-01T08:30:00+08:00")
    r = client.post(f"/shifts/{sid}/adjudications", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "RULING_OUT_OF_RANGE"
    # 终点晚于班次结束；半开区间端点与班次端点相等合法
    body = _ruling("R2", "failure", "2026-10-01T15:30:00+08:00", "2026-10-01T16:30:00+08:00")
    r = client.post(f"/shifts/{sid}/adjudications", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "RULING_OUT_OF_RANGE"


# ---------------------------------------------------------------- 提交 / 覆盖 / 幂等


def test_submit_ruling_overrides_events_and_revision_flow(client):
    sid = _settled_shift(client)
    # 初始：保养 120（10-12），故障 60（12-13）
    body0 = client.get(f"/shifts/{sid}/result").json()
    assert body0["totals"] == {"maintenance": 120, "failure": 60, "pending": 0}

    # 裁定 10:30-11:30 为故障：覆盖 60 分钟保养
    body = _ruling("R-001", "failure", "2026-10-01T10:30:00+08:00", "2026-10-01T11:30:00+08:00")
    r = client.post(f"/shifts/{sid}/adjudications", json=body)
    assert r.status_code == 201
    ack = r.json()
    assert ack["ruling_no"] == "R-001"
    assert ack["status"] == "active"
    assert ack["idempotent"] is False
    assert ack["revision"] == 2
    assert ack["latest_revision"] == 2
    assert ack["revision_detail"]["revision"] == 2
    assert ack["revision_detail"]["trigger"] == "adjudication"
    assert ack["revision_detail"]["totals"] == {"maintenance": 60, "failure": 120, "pending": 0}
    assert ack["revision_detail"]["delta"] == {"maintenance": -60, "failure": 60, "pending": 0}
    assert ack["revision_detail"]["seq"] >= 1

    # 最新结果随之更新；历史修订 1 不变
    res = client.get(f"/shifts/{sid}/result").json()
    assert res["revision"] == 2
    assert res["totals"] == {"maintenance": 60, "failure": 120, "pending": 0}
    res = client.get(f"/shifts/{sid}/result", params={"revision": 1}).json()
    assert res["totals"] == {"maintenance": 120, "failure": 60, "pending": 0}

    # 同单号同内容重试：幂等 200，无新修订，明确标示
    r = client.post(f"/shifts/{sid}/adjudications", json=body)
    assert r.status_code == 200
    ack = r.json()
    assert ack["idempotent"] is True
    assert ack["revision"] is None
    assert ack["revision_detail"] is None
    assert ack["latest_revision"] == 2

    # 同单号异内容冲突
    r = client.post(
        f"/shifts/{sid}/adjudications",
        json={**body, "reason": "pending"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RULING_CONFLICT"

    # 修订列表只追加了一条
    revs = client.get(f"/shifts/{sid}/revisions").json()
    assert [x["revision"] for x in revs] == [1, 2]
    assert revs[1]["trigger"] == "adjudication"


def test_ruling_without_minute_change_creates_no_revision(client):
    sid = _settled_shift(client)
    # 12:00-13:00 本来就是故障 60 分钟：裁定同原因，分钟数不变 → 无修订、无变更
    body = _ruling("R-NEUTRAL", "failure", "2026-10-01T12:00:00+08:00", "2026-10-01T13:00:00+08:00")
    r = client.post(f"/shifts/{sid}/adjudications", json=body)
    assert r.status_code == 201
    ack = r.json()
    assert ack["revision"] is None
    assert ack["revision_detail"] is None
    assert ack["latest_revision"] == 1
    # 重试幂等
    assert client.post(f"/shifts/{sid}/adjudications", json=body).status_code == 200
    # 无事件窗口裁定为 pending：有变化，产生修订
    body2 = _ruling("R-P", "pending", "2026-10-01T14:00:00+08:00", "2026-10-01T15:00:00+08:00")
    r = client.post(f"/shifts/{sid}/adjudications", json=body2)
    assert r.status_code == 201
    assert r.json()["revision"] == 2


def test_overlapping_rulings_latest_wins_and_revoke_falls_back(client):
    sid = _settled_shift(client)
    # R1：11:00-12:00 裁定 pending（覆盖该小时保养）
    b1 = _ruling("R1", "pending", "2026-10-01T11:00:00+08:00", "2026-10-01T12:00:00+08:00")
    r = client.post(f"/shifts/{sid}/adjudications", json=b1)
    assert r.json()["revision_detail"]["totals"] == {
        "maintenance": 60,
        "failure": 60,
        "pending": 60,
    }
    # R2（更晚生效）：11:30-12:30 裁定 failure —— 与 R1 重叠的 11:30-12:00 由 R2 决定
    b2 = _ruling("R2", "failure", "2026-10-01T11:30:00+08:00", "2026-10-01T12:30:00+08:00")
    r = client.post(f"/shifts/{sid}/adjudications", json=b2)
    # 当前生效：保养 10-11(60)；R1 pending 11-11:30(30)；
    # R2 failure 11:30-12:30(60)；事件故障 12:30-13(30)
    assert r.json()["revision_detail"]["totals"] == {
        "maintenance": 60,
        "failure": 90,
        "pending": 30,
    }

    # 撤销 R2：重叠的 11:30-12:00 回退到 R1(pending)，12:00-12:30 回退到事件（无覆盖）
    r = client.post(f"/shifts/{sid}/adjudications/R2/revoke")
    assert r.status_code == 201
    ack = r.json()
    assert ack["status"] == "revoked"
    assert ack["revision"] == 4
    assert ack["revision_detail"]["trigger"] == "adjudication_revoke"
    assert ack["revision_detail"]["totals"] == {
        "maintenance": 60,
        "failure": 60,
        "pending": 60,
    }
    # 回退后与 R1 生效时结果一致
    body = client.get(f"/shifts/{sid}/result", params={"revision": 2}).json()
    assert body["totals"] == ack["revision_detail"]["totals"]

    # 重复撤销幂等
    r = client.post(f"/shifts/{sid}/adjudications/R2/revoke")
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["revision"] is None
    assert r.json()["status"] == "revoked"

    # 撤销 R1：全部回退到事件归因（与修订 1 相同），仍产生一条新修订
    r = client.post(f"/shifts/{sid}/adjudications/R1/revoke")
    assert r.status_code == 201
    assert r.json()["revision_detail"]["totals"] == {
        "maintenance": 120,
        "failure": 60,
        "pending": 0,
    }


def test_revoked_ruling_no_reuse_with_different_content_conflicts(client):
    sid = _settled_shift(client)
    body = _ruling("R1", "failure", "2026-10-01T12:00:00+08:00", "2026-10-01T12:30:00+08:00")
    client.post(f"/shifts/{sid}/adjudications", json=body)
    r = client.post(f"/shifts/{sid}/adjudications/R1/revoke")
    assert r.status_code == 201
    # 已撤销单号复用：同内容幂等（视为对原裁定的重试），异内容冲突
    assert client.post(f"/shifts/{sid}/adjudications", json=body).status_code == 200
    r = client.post(
        f"/shifts/{sid}/adjudications", json={**body, "reason": "pending"}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RULING_CONFLICT"


def test_list_adjudications_includes_revoked(client):
    sid = _settled_shift(client)
    b1 = _ruling("R1", "failure", "2026-10-01T12:00:00+08:00", "2026-10-01T12:30:00+08:00")
    b2 = _ruling("R2", "pending", "2026-10-01T13:00:00+08:00", "2026-10-01T13:30:00+08:00")
    client.post(f"/shifts/{sid}/adjudications", json=b1)
    client.post(f"/shifts/{sid}/adjudications", json=b2)
    client.post(f"/shifts/{sid}/adjudications/R1/revoke")
    r = client.get(f"/shifts/{sid}/adjudications")
    assert r.status_code == 200
    rows = r.json()
    assert [x["ruling_no"] for x in rows] == ["R1", "R2"]  # 按生效顺序
    assert rows[0]["status"] == "revoked" and rows[0]["revoked_at"] is not None
    assert rows[1]["status"] == "active" and rows[1]["revoked_at"] is None
    assert rows[1]["note"] == "现场核对"

    # 未知班次 → 404
    assert client.get("/shifts/99999/adjudications").status_code == 404


# ---------------------------------------------------------------- 与事件更正联动


def test_event_correction_recomputes_under_active_ruling(client):
    sid = _settled_shift(client)
    # 裁定 10:00-11:00 为故障（原为保养）→ m60 f120
    body = _ruling("R1", "failure", "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00")
    r = client.post(f"/shifts/{sid}/adjudications", json=body)
    assert r.json()["revision_detail"]["totals"] == {
        "maintenance": 60,
        "failure": 120,
        "pending": 0,
    }
    # 事件更正：保养缩短为 11:00-11:30（v2）。
    # 裁定 10-11 仍为故障；11-11:30 保养(30)；11:30-13 故障(90)
    r = client.post(
        "/events",
        json={
            **E_MAINT,
            "version": 2,
            "start": "2026-10-01T11:00:00+08:00",
            "end": "2026-10-01T11:30:00+08:00",
        },
    )
    assert r.status_code == 201
    rec = r.json()["recomputed"]
    assert len(rec) == 1
    assert rec[0]["revision"] == 3
    assert rec[0]["totals"] == {"maintenance": 30, "failure": 150, "pending": 0}
    assert rec[0]["delta"] == {"maintenance": -30, "failure": 30, "pending": 0}


# ---------------------------------------------------------------- 变更流


def test_adjudication_changes_enter_global_cursor_feed(client):
    sid = _settled_shift(client)  # seq 1: settle
    b1 = _ruling("R1", "pending", "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00")
    client.post(f"/shifts/{sid}/adjudications", json=b1)  # seq 2
    # 无分钟变化的裁定不产生变更
    bn = _ruling("RN", "failure", "2026-10-01T12:00:00+08:00", "2026-10-01T13:00:00+08:00")
    client.post(f"/shifts/{sid}/adjudications", json=bn)
    client.post(f"/shifts/{sid}/adjudications/R1/revoke")  # seq 3

    body = client.get("/changes", params={"cursor": 0, "limit": 100}).json()
    seqs = [(c["seq"], c["trigger"], c["revision"]) for c in body["changes"]]
    assert seqs == [(1, "settle", 1), (2, "adjudication", 2), (3, "adjudication_revoke", 3)]
    assert body["next_cursor"] == 3
    # 已返回变更不可变：seq 2 重读内容一致
    c2 = [c for c in body["changes"] if c["seq"] == 2][0]
    again = client.get("/changes", params={"cursor": 1, "limit": 1}).json()["changes"][0]
    assert again == c2


# ---------------------------------------------------------------- 并发


def test_concurrent_ruling_submits_lose_no_update(client):
    sid = _settled_shift(client)
    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def worker(idx: int):
        try:
            barrier.wait(timeout=10)
            # 每个线程提交互不重叠的半小时裁定（13:00 起，原本无事件覆盖）
            start = dt(2026, 10, 1, 13, 0) + timedelta(minutes=30 * idx)
            end = start + timedelta(minutes=30)
            body = _ruling(
                f"RC-{idx}",
                "pending",
                start.isoformat(),
                end.isoformat(),
            )
            r = client.post(f"/shifts/{sid}/adjudications", json=body)
            assert r.status_code == 201, r.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 四条裁定均落库且各自产生修订，最终 pending=120
    rows = client.get(f"/shifts/{sid}/adjudications").json()
    assert len(rows) == 4
    body = client.get(f"/shifts/{sid}/result").json()
    assert body["totals"]["pending"] == 120
    revs = client.get(f"/shifts/{sid}/revisions").json()
    assert [x["revision"] for x in revs] == [1, 2, 3, 4, 5]

    # 并发同单号提交：恰好一个成功，其余按同内容幂等
    barrier2 = threading.Barrier(4)
    statuses: list[int] = []

    def same_no_worker():
        try:
            barrier2.wait(timeout=10)
            r = client.post(
                f"/shifts/{sid}/adjudications",
                json=_ruling(
                    "RDUP",
                    "failure",
                    "2026-10-01T09:00:00+08:00",
                    "2026-10-01T09:30:00+08:00",
                ),
            )
            statuses.append(r.status_code)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=same_no_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(statuses) == sorted([201, 200, 200, 200])
    revs_after = client.get(f"/shifts/{sid}/revisions").json()
    assert len(revs_after) == 6  # 只多了一条修订
