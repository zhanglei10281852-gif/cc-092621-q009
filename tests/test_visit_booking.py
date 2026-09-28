from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock, to_storage
from app.database import close_connection, get_connection, init_db
from app.temple.booking import TempleBookingService
from app.temple.operations import TempleRestorationService
from app.temple.service import TempleSafetyService

BASE = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
START = BASE + timedelta(hours=2)
END = BASE + timedelta(hours=4)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMPLE_DATABASE_PATH", str(tmp_path / "booking.db"))
    close_connection()
    init_db()
    yield
    close_connection()


def make_service(clock: FrozenClock | None = None) -> TempleBookingService:
    return TempleBookingService(get_connection(), clock or FrozenClock(BASE))


def setup_temple(service: TempleBookingService, *, code: str = "miaohui-temple", quotas=None, total: int = 100, timeout: int = 300) -> dict:
    TempleSafetyService(get_connection(), FrozenClock(BASE)).create_temple({
        "code": code, "name": "庙会古寺", "temple_type": "heritage", "timezone": "Asia/Shanghai",
        "max_concurrent_mitigation_sessions": 100, "ventilation_capacity": 1000,
    })
    for gate_code in ("east", "west"):
        service.create_gate({"temple_code": code, "code": gate_code, "name": f"{gate_code}门"})
    payload = {
        "temple_code": code, "code": "morning", "starts_at": to_storage(START), "ends_at": to_storage(END),
        "total_capacity": total, "confirm_timeout_seconds": timeout, "quotas": quotas or [],
    }
    return service.create_segment(payload)


def reserve(service: TempleBookingService, identity: str, cohort: str = "general", gate: str | None = None,
           party: int = 1, segment: str = "morning", temple: str = "miaohui-temple", key: str | None = None) -> dict:
    return service.create_reservation({
        "temple_code": temple, "segment_code": segment, "identity_hash": identity,
        "cohort": cohort, "gate_code": gate, "party_size": party, "idempotency_key": key,
    })


def test_layered_quotas_never_oversell():
    service = make_service()
    quotas = [
        {"gate_code": "east", "cohort": "general", "capacity": 1},
        {"gate_code": "west", "cohort": "general", "capacity": 1},
        {"gate_code": None, "cohort": "elderly", "capacity": 1},
    ]
    setup_temple(service, total=3, quotas=quotas)
    assert reserve(service, "id-e1", "general", "east")["state"] == "held"
    assert reserve(service, "id-w1", "general", "west")["state"] == "held"
    assert reserve(service, "id-old1", "elderly", "east")["state"] == "held"
    # 总量已满（3/3）：新预约进入候补而非超卖
    overflow = reserve(service, "id-old2", "elderly", "west")
    assert overflow["state"] == "waiting"
    # 东门口×普通人群分层已满，即便总量未满也不能占该层
    service2 = make_service()
    setup_temple(service2, code="layer-temple", total=10, quotas=[{"gate_code": "east", "cohort": "general", "capacity": 1}])
    assert reserve(service2, "a", "general", "east", temple="layer-temple")["state"] == "held"
    blocked = reserve(service2, "b", "general", "east", temple="layer-temple")
    assert blocked["state"] == "waiting"
    # 不走东门则不受该分层限制
    assert reserve(service2, "c", "general", "west", temple="layer-temple")["state"] == "held"


def test_held_reservation_requires_confirmation_and_double_confirm_is_consistent():
    service = make_service()
    setup_temple(service, total=1)
    booking = reserve(service, "id-confirm")
    assert booking["state"] == "held"
    assert booking["confirm_deadline"] is not None
    confirmed = service.confirm_reservation(booking["reservation_no"], "visitor")
    assert confirmed["state"] == "confirmed"
    # 并发/重放确认：第二次直接返回同一状态，不产生重复状态迁移
    again = service.confirm_reservation(booking["reservation_no"], "visitor")
    assert again["state"] == "confirmed"
    events = [event["event_type"] for event in again["events"]]
    assert events.count("confirmed") == 1


def test_same_identity_cannot_hold_overlapping_segments():
    service = make_service()
    setup_temple(service, total=10)
    service.create_segment({
        "temple_code": "miaohui-temple", "code": "morning-2",
        "starts_at": to_storage(START + timedelta(hours=1)), "ends_at": to_storage(END + timedelta(hours=1)),
        "total_capacity": 10, "confirm_timeout_seconds": 300, "quotas": [],
    })
    reserve(service, "id-overlap", "general")
    try:
        reserve(service, "id-overlap", "general", segment="morning-2")
    except Exception as exc:
        assert getattr(exc, "code", "") == "conflict"
        assert exc.context["overlap_segment"] == "morning"
    else:
        raise AssertionError("重叠时段重复占位必须被拒绝")
    # 不重叠的更晚时段允许预约
    service.create_segment({
        "temple_code": "miaohui-temple", "code": "afternoon",
        "starts_at": to_storage(END), "ends_at": to_storage(END + timedelta(hours=2)),
        "total_capacity": 10, "confirm_timeout_seconds": 300, "quotas": [],
    })
    assert reserve(service, "id-overlap", "general", segment="afternoon")["state"] == "held"


def test_idempotent_replay_returns_same_reservation():
    service = make_service()
    setup_temple(service, total=2)
    payload = dict(temple_code="miaohui-temple", segment_code="morning", identity_hash="id-replay",
                   cohort="general", gate_code=None, party_size=1)
    first = service.create_reservation({**payload, "idempotency_key": "key-0001"})
    replay = service.create_reservation({**payload, "idempotency_key": "key-0001"})
    assert replay["reservation_no"] == first["reservation_no"]
    assert replay.get("replayed") is True
    # 无幂等键的网络重放：同一身份同一时段返回原预约
    plain_replay = service.create_reservation({**payload, "idempotency_key": None})
    assert plain_replay["reservation_no"] == first["reservation_no"]
    assert plain_replay.get("replayed") is True
    # 相同幂等键但请求体不同 -> 冲突
    try:
        service.create_reservation({**payload, "party_size": 2, "idempotency_key": "key-0001"})
    except Exception as exc:
        assert getattr(exc, "code", "") == "conflict"
    else:
        raise AssertionError("幂等键对应不同请求体必须被拒绝")


def test_cancellation_releases_and_promotes_in_explainable_order():
    clock = FrozenClock(BASE)
    service = make_service(clock)
    setup_temple(service, total=3)
    seated = [reserve(service, f"id-seat-{i}", "general") for i in range(3)]
    g1 = reserve(service, "id-g1", "general")
    e1 = reserve(service, "id-e1", "elderly")
    c1 = reserve(service, "id-c1", "ceremony")
    assert [item["state"] for item in (g1, e1, c1)] == ["waiting", "waiting", "waiting"]
    # 候补名次：老人 > 法会 > 普通，即使老人登记更晚
    waitlist = service.waitlist(seated[0]["segment_id"])
    assert [item["identity_hash"] for item in waitlist] == ["id-e1", "id-c1", "id-g1"]
    assert waitlist[0]["queue_position"] == 1

    cancel_result = service.cancel_reservation(seated[0]["reservation_no"], "visitor", "visitor_cancelled")
    promoted = cancel_result["promotions"]
    assert len(promoted) == 1
    assert promoted[0]["reservation_no"] == e1["reservation_no"]
    assert promoted[0]["rule_code"] == "cohort_priority"
    # 管理接口能说明递补用了哪条规则、消耗了哪笔释放容量
    records = service.list_segment_promotions(seated[0]["segment_id"])
    assert len(records) == 1
    record = records[0]
    assert record["rule_code"] == "cohort_priority"
    assert record["rule_label"].startswith("人群优先")
    assert record["release"]["reason"] == "cancelled"
    assert record["release"]["source_reservation_id"] == seated[0]["id"]
    assert record["reservation"]["reservation_no"] == e1["reservation_no"]

    # 再释放两笔：法会先于普通
    service.cancel_reservation(seated[1]["reservation_no"], "visitor", "visitor_cancelled")
    service.cancel_reservation(seated[2]["reservation_no"], "visitor", "visitor_cancelled")
    records = service.list_segment_promotions(seated[0]["segment_id"])
    assert [r["reservation"]["identity_hash"] for r in records] == ["id-e1", "id-c1", "id-g1"]
    detail = service.reservation_detail(g1["reservation_no"])
    assert detail["state"] == "held"
    assert detail["promotion"]["rule_code"] == "fifo"
    assert detail["promotion"]["release"]["reason"] == "cancelled"
    # 每次递补都关联到具体释放批次
    release_ids = {r["release_id"] for r in records}
    assert len(release_ids) == 3


def test_confirm_timeout_expires_releases_and_promotes():
    clock = FrozenClock(BASE)
    service = TempleBookingService(get_connection(), clock)
    setup_temple(service, total=1, timeout=300)
    booking = reserve(service, "id-held", "general")
    waiter = reserve(service, "id-wait", "elderly")
    assert waiter["state"] == "waiting"
    clock.advance(minutes=6)
    result = service.expire_unconfirmed("reaper")
    assert booking["reservation_no"] in result["expired"]
    expired = service.reservation_detail(booking["reservation_no"])
    assert expired["state"] == "expired"
    promoted = service.reservation_detail(waiter["reservation_no"])
    assert promoted["state"] == "held"
    assert promoted["promotion"]["release"]["reason"] == "confirm_timeout"
    assert promoted["promotion"]["rule_code"] == "fifo"


def test_gate_affinity_breaks_tie_and_is_recorded():
    service = make_service()
    quotas = [
        {"gate_code": "east", "cohort": None, "capacity": 1},
        {"gate_code": "west", "cohort": None, "capacity": 3},
    ]
    setup_temple(service, total=3, quotas=quotas)
    east_seat = reserve(service, "id-east-0", "general", "east")
    reserve(service, "id-west-0", "general", "west")
    reserve(service, "id-west-1", "general", "west")
    # 总量已满；东门候补被入口层和总量双重阻塞，西门候补只被总量阻塞
    east_waiter = reserve(service, "id-east-wait", "general", "east")
    west_waiter = reserve(service, "id-west-wait", "general", "west")
    assert east_waiter["state"] == west_waiter["state"] == "waiting"
    service.cancel_reservation(east_seat["reservation_no"], "visitor", "visitor_cancelled")
    promoted_east = service.reservation_detail(east_waiter["reservation_no"])
    still_waiting = service.reservation_detail(west_waiter["reservation_no"])
    assert promoted_east["state"] == "held"
    assert promoted_east["promotion"]["rule_code"] == "gate_affinity"
    assert promoted_east["promotion"]["rule_detail"]["release_gate_id"] is not None
    assert still_waiting["state"] == "waiting"


def test_closure_hold_tightens_capacity_and_cancel_releases_for_waitlist():
    clock = FrozenClock(BASE)
    operations = TempleRestorationService(get_connection(), clock)
    service = TempleBookingService(get_connection(), clock)
    setup_temple(service, total=5)
    window = operations.create_closure({
        "temple_code": "miaohui-temple", "hall_code": None, "code": "grand-closure",
        "reason": "临时法事清场", "starts_at": to_storage(START), "ends_at": to_storage(START + timedelta(hours=1)),
        "drain_mode": "block_new", "actor": "operator",
    })
    result = service.add_closure_impacts(window["id"], [
        {"segment_code": "morning", "gate_code": None, "cohort": None, "seats": 2}
    ], "operator")
    assert len(result["holds"]) == 1
    detail = service.segment_detail_by_code("miaohui-temple", "morning")
    assert detail["snapshot"]["total"]["closure_held"] == 2
    assert detail["snapshot"]["total"]["available"] == 3
    bookings = [reserve(service, f"id-cap-{i}", "general") for i in range(3)]
    waiter = reserve(service, "id-cap-wait", "elderly")
    assert waiter["state"] == "waiting"
    # 封闭窗口取消：收紧名额释放，候补递补
    cancelled = operations.cancel_closure(window["id"], "operator", "法事提前结束")
    assert cancelled["state"] == "cancelled"
    assert len(cancelled["releases"]) == 1
    assert cancelled["releases"][0]["reason"] == "closure_released"
    assert cancelled["releases"][0]["closure_window_id"] == window["id"]
    promoted = service.reservation_detail(waiter["reservation_no"])
    assert promoted["state"] == "held"
    assert promoted["promotion"]["release"]["reason"] == "closure_released"
    snapshot = service.segment_detail(bookings[0]["segment_id"])["snapshot"]
    assert snapshot["total"]["closure_held"] == 0
    assert snapshot["total"]["used"] == 4


def test_closure_completion_by_scheduler_releases_holds():
    clock = FrozenClock(BASE)
    operations = TempleRestorationService(get_connection(), clock)
    service = TempleBookingService(get_connection(), clock)
    setup_temple(service, total=2)
    window = operations.create_closure({
        "temple_code": "miaohui-temple", "hall_code": None, "code": "scheduled-closure",
        "reason": "设备检修", "starts_at": to_storage(START), "ends_at": to_storage(START + timedelta(minutes=30)),
        "drain_mode": "finish_active", "actor": "operator",
    })
    service.add_closure_impacts(window["id"], [
        {"segment_code": "morning", "gate_code": None, "cohort": None, "seats": 2}
    ], "operator")
    waiter = reserve(service, "id-sched-wait", "general")
    assert waiter["state"] == "waiting"
    later = TempleRestorationService(get_connection(), FrozenClock(START + timedelta(hours=1)))
    advanced = later.activate_due_closure("scheduler")
    assert window["id"] in advanced["completed"]
    assert advanced["releases"] and advanced["releases"][0]["reason"] == "closure_released"
    promoted = TempleBookingService(get_connection(), FrozenClock(START + timedelta(hours=1))).reservation_detail(waiter["reservation_no"])
    assert promoted["state"] == "held"


def test_state_and_queue_rank_survive_service_restart():
    service = make_service()
    segment = setup_temple(service, total=1)
    reserve(service, "id-full", "general")
    elderly_waiter = reserve(service, "id-persist-old", "elderly")
    general_waiter = reserve(service, "id-persist-gen", "general")
    # 模拟服务重启：释放线程内连接后用新实例重新打开同一个数据库文件
    close_connection()
    restarted = TempleBookingService(get_connection(), FrozenClock(BASE))
    detail = restarted.segment_detail(segment["id"])
    assert detail["snapshot"]["total"]["used"] == 1
    waitlist = restarted.waitlist(segment["id"])
    assert [item["identity_hash"] for item in waitlist] == ["id-persist-old", "id-persist-gen"]
    assert restarted.reservation_detail(elderly_waiter["reservation_no"])["queue_position"] == 1
    assert restarted.reservation_detail(general_waiter["reservation_no"])["queue_position"] == 2
    promotions = restarted.list_segment_promotions(segment["id"])
    assert promotions == []


def test_concurrent_reservations_never_oversell():
    service = make_service()
    segment = setup_temple(service, total=10, quotas=[{"gate_code": "east", "cohort": None, "capacity": 6}])
    results: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            close_connection()  # 每个工作线程使用独立连接
            local = TempleBookingService(get_connection(), FrozenClock(BASE))
            gate = "east" if index % 2 == 0 else None
            outcome = local.create_reservation({
                "temple_code": "miaohui-temple", "segment_code": "morning",
                "identity_hash": f"id-concurrent-{index:03d}", "cohort": "general",
                "gate_code": gate, "party_size": 1,
            })
            with lock:
                results.append(outcome)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    active = [item for item in results if item["state"] in ("held", "confirmed")]
    waiting = [item for item in results if item["state"] == "waiting"]
    assert len(active) == 10
    assert len(waiting) == 30
    snapshot = TempleBookingService(get_connection(), FrozenClock(BASE)).segment_detail(segment["id"])["snapshot"]
    east_active = sum(1 for item in active if item["gate_id"] is not None)
    assert east_active <= 6
    assert snapshot["total"]["used"] == 10
    assert snapshot["total"]["available"] == 0
    # 候补序号唯一且单调
    seqs = [item["queue_seq"] for item in waiting]
    assert len(seqs) == len(set(seqs))


def test_http_flow_segment_quota_waitlist_promotion_admin(client):
    # HTTP 链路使用真实系统时钟，时段取未来日期
    http_start = to_storage(datetime(2026, 12, 31, 2, 0, tzinfo=UTC))
    http_end = to_storage(datetime(2026, 12, 31, 4, 0, tzinfo=UTC))
    headers = {"Content-Type": "application/json"}
    client.post("/api/temple/temples", json={
        "code": "http-temple", "name": "接口古寺", "temple_type": "community",
        "max_concurrent_mitigation_sessions": 10, "ventilation_capacity": 100,
    })
    client.post("/api/temple/visit/gates", json={"temple_code": "http-temple", "code": "shan", "name": "山门"})
    segment = client.post("/api/temple/visit/segments", json={
        "temple_code": "http-temple", "code": "slot-1",
        "starts_at": http_start, "ends_at": http_end, "total_capacity": 1,
        "confirm_timeout_seconds": 300,
        "quotas": [{"gate_code": "shan", "cohort": "general", "capacity": 1}],
    }).json()
    first = client.post("/api/temple/visit/reservations", json={
        "temple_code": "http-temple", "segment_code": "slot-1", "identity_hash": "http-ident-1",
        "cohort": "general", "gate_code": "shan", "party_size": 1,
    }, headers=headers)
    assert first.status_code == 201, first.text
    general_waiter = client.post("/api/temple/visit/reservations", json={
        "temple_code": "http-temple", "segment_code": "slot-1", "identity_hash": "http-ident-2",
        "cohort": "general", "gate_code": "shan", "party_size": 1,
    }, headers=headers)
    assert general_waiter.status_code == 201, general_waiter.text
    assert general_waiter.json()["state"] == "waiting"
    # 老人候补登记更晚，但人群优先应最先递补
    waiter = client.post("/api/temple/visit/reservations", json={
        "temple_code": "http-temple", "segment_code": "slot-1", "identity_hash": "http-ident-3",
        "cohort": "elderly", "gate_code": "shan", "party_size": 1,
    }, headers=headers)
    assert waiter.status_code == 201, waiter.text
    assert waiter.json()["state"] == "waiting"
    # 分层容量在时段详情中可见
    detail = client.get(f"/api/temple/visit/segments/{segment['id']}").json()
    assert detail["snapshot"]["total"]["capacity"] == 1
    cancel = client.post(f"/api/temple/visit/reservations/{first.json()['reservation_no']}/cancel",
                         json={"actor": "visitor", "reason": "visitor_cancelled"})
    assert cancel.status_code == 200
    assert cancel.json()["promotions"][0]["rule_code"] == "cohort_priority"
    promotions = client.get(f"/api/temple/visit/segments/{segment['id']}/promotions").json()["items"]
    assert len(promotions) == 1
    assert promotions[0]["rule_label"]
    assert promotions[0]["release"]["reason"] == "cancelled"
    assert promotions[0]["reservation"]["reservation_no"] == waiter.json()["reservation_no"]
    confirmed = client.post(f"/api/temple/visit/reservations/{waiter.json()['reservation_no']}/confirm",
                            json={"actor": "visitor"})
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "confirmed"
