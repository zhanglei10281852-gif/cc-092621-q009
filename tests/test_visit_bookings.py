from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from app.core.clock import FrozenClock
from app.database import close_connection, get_connection
from app.temple.operations import TempleRestorationService
from app.temple.visits import VisitBookingService

SLOT_START = "2026-10-01T09:00:00Z"
SLOT_END = "2026-10-01T10:00:00Z"


def prepare_temple(client, total_capacity: int = 4) -> int:
    client.post(
        "/api/temple/temples",
        json={"code": "fahui-temple", "name": "法会古寺", "temple_type": "heritage", "timezone": "Asia/Shanghai",
              "max_concurrent_mitigation_sessions": 100, "ventilation_capacity": 1000},
    )
    for seq, (code, name) in enumerate((("daxiong", "大雄宝殿"), ("guanyin", "观音殿")), start=1):
        client.post(
            "/api/temple/temples/fahui-temple/halls",
            json={"code": code, "name": name, "visit_order": seq, "expected_visit_seconds": 900,
                  "ventilation_capacity": 400},
        )
    slot = client.post(
        "/api/temple/visits/slots",
        json={"temple_code": "fahui-temple", "starts_at": SLOT_START, "ends_at": SLOT_END, "actor": "tests"},
    )
    assert slot.status_code == 201, slot.text
    slot_id = slot.json()["id"]
    # 分层名额：寺院总量是默认绑定层；殿堂/入口交叉层保持宽松以单独验证总量，
    # 叶子层收紧场景由各测试自行 PUT 覆盖。
    for body in (
        {"capacity": total_capacity},
        {"hall_code": "daxiong", "capacity": 100},
        {"entrance_code": "east", "capacity": 100},
        {"visitor_group": "elder", "capacity": 2},
        {"hall_code": "daxiong", "entrance_code": "east", "capacity": 100},
    ):
        response = client.put(f"/api/temple/visits/slots/{slot_id}/capacities", json=body)
        assert response.status_code == 200, response.text
    return slot_id


def reserve(client, slot_id: int, *, visitor_hash: str, group: str = "general", entrance: str = "east",
            hall: str = "daxiong", party_size: int = 1, request_id: str | None = None):
    body = {"slot_id": slot_id, "hall_code": hall, "entrance_code": entrance, "visitor_group": group,
            "visitor_hash": visitor_hash, "party_size": party_size, "actor": "tests"}
    if request_id:
        body["request_id"] = request_id
    return client.post("/api/temple/visits/reservations", json=body)


def test_layered_capacity_never_oversells_and_overlaps_rejected(client):
    slot_id = prepare_temple(client)
    statuses = []
    for index in range(1, 6):
        response = reserve(client, slot_id, visitor_hash=f"visitor-{index:016d}")
        assert response.status_code == 201, response.text
        statuses.append(response.json()["status"])
    assert statuses.count("pending_confirmation") == 4
    assert statuses.count("waiting") == 1
    detail = client.get(f"/api/temple/visits/slots/{slot_id}").json()
    total_layer = next(layer for layer in detail["capacities"]
                       if layer["hall_id"] is None and not layer["entrance_code"] and not layer["visitor_group"])
    assert total_layer["occupied"] == 4
    assert total_layer["available"] == 0
    waiter = next(item for item in detail["waitlist"] if item["waitlist_rank"] == 1)
    # 同一身份不能在重叠时段重复占位（候补也占身份）
    conflict = reserve(client, slot_id, visitor_hash=waiter["visitor_hash"], entrance="west")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["context"]["overlap_reservation_id"] == waiter["id"]


def test_leaf_layer_independently_caps_even_when_total_has_room(client):
    slot_id = prepare_temple(client, total_capacity=10)
    client.put(f"/api/temple/visits/slots/{slot_id}/capacities", json={"entrance_code": "north", "capacity": 1})
    first = reserve(client, slot_id, visitor_hash="north-visitor-0001", entrance="north")
    second = reserve(client, slot_id, visitor_hash="north-visitor-0002", entrance="north")
    west = reserve(client, slot_id, visitor_hash="west-visitor-00001", entrance="west")
    assert first.json()["status"] == "pending_confirmation"
    assert second.json()["status"] == "waiting"
    assert west.json()["status"] == "pending_confirmation"


def test_request_replay_returns_same_reservation_and_detects_payload_change(client):
    slot_id = prepare_temple(client)
    request_id = "request-replay-0001"
    first = reserve(client, slot_id, visitor_hash="replay-visitor-0001", request_id=request_id)
    assert first.status_code == 201
    assert first.json()["replayed"] is False
    replay = reserve(client, slot_id, visitor_hash="replay-visitor-0001", request_id=request_id)
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["id"] == first.json()["id"]
    changed = reserve(client, slot_id, visitor_hash="replay-visitor-0002", request_id=request_id)
    assert changed.status_code == 409
    confirm_request = "confirm-replay-001"
    confirmed = client.post(f"/api/temple/visits/reservations/{first.json()['id']}/confirm",
                            json={"request_id": confirm_request, "actor": "visitor"})
    assert confirmed.status_code == 200
    replay_confirm = client.post(f"/api/temple/visits/reservations/{first.json()['id']}/confirm",
                                 json={"request_id": confirm_request, "actor": "visitor"})
    assert replay_confirm.status_code == 200
    assert replay_confirm.json()["id"] == first.json()["id"]


def test_cancellation_releases_and_promotes_with_explained_rule_and_release(client):
    slot_id = prepare_temple(client)
    general_one = reserve(client, slot_id, visitor_hash="occupy-general-0001")
    reserve(client, slot_id, visitor_hash="occupy-general-0002")
    ceremony = reserve(client, slot_id, visitor_hash="occupy-ceremony-001", group="ceremony")
    elder = reserve(client, slot_id, visitor_hash="occupy-elder-000001", group="elder")
    assert general_one.status_code == 201 and ceremony.status_code == 201 and elder.status_code == 201
    waiting_general = reserve(client, slot_id, visitor_hash="wait-general-0000001")
    waiting_elder = reserve(client, slot_id, visitor_hash="wait-elder-000000001", group="elder")
    waiting_ceremony = reserve(client, slot_id, visitor_hash="wait-ceremony-000001", group="ceremony")
    for response in (waiting_general, waiting_elder, waiting_ceremony):
        assert response.status_code == 201 and response.json()["status"] == "waiting"
    cancelled_id = general_one.json()["id"]
    cancel = client.post(f"/api/temple/visits/reservations/{cancelled_id}/cancel",
                         json={"reason": "行程变更", "actor": "visitor"})
    assert cancel.status_code == 200
    # 老人在队列中优先级最高，应被这笔取消释放递补
    promoted_elder = client.get(f"/api/temple/visits/reservations/{waiting_elder.json()['id']}").json()
    assert promoted_elder["status"] == "pending_confirmation"
    assert promoted_elder["promotion"]["rule_code"] == "elder_first"
    assert promoted_elder["promotion"]["rule_label"] == "老人团队优先"
    assert promoted_elder["promotion"]["release"]["kind"] == "cancellation"
    assert promoted_elder["promotion"]["release"]["source"] == {
        "type": "reservation", "reservation_id": cancelled_id, "code": general_one.json()["code"],
    }
    promotions = client.get(f"/api/temple/visits/promotions?slot_id={slot_id}").json()["items"]
    assert promotions[0]["reservation_id"] == waiting_elder.json()["id"]
    # 普通候补主动退出不释放占用名额；再取消法会占位，法会候补按 ceremony_next 递补
    client.post(f"/api/temple/visits/reservations/{waiting_general.json()['id']}/cancel",
                json={"reason": "不等了", "actor": "visitor"})
    ceremony_cancel = client.post(f"/api/temple/visits/reservations/{ceremony.json()['id']}/cancel",
                                  json={"reason": "法会改期", "actor": "visitor"})
    assert ceremony_cancel.status_code == 200
    promoted_ceremony = client.get(f"/api/temple/visits/reservations/{waiting_ceremony.json()['id']}").json()
    assert promoted_ceremony["status"] == "pending_confirmation"
    assert promoted_ceremony["promotion"]["rule_code"] == "ceremony_next"
    assert promoted_ceremony["promotion"]["release"]["source"]["reservation_id"] == ceremony.json()["id"]
    # 剩余普通候补此前已主动退出，队列应为空；取消不产生释放与递补记录
    detail = client.get(f"/api/temple/visits/slots/{slot_id}").json()
    assert detail["waitlist"] == []
    remaining_promotions = client.get(f"/api/temple/visits/promotions?slot_id={slot_id}").json()["items"]
    assert {item["reservation_id"] for item in remaining_promotions} == {
        waiting_elder.json()["id"], waiting_ceremony.json()["id"],
    }


def test_same_entrance_and_same_hall_rules_apply_in_order(client):
    slot_id = prepare_temple(client, total_capacity=10)
    # 只开放东门 1 个、西门 1 个分层名额，总量充足以观察入口层规则
    client.put(f"/api/temple/visits/slots/{slot_id}/capacities", json={"entrance_code": "east", "capacity": 1})
    client.put(f"/api/temple/visits/slots/{slot_id}/capacities", json={"entrance_code": "west", "capacity": 1})
    east_holder = reserve(client, slot_id, visitor_hash="east-holder-000001", entrance="east")
    west_holder = reserve(client, slot_id, visitor_hash="west-holder-000001", entrance="west", hall="guanyin")
    assert east_holder.json()["status"] == "pending_confirmation"
    assert west_holder.json()["status"] == "pending_confirmation"
    same_hall_waiter = reserve(client, slot_id, visitor_hash="east-hall-waiter-1", entrance="east")
    other_hall_waiter = reserve(client, slot_id, visitor_hash="west-hall-waiter-1", entrance="east", hall="guanyin")
    assert same_hall_waiter.json()["status"] == "waiting"
    assert other_hall_waiter.json()["status"] == "waiting"
    client.post(f"/api/temple/visits/reservations/{east_holder.json()['id']}/cancel",
                json={"reason": "东门取消", "actor": "visitor"})
    promoted = client.get(f"/api/temple/visits/reservations/{same_hall_waiter.json()['id']}").json()
    assert promoted["status"] == "pending_confirmation"
    assert promoted["promotion"]["rule_code"] == "same_entrance"
    still_waiting = client.get(f"/api/temple/visits/reservations/{other_hall_waiter.json()['id']}").json()
    assert still_waiting["status"] == "waiting"


def test_expired_confirmation_releases_and_promotes_with_fixed_clock(client):
    slot_id = prepare_temple(client)
    holder = reserve(client, slot_id, visitor_hash="deadline-holder-0001")
    for index in range(2, 5):
        assert reserve(client, slot_id, visitor_hash=f"deadline-fill-{index:010d}").status_code == 201
    waiter = reserve(client, slot_id, visitor_hash="deadline-waiter-0001")
    assert waiter.json()["status"] == "waiting"
    deadline = datetime.fromisoformat(holder.json()["confirm_deadline"].replace("Z", "+00:00"))
    service = VisitBookingService(get_connection(), FrozenClock(deadline + timedelta(seconds=1)))
    result = service.expire_unconfirmed("tests")
    assert holder.json()["id"] in result["expired"]
    assert waiter.json()["id"] in result["promoted"]
    expired_detail = client.get(f"/api/temple/visits/reservations/{holder.json()['id']}").json()
    assert expired_detail["status"] == "expired"
    promoted_detail = client.get(f"/api/temple/visits/reservations/{waiter.json()['id']}").json()
    assert promoted_detail["status"] == "pending_confirmation"
    assert promoted_detail["promotion"]["release"]["kind"] == "confirmation_timeout"
    assert promoted_detail["promotion"]["release"]["source"]["reservation_id"] == holder.json()["id"]
    # 对已过期预约再确认必须失败，但释放与递补不回滚
    late = client.post(f"/api/temple/visits/reservations/{holder.json()['id']}/confirm", json={"actor": "visitor"})
    assert late.status_code == 409
    assert late.json()["error"]["context"]["status"] == "expired"
    # 重复执行过期扫描是幂等的
    again = VisitBookingService(get_connection(), FrozenClock(deadline + timedelta(seconds=60))).expire_unconfirmed("tests")
    assert again["expired"] == []


def test_waitlist_rank_survives_restart_and_statuses_persist(client):
    slot_id = prepare_temple(client)
    created = []
    for index in range(1, 7):
        response = reserve(client, slot_id, visitor_hash=f"restart-visitor-{index:010d}")
        created.append(response.json())
    waiters = [item for item in created if item["status"] == "waiting"]
    assert len(waiters) == 2
    close_connection()
    service = VisitBookingService()
    for index, waiter in enumerate(waiters, start=1):
        detail = service.reservation_detail(waiter["id"])
        assert detail["status"] == "waiting"
        assert detail["waitlist_rank"] == index
    slot = service.slot_detail(slot_id)
    assert [row["id"] for row in slot["waitlist"]] == [waiter["id"] for waiter in waiters]


def test_concurrent_reservations_keep_total_within_capacity(client):
    slot_id = prepare_temple(client)
    results: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            close_connection()
            service = VisitBookingService()
            result = service.reserve({
                "slot_id": slot_id, "hall_code": "daxiong", "entrance_code": "east",
                "visitor_group": "general", "visitor_hash": f"parallel-visitor-{index:010d}",
                "party_size": 1, "actor": "tests",
            })
            with lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - 仅用于暴露并发问题
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(1, 13)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    occupying = [item for item in results if item["status"] in {"pending_confirmation", "confirmed"}]
    waiting = [item for item in results if item["status"] == "waiting"]
    assert len(occupying) == 4
    assert len(waiting) == 8
    assert sorted(item["waitlist_rank"] for item in waiting) == list(range(1, 9))
    visitor_hashes = [item["visitor_hash"] for item in results]
    assert len(visitor_hashes) == len(set(visitor_hashes))


def test_concurrent_confirmation_keeps_single_consistent_state(client):
    slot_id = prepare_temple(client)
    reservation = reserve(client, slot_id, visitor_hash="parallel-confirm-0001").json()
    outcomes: list[str] = []
    lock = threading.Lock()

    def confirm() -> None:
        close_connection()
        service = VisitBookingService()
        detail = service.confirm_reservation(reservation["id"], {"actor": "visitor"})
        with lock:
            outcomes.append(detail["status"])

    threads = [threading.Thread(target=confirm) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes == ["confirmed"] * 6
    close_connection()
    detail = VisitBookingService().reservation_detail(reservation["id"])
    assert detail["status"] == "confirmed"
    assert len([event for event in detail["events"] if event["event_type"] == "confirmed"]) == 1


def test_capacity_increase_promotes_waiters_via_adjustment_release(client):
    slot_id = prepare_temple(client)
    for index in range(1, 5):
        assert reserve(client, slot_id, visitor_hash=f"grow-occupy-{index:010d}").status_code == 201
    waiter = reserve(client, slot_id, visitor_hash="grow-waiter-000001")
    assert waiter.json()["status"] == "waiting"
    adjusted = client.put(f"/api/temple/visits/slots/{slot_id}/capacities",
                          json={"capacity": 5, "reason": "追加开放名额", "actor": "admin"})
    assert adjusted.status_code == 200
    detail = client.get(f"/api/temple/visits/reservations/{waiter.json()['id']}").json()
    assert detail["status"] == "pending_confirmation"
    assert detail["promotion"]["release"]["kind"] == "capacity_adjustment"
    # 不能把名额收紧到占用以下
    refused = client.put(f"/api/temple/visits/slots/{slot_id}/capacities", json={"capacity": 2})
    assert refused.status_code == 409
    assert refused.json()["error"]["context"]["occupied"] == 5


def test_closure_cancels_reservations_declines_waitlist_and_explains_promotion(client):
    slot_id = prepare_temple(client)
    # 观音殿 1 个占位 + 大雄宝殿 3 个占位占满总量；观音殿候补 1 人、大雄宝殿候补 1 人
    guanyin = reserve(client, slot_id, visitor_hash="closure-guanyin-001", hall="guanyin", entrance="west")
    fills = []
    for index in range(1, 4):
        fills.append(reserve(client, slot_id, visitor_hash=f"closure-fill-{index:010d}"))
        assert fills[-1].status_code == 201
    assert guanyin.json()["status"] == "pending_confirmation"
    guanyin_waiter = reserve(client, slot_id, visitor_hash="closure-guanyin-wait", hall="guanyin", entrance="west")
    daxiong_waiter = reserve(client, slot_id, visitor_hash="closure-daxiong-wait")
    assert guanyin_waiter.json()["status"] == "waiting"
    assert daxiong_waiter.json()["status"] == "waiting"
    base = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    restoration = TempleRestorationService(get_connection(), FrozenClock(base))
    restoration.create_closure({
        "temple_code": "fahui-temple", "hall_code": "daxiong", "code": "daxiong-closure",
        "reason": "梁架临时检查", "starts_at": "2026-10-01T08:30:00Z", "ends_at": "2026-10-01T11:00:00Z",
        "drain_mode": "cancel_active", "actor": "operator",
    })
    TempleRestorationService(get_connection(), FrozenClock(base + timedelta(hours=1))).activate_due_closure("scheduler")
    applied = VisitBookingService(get_connection(), FrozenClock(base + timedelta(hours=1))).apply_closure_changes("scheduler")
    assert {item.json()["id"] for item in fills} <= set(applied["released"])
    assert daxiong_waiter.json()["id"] in applied["declined"]
    assert guanyin_waiter.json()["id"] in applied["promoted"]
    for holder in fills:
        cancelled = client.get(f"/api/temple/visits/reservations/{holder.json()['id']}").json()
        assert cancelled["status"] == "cancelled"
    declined = client.get(f"/api/temple/visits/reservations/{daxiong_waiter.json()['id']}").json()
    assert declined["status"] == "declined"
    promoted = client.get(f"/api/temple/visits/reservations/{guanyin_waiter.json()['id']}").json()
    assert promoted["status"] == "pending_confirmation"
    assert promoted["promotion"]["release"]["kind"] == "closure"
    assert promoted["promotion"]["release"]["source"]["code"] in {holder.json()["code"] for holder in fills}
    assert promoted["promotion"]["release"]["source"]["type"] == "reservation"
    # 封闭期间大雄宝殿不能新建预约
    blocked = reserve(client, slot_id, visitor_hash="closure-blocked-001")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["closure_code"] == "daxiong-closure"
    # 再执行一次封闭处理必须幂等
    again = VisitBookingService(get_connection(), FrozenClock(base + timedelta(hours=2))).apply_closure_changes("scheduler")
    assert again["released"] == [] and again["declined"] == [] and again["promoted"] == []


def test_closure_drain_modes_keep_or_cancel_distinct_states(client):
    prepare_temple(client)
    base = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

    def fresh_slot(hour: int) -> int:
        created = client.post(
            "/api/temple/visits/slots",
            json={"temple_code": "fahui-temple",
                  "starts_at": f"2026-10-01T{hour:02d}:00:00Z",
                  "ends_at": f"2026-10-01T{hour + 1:02d}:00:00Z", "actor": "tests"},
        )
        slot_id = created.json()["id"]
        for body in (
            {"capacity": 4}, {"hall_code": "daxiong", "capacity": 100},
            {"hall_code": "guanyin", "capacity": 100}, {"entrance_code": "west", "capacity": 100},
        ):
            client.put(f"/api/temple/visits/slots/{slot_id}/capacities", json=body)
        return slot_id

    def run_case(drain_mode: str, hour: int):
        slot_id = fresh_slot(hour)
        pending = reserve(client, slot_id, visitor_hash=f"dm-{drain_mode}-pending")
        confirmed_response = reserve(client, slot_id, visitor_hash=f"dm-{drain_mode}-confirm")
        client.post(f"/api/temple/visits/reservations/{confirmed_response.json()['id']}/confirm",
                    json={"actor": "visitor"})
        reserve(client, slot_id, visitor_hash=f"dm-{drain_mode}-fill-1", hall="guanyin", entrance="west")
        reserve(client, slot_id, visitor_hash=f"dm-{drain_mode}-fill-2", hall="guanyin", entrance="west")
        waiter = reserve(client, slot_id, visitor_hash=f"dm-{drain_mode}-waiter", hall="guanyin", entrance="west")
        assert waiter.json()["status"] == "waiting"
        code = f"closure-{drain_mode}-{hour}"
        TempleRestorationService(get_connection(), FrozenClock(base)).create_closure({
            "temple_code": "fahui-temple", "hall_code": "daxiong", "code": code,
            "reason": "法物流通处临时封闭",
            "starts_at": f"2026-10-01T{hour:02d}:30:00Z",
            "ends_at": f"2026-10-01T{hour + 1:02d}:30:00Z",
            "drain_mode": drain_mode, "actor": "operator",
        })
        clock = FrozenClock(base.replace(hour=hour, minute=45))
        TempleRestorationService(get_connection(), clock).activate_due_closure("scheduler")
        applied = VisitBookingService(get_connection(), clock).apply_closure_changes("scheduler")
        pending_status = client.get(f"/api/temple/visits/reservations/{pending.json()['id']}").json()["status"]
        confirmed_status = client.get(f"/api/temple/visits/reservations/{confirmed_response.json()['id']}").json()["status"]
        waiter_status = client.get(f"/api/temple/visits/reservations/{waiter.json()['id']}").json()["status"]
        return applied, pending_status, confirmed_status, waiter_status

    applied, pending_status, confirmed_status, waiter_status = run_case("block_new", 12)
    # block_new 保留大雄宝殿全部存量；观音殿候补不在窗口作用域内，保持候补
    assert (pending_status, confirmed_status, waiter_status) == ("pending_confirmation", "confirmed", "waiting")
    assert applied["released"] == [] and applied["declined"] == [] and applied["promoted"] == []

    applied, pending_status, confirmed_status, waiter_status = run_case("finish_active", 14)
    # finish_active 取消未确认占位并释放总量，观音殿候补被这笔封闭释放递补
    assert (pending_status, confirmed_status, waiter_status) == ("cancelled", "confirmed", "pending_confirmation")
    assert applied["released"] and applied["promoted"] == [applied["promoted"][0]]
    promoted = client.get(f"/api/temple/visits/reservations/{applied['promoted'][0]}").json()
    assert promoted["promotion"]["release"]["kind"] == "closure"

    applied, pending_status, confirmed_status, waiter_status = run_case("cancel_active", 16)
    assert (pending_status, confirmed_status, waiter_status) == ("cancelled", "cancelled", "pending_confirmation")
    assert len(applied["released"]) == 2 and len(applied["promoted"]) == 1


def test_slot_close_stops_new_bookings_and_declines_waiters(client):
    slot_id = prepare_temple(client)
    for index in range(1, 5):
        assert reserve(client, slot_id, visitor_hash=f"close-occupy-{index:010d}").status_code == 201
    waiter = reserve(client, slot_id, visitor_hash="close-waiter-00001")
    assert waiter.json()["status"] == "waiting"
    closed = client.post(f"/api/temple/visits/slots/{slot_id}/close",
                         json={"reason": "法会提前结束清场", "actor": "admin"})
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"
    assert closed.json()["declined"] == [waiter.json()["id"]]
    declined = client.get(f"/api/temple/visits/reservations/{waiter.json()['id']}").json()
    assert declined["status"] == "declined"
    blocked = reserve(client, slot_id, visitor_hash="close-blocked-0001")
    assert blocked.status_code == 409
    # 已存在的已占位预约不被时段关闭自动取消（由取消或封闭窗口流程处理）
    holders = client.get(f"/api/temple/visits/slots/{slot_id}").json()
    assert holders["waitlist"] == []


def test_promotion_log_reports_rule_and_release_for_each_fill(client):
    slot_id = prepare_temple(client)
    holders = [reserve(client, slot_id, visitor_hash=f"log-occupy-{index:010d}").json() for index in range(1, 5)]
    waiter = reserve(client, slot_id, visitor_hash="log-waiter-0000001", hall="guanyin", entrance="west")
    assert waiter.json()["status"] == "waiting"
    client.post(f"/api/temple/visits/reservations/{holders[0]['id']}/cancel",
                json={"reason": "主动取消", "actor": "visitor"})
    log = client.get(f"/api/temple/visits/promotions").json()["items"]
    assert len(log) == 1
    entry = log[0]
    assert entry["slot_id"] == slot_id
    assert entry["rule_code"] == "fifo"
    assert entry["rule_label"] == "候补队列先到先得"
    assert entry["release"]["kind"] == "cancellation"
    assert entry["release"]["quantity"] == 1
    assert entry["release"]["source"]["code"] == holders[0]["code"]
    assert entry["rank_before"] == 1 and entry["queue_depth"] == 1
