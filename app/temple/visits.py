"""分时入寺预约、分层名额与持久化候补服务。

名额可以按时段、殿堂（含寺院总量层）、入口与人群分层配置；所有占用（含
待确认占位）都在同一个即时事务内计数与落盘，保证总量不超卖。取消、确认
超时与封闭窗口都会生成可追溯的释放记录（capacity_releases），并按固定的
优先规则递补候补：老人团队 → 预约法会人员 → 同入口 → 同殿堂 → 先到先得。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import request_fingerprint
from app.database import get_connection, transaction
from app.services.idempotency import IdempotencyService
from app.temple.schema import ensure_temple_schema

GROUP_RANK = {"elder": 0, "ceremony": 1, "general": 2}
PROMOTION_RULES = {
    "elder_first": "老人团队优先",
    "ceremony_next": "预约法会人员优先",
    "same_entrance": "同入口优先递补",
    "same_hall": "同殿堂优先递补",
    "fifo": "候补队列先到先得",
}
DEFAULT_CONFIRM_WINDOW_SECONDS = 900
WILDCARD_HALL = -1


class VisitBookingService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()
        self.idempotency = IdempotencyService(self.connection, self.clock)

    # ------------------------------------------------------------------ 时段

    def create_slot(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        try:
            start = to_storage(from_storage(payload["starts_at"]))
            end = to_storage(from_storage(payload["ends_at"]))
        except (TypeError, ValueError) as exc:
            raise ValidationError("时段时间格式不正确") from exc
        if end <= start:
            raise ValidationError("时段结束时间必须晚于开始时间")
        slot_date = start[:10]
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO visit_slots(temple_id,slot_date,slot_start,slot_end,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (temple["id"], slot_date, start, end, payload.get("note", ""), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("同一时段窗口已存在") from exc
            self._event(connection, "visit_slot", cursor.lastrowid, "created", payload.get("actor", "admin"), {}, now)
            return self.slot_detail(cursor.lastrowid, connection)

    def list_slots(self, temple_code: str | None = None, slot_date: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if temple_code:
            temple = self._temple(temple_code)
            clauses.append("s.temple_id=?")
            params.append(temple["id"])
        if slot_date:
            clauses.append("s.slot_date=?")
            params.append(slot_date)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            "SELECT s.*,n.code AS temple_code,n.name AS temple_name FROM visit_slots s "
            "JOIN temple_sites n ON n.id=s.temple_id" + where + " ORDER BY s.slot_start,s.id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def slot_detail(self, slot_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT s.*,n.code AS temple_code,n.name AS temple_name FROM visit_slots s "
            "JOIN temple_sites n ON n.id=s.temple_id WHERE s.id=?",
            (slot_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("入寺时段不存在")
        result = dict(row)
        capacities = connection.execute(
            "SELECT c.*,h.code AS hall_code,h.name AS hall_name FROM visit_slot_capacities c "
            "LEFT JOIN worship_halls h ON h.id=c.hall_id WHERE c.slot_id=? ORDER BY c.id",
            (slot_id,),
        ).fetchall()
        layers = []
        for capacity_row in capacities:
            item = dict(capacity_row)
            item["occupied"] = self._layer_occupied(connection, slot_id, item["hall_id"], item["entrance_code"], item["visitor_group"])
            item["available"] = max(0, int(item["capacity"]) - item["occupied"])
            layers.append(item)
        result["capacities"] = layers
        waitlist = connection.execute(
            "SELECT r.*,h.code AS hall_code FROM visit_reservations r LEFT JOIN worship_halls h ON h.id=r.hall_id "
            "WHERE r.slot_id=? AND r.status='waiting' ORDER BY r.hall_key,r.entrance_code,r.visitor_group,r.waitlist_rank",
            (slot_id,),
        ).fetchall()
        result["waitlist"] = [dict(row) for row in waitlist]
        result["events"] = self._events(connection, "visit_slot", slot_id)
        return result

    # ------------------------------------------------------------------ 名额

    def close_slot(self, slot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """管理性关闭整个时段：停止新预约并谢绝全部候补（候补未占名额，无需释放）。"""
        with transaction(immediate=True) as connection:
            slot = connection.execute("SELECT * FROM visit_slots WHERE id=?", (slot_id,)).fetchone()
            if slot is None:
                raise NotFoundError("入寺时段不存在")
            now = to_storage(self.clock.now())
            actor = payload.get("actor", "admin")
            reason = payload.get("reason") or "时段关闭"
            declined: list[int] = []
            if slot["state"] != "closed":
                connection.execute(
                    "UPDATE visit_slots SET state='closed',note=?,version=version+1,updated_at=? WHERE id=?",
                    (reason, now, slot_id),
                )
                waiters = connection.execute(
                    "SELECT * FROM visit_reservations WHERE slot_id=? AND status='waiting' ORDER BY id", (slot_id,)
                ).fetchall()
                for reservation in waiters:
                    connection.execute(
                        "UPDATE visit_reservations SET status='declined',decided_at=?,end_reason=?,version=version+1,updated_at=? WHERE id=?",
                        (now, f"时段关闭：{reason}", now, reservation["id"]),
                    )
                    self._resequence_waitlist(connection, reservation)
                    declined.append(reservation["id"])
                    self._event(connection, "visit_reservation", reservation["id"], "declined", actor,
                                {"slot_closed": True, "reason": reason}, now, reservation_id=reservation["id"])
                self._event(connection, "visit_slot", slot_id, "closed", actor, {"reason": reason}, now)
            result = self.slot_detail(slot_id, connection)
            result["declined"] = declined
            return result

    def set_capacity(self, slot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            slot = connection.execute("SELECT * FROM visit_slots WHERE id=?", (slot_id,)).fetchone()
            if slot is None:
                raise NotFoundError("入寺时段不存在")
            hall_id = self._resolve_hall_id(connection, slot["temple_id"], payload.get("hall_code"))
            entrance = (payload.get("entrance_code") or "").strip()
            group = payload.get("visitor_group") or ""
            quantity = int(payload["capacity"])
            if quantity < 0:
                raise ValidationError("名额不能为负数")
            now = to_storage(self.clock.now())
            existing = connection.execute(
                "SELECT * FROM visit_slot_capacities WHERE slot_id=? AND hall_key=? AND entrance_code=? AND visitor_group=?",
                (slot_id, hall_id if hall_id is not None else WILDCARD_HALL, entrance, group),
            ).fetchone()
            if existing is not None and quantity < int(existing["capacity"]):
                occupied = self._layer_occupied(connection, slot_id, hall_id, entrance, group)
                if occupied > quantity:
                    raise ConflictError(
                        "名额不能收紧到当前占用以下",
                        context={"capacity": quantity, "occupied": occupied},
                    )
            try:
                connection.execute(
                    "INSERT INTO visit_slot_capacities(slot_id,hall_id,entrance_code,visitor_group,capacity,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (slot_id, hall_id, entrance, group, quantity, now, now),
                )
            except sqlite3.IntegrityError:
                connection.execute(
                    "UPDATE visit_slot_capacities SET capacity=?,version=version+1,updated_at=? "
                    "WHERE slot_id=? AND hall_key=? AND entrance_code=? AND visitor_group=?",
                    (quantity, now, slot_id, hall_id if hall_id is not None else WILDCARD_HALL, entrance, group),
                )
            self._event(
                connection, "visit_slot", slot_id, "capacity_set", payload.get("actor", "admin"),
                {"hall_code": payload.get("hall_code"), "entrance_code": entrance, "visitor_group": group, "capacity": quantity,
                 "previous_capacity": int(existing["capacity"]) if existing else None},
                now,
            )
            releases: list[int] = []
            if existing is not None and quantity > int(existing["capacity"]):
                release_id = self._write_release(
                    connection, slot, hall_id, entrance, group, quantity - int(existing["capacity"]),
                    kind="capacity_adjustment", reason=payload.get("reason") or "名额上调", actor=payload.get("actor", "admin"), now=now,
                )
                releases.append(release_id)
            for release_id in releases:
                self._promote_from_release(connection, slot, release_id, now)
            return self.slot_detail(slot_id, connection)

    # ------------------------------------------------------------------ 预约

    def reserve(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id")
        fingerprint_payload = {key: value for key, value in payload.items() if key != "request_id"}
        with transaction(immediate=True) as connection:
            if request_id:
                stored = self.idempotency.lookup("visit.reserve", request_id, fingerprint_payload)
                if stored is not None:
                    return {**stored.body, "replayed": True}
            result = self._reserve_inner(connection, payload, now=to_storage(self.clock.now()))
            body = dict(result)
            if request_id:
                self.idempotency.save("visit.reserve", request_id, fingerprint_payload, body, 201)
            body["replayed"] = False
            return body

    def _reserve_inner(self, connection: sqlite3.Connection, payload: dict[str, Any], *, now: str) -> dict[str, Any]:
        slot = connection.execute(
            "SELECT s.*,n.code AS temple_code FROM visit_slots s JOIN temple_sites n ON n.id=s.temple_id WHERE s.id=?",
            (payload["slot_id"],),
        ).fetchone()
        if slot is None:
            raise NotFoundError("入寺时段不存在")
        if slot["state"] != "open":
            raise ConflictError("该时段已关闭入寺预约")
        hall = connection.execute(
            "SELECT * FROM worship_halls WHERE temple_id=? AND code=?",
            (slot["temple_id"], payload["hall_code"]),
        ).fetchone()
        if hall is None:
            raise NotFoundError("预约殿堂不存在")
        entrance = (payload.get("entrance_code") or "").strip()
        if not entrance:
            raise ValidationError("入寺入口不能为空")
        group = payload.get("visitor_group", "general")
        if group not in GROUP_RANK:
            raise ValidationError("人群类型必须是 elder、ceremony 或 general")
        party_size = int(payload.get("party_size", 1))
        visitor_hash = payload["visitor_hash"]
        overlap = connection.execute(
            "SELECT r.id,s.slot_start,s.slot_end FROM visit_reservations r JOIN visit_slots s ON s.id=r.slot_id "
            "WHERE r.visitor_hash=? AND r.status IN ('pending_confirmation','confirmed','waiting') "
            "AND s.slot_start<? AND s.slot_end>?",
            (visitor_hash, slot["slot_end"], slot["slot_start"]),
        ).fetchone()
        if overlap is not None:
            raise ConflictError(
                "同一身份在重叠时段已有占位",
                context={"overlap_reservation_id": overlap["id"]},
            )
        closure = self._blocking_closure(connection, slot["temple_id"], hall["id"], slot["slot_start"], slot["slot_end"])
        if closure is not None:
            raise ConflictError("该时段殿堂处于封闭窗口，不能新建预约", context={"closure_code": closure["code"]})
        layers = self._constraint_layers(connection, slot["id"], hall["id"], entrance, group)
        if not layers:
            raise ConflictError("该时段尚未配置可售名额")
        fits = all(int(layer["capacity"]) - self._layer_occupied(
            connection, slot["id"], layer["hall_id"], layer["entrance_code"], layer["visitor_group"]
        ) >= party_size for layer in layers)
        confirm_window = int(payload.get("confirm_window_seconds", DEFAULT_CONFIRM_WINDOW_SECONDS))
        code = self._new_code(connection, "V")
        token = secrets.token_urlsafe(24)
        deadline = to_storage(from_storage(now) + timedelta(seconds=confirm_window))
        if fits:
            status, rank, queued_at = "pending_confirmation", None, None
        else:
            status = "waiting"
            rank_row = connection.execute(
                "SELECT COALESCE(MAX(waitlist_rank),0)+1 FROM visit_reservations WHERE slot_id=? AND hall_key=? AND entrance_code=? AND visitor_group=? AND status='waiting'",
                (slot["id"], hall["id"], entrance, group),
            ).fetchone()
            rank, queued_at = int(rank_row[0]), now
        cursor = connection.execute(
            "INSERT INTO visit_reservations(code,temple_id,slot_id,hall_id,entrance_code,visitor_group,visitor_hash,party_size,contact,"
            "status,confirm_token,confirm_deadline,waitlist_rank,queued_at,request_id,request_digest,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, slot["temple_id"], slot["id"], hall["id"], entrance, group, visitor_hash, party_size, payload.get("contact", ""),
             status, token, deadline, rank, queued_at, payload.get("request_id"),
             request_fingerprint({k: v for k, v in payload.items() if k != "request_id"}), now, now),
        )
        reservation_id = cursor.lastrowid
        self._event(
            connection, "visit_reservation", reservation_id, "reserved", payload.get("actor", "visitor"),
            {"status": status, "party_size": party_size, "waitlist_rank": rank}, now,
            reservation_id=reservation_id,
        )
        return self.reservation_detail(reservation_id, connection)

    def confirm_reservation(self, reservation_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        request_id = payload.get("request_id")
        timeout_context: dict[str, Any] | None = None
        with transaction(immediate=True) as connection:
            if request_id:
                stored = self.idempotency.lookup("visit.confirm", request_id, {"reservation_id": reservation_id})
                if stored is not None:
                    return {**stored.body, "replayed": True}
            now = to_storage(self.clock.now())
            reservation = self._locked_reservation(connection, reservation_id)
            if reservation["status"] == "confirmed":
                body = self.reservation_detail(reservation_id, connection)
                if request_id:
                    self.idempotency.save("visit.confirm", request_id, {"reservation_id": reservation_id}, body, 200)
                return {**body, "replayed": False}
            if reservation["status"] != "pending_confirmation":
                raise ConflictError(f"当前状态（{reservation['status']}）不能确认预约",
                                    context={"reservation_id": reservation_id, "status": reservation["status"]})
            if reservation["confirm_deadline"] <= now:
                # 释放与递补必须先随事务提交，再在事务外返回冲突结果。
                release_id = self._expire_reservation(connection, reservation, "确认超时（确认请求到达时已过截止时间）", now,
                                                      kind="confirmation_timeout", actor=payload.get("actor", "visitor"))
                slot = connection.execute("SELECT * FROM visit_slots WHERE id=?", (reservation["slot_id"],)).fetchone()
                self._promote_from_release(connection, slot, release_id, now)
                timeout_context = {"reservation_id": reservation_id, "status": "expired", "capacity_release_id": release_id}
            else:
                connection.execute(
                    "UPDATE visit_reservations SET status='confirmed',confirmed_at=?,decided_at=?,version=version+1,updated_at=? WHERE id=?",
                    (now, now, now, reservation_id),
                )
                self._event(connection, "visit_reservation", reservation_id, "confirmed", payload.get("actor", "visitor"), {}, now,
                            reservation_id=reservation_id)
                body = self.reservation_detail(reservation_id, connection)
                if request_id:
                    self.idempotency.save("visit.confirm", request_id, {"reservation_id": reservation_id}, body, 200)
                body["replayed"] = False
                return body
        raise ConflictError("预约确认已超过截止时间，名额已释放并按候补顺序递补", context=timeout_context)

    def cancel_reservation(self, reservation_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id")
        with transaction(immediate=True) as connection:
            if request_id:
                stored = self.idempotency.lookup("visit.cancel", request_id, {"reservation_id": reservation_id})
                if stored is not None:
                    return {**stored.body, "replayed": True}
            now = to_storage(self.clock.now())
            reservation = self._locked_reservation(connection, reservation_id)
            if reservation["status"] in {"cancelled", "expired", "declined"}:
                body = self.reservation_detail(reservation_id, connection)
                if request_id:
                    self.idempotency.save("visit.cancel", request_id, {"reservation_id": reservation_id}, body, 200)
                return {**body, "replayed": False}
            actor = payload.get("actor", "visitor")
            reason = payload.get("reason") or "访客主动取消"
            if reservation["status"] == "waiting":
                connection.execute(
                    "UPDATE visit_reservations SET status='cancelled',cancelled_at=?,decided_at=?,end_reason=?,version=version+1,updated_at=? WHERE id=?",
                    (now, now, reason, now, reservation_id),
                )
                self._resequence_waitlist(connection, reservation)
                self._event(connection, "visit_reservation", reservation_id, "cancelled", actor, {"reason": reason}, now,
                            reservation_id=reservation_id)
            else:
                connection.execute(
                    "UPDATE visit_reservations SET status='cancelled',cancelled_at=?,decided_at=?,end_reason=?,version=version+1,updated_at=? WHERE id=?",
                    (now, now, reason, now, reservation_id),
                )
                release_id = self._record_release_for_reservation(connection, reservation, "cancellation", reason, actor, now)
                self._event(connection, "visit_reservation", reservation_id, "cancelled", actor, {"reason": reason}, now,
                            reservation_id=reservation_id)
                slot = connection.execute("SELECT * FROM visit_slots WHERE id=?", (reservation["slot_id"],)).fetchone()
                self._promote_from_release(connection, slot, release_id, now)
            body = self.reservation_detail(reservation_id, connection)
            if request_id:
                self.idempotency.save("visit.cancel", request_id, {"reservation_id": reservation_id}, body, 200)
            body["replayed"] = False
            return body

    def reservation_detail(self, reservation_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT r.*,n.code AS temple_code,h.code AS hall_code,s.slot_start,s.slot_end,s.slot_date "
            "FROM visit_reservations r JOIN temple_sites n ON n.id=r.temple_id "
            "JOIN worship_halls h ON h.id=r.hall_id JOIN visit_slots s ON s.id=r.slot_id WHERE r.id=?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("预约不存在")
        result = dict(row)
        promotion = connection.execute(
            "SELECT p.*,x.release_kind,x.quantity AS release_quantity,x.reason AS release_reason,"
            "x.source_reservation_id,x.closure_window_id,rc.code AS release_reservation_code,w.code AS closure_code "
            "FROM waitlist_promotions p LEFT JOIN capacity_releases x ON x.id=p.capacity_release_id "
            "LEFT JOIN visit_reservations rc ON rc.id=x.source_reservation_id "
            "LEFT JOIN hall_closure_windows w ON w.id=x.closure_window_id WHERE p.reservation_id=?",
            (reservation_id,),
        ).fetchone()
        result["promotion"] = self._promotion_view(promotion)
        result["events"] = self._events(connection, "visit_reservation", reservation_id)
        return result

    def reservation_by_token(self, confirm_token: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT id FROM visit_reservations WHERE confirm_token=?", (confirm_token,)).fetchone()
        if row is None:
            raise NotFoundError("预约确认凭证不存在")
        return self.reservation_detail(row["id"])

    # ------------------------------------------------------------ 过期与封闭

    def expire_unconfirmed(self, actor: str = "visit-confirmation-reaper") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        expired: list[int] = []
        promoted: list[int] = []
        with transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT * FROM visit_reservations WHERE status='pending_confirmation' AND confirm_deadline<=? ORDER BY id",
                (now,),
            ).fetchall()
            for reservation in rows:
                release_id = self._expire_reservation(connection, reservation, "确认超时未确认", now,
                                                      kind="confirmation_timeout", actor=actor)
                expired.append(reservation["id"])
                slot = connection.execute("SELECT * FROM visit_slots WHERE id=?", (reservation["slot_id"],)).fetchone()
                self._promote_from_release(connection, slot, release_id, now)
                promoted.extend(row["reservation_id"] for row in connection.execute(
                    "SELECT reservation_id FROM waitlist_promotions WHERE capacity_release_id=?", (release_id,)
                ).fetchall())
        return {"expired": expired, "promoted": promoted, "checked_at": now}

    def apply_closure_changes(self, actor: str = "closure-scheduler") -> dict[str, Any]:
        """对所有已激活封闭窗口收紧相关时段名额并触发候补递补。重复执行幂等。"""
        now = to_storage(self.clock.now())
        affected: list[int] = []
        declined: list[int] = []
        released: list[int] = []
        promoted: list[int] = []
        with transaction(immediate=True) as connection:
            windows = connection.execute(
                "SELECT * FROM hall_closure_windows WHERE state='active' ORDER BY id"
            ).fetchall()
            # 阶段一：全部窗口完成谢绝候补与取消占位，收集释放与所属时段。
            # 阶段二：待封闭状态全部落盘后再统一递补，确保被封闭殿堂的候补不会被他窗释放误提升。
            pending_promotions: list[tuple[sqlite3.Row, int]] = []
            for window in windows:
                slots = connection.execute(
                    "SELECT * FROM visit_slots WHERE temple_id=? AND slot_start<? AND slot_end>? ORDER BY id",
                    (window["temple_id"], window["ends_at"], window["starts_at"]),
                ).fetchall()
                for slot in slots:
                    scope_sql = "slot_id=?"
                    params: list[Any] = [slot["id"]]
                    if window["hall_id"] is not None:
                        scope_sql += " AND hall_id=?"
                        params.append(window["hall_id"])
                    reservations = connection.execute(
                        f"SELECT * FROM visit_reservations WHERE {scope_sql} AND status IN "
                        "('pending_confirmation','confirmed','waiting') ORDER BY id",
                        params,
                    ).fetchall()
                    if not reservations:
                        continue
                    affected.append(slot["id"])
                    for reservation in reservations:
                        if reservation["status"] == "waiting":
                            # 任何封闭模式下候补都不再有意义：谢绝且不产生释放（候补本就未占名额）。
                            connection.execute(
                                "UPDATE visit_reservations SET status='declined',decided_at=?,end_reason=?,version=version+1,updated_at=? WHERE id=?",
                                (now, f"殿堂封闭窗口 {window['code']} 谢绝候补", now, reservation["id"]),
                            )
                            self._resequence_waitlist(connection, reservation)
                            declined.append(reservation["id"])
                            self._event(connection, "visit_reservation", reservation["id"], "declined", actor,
                                        {"closure_code": window["code"], "drain_mode": window["drain_mode"]}, now,
                                        reservation_id=reservation["id"])
                            continue
                        # block_new 保留全部存量；finish_active 只保留已确认（未确认占位取消释放）；
                        # cancel_active 取消全部已占位预约。
                        if window["drain_mode"] == "block_new":
                            continue
                        if reservation["status"] == "confirmed" and window["drain_mode"] == "finish_active":
                            continue
                        connection.execute(
                            "UPDATE visit_reservations SET status='cancelled',cancelled_at=?,decided_at=?,end_reason=?,version=version+1,updated_at=? WHERE id=?",
                            (now, now, f"殿堂封闭窗口 {window['code']}", now, reservation["id"]),
                        )
                        release_id = self._write_release(
                            connection, slot, reservation["hall_id"], reservation["entrance_code"],
                            reservation["visitor_group"], int(reservation["party_size"]),
                            kind="closure", reason=f"封闭窗口 {window['code']}", actor=actor, now=now,
                            source_reservation_id=reservation["id"], closure_window_id=window["id"],
                        )
                        pending_promotions.append((slot, release_id))
                        released.append(reservation["id"])
                        self._event(connection, "visit_reservation", reservation["id"], "cancelled_by_closure",
                                    actor, {"closure_code": window["code"], "drain_mode": window["drain_mode"]}, now,
                                    reservation_id=reservation["id"])
            for slot, release_id in pending_promotions:
                self._promote_from_release(connection, slot, release_id, now)
                promoted.extend(row["reservation_id"] for row in connection.execute(
                    "SELECT reservation_id FROM waitlist_promotions WHERE capacity_release_id=?", (release_id,)
                ).fetchall())
        return {"slots": sorted(set(affected)), "declined": declined, "released": released, "promoted": promoted, "applied_at": now}

    # ------------------------------------------------------------ 管理查询

    def promotion_log(self, slot_id: int | None = None, reservation_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if slot_id is not None:
            clauses.append("p.slot_id=?")
            params.append(slot_id)
        if reservation_id is not None:
            clauses.append("p.reservation_id=?")
            params.append(reservation_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 500)))
        rows = self.connection.execute(
            "SELECT p.*,x.release_kind,x.quantity AS release_quantity,x.reason AS release_reason,"
            "x.source_reservation_id,x.closure_window_id,rc.code AS release_reservation_code,w.code AS closure_code,"
            "r.code AS reservation_code,r.visitor_group,r.entrance_code,h.code AS hall_code "
            "FROM waitlist_promotions p LEFT JOIN capacity_releases x ON x.id=p.capacity_release_id "
            "LEFT JOIN visit_reservations rc ON rc.id=x.source_reservation_id "
            "LEFT JOIN hall_closure_windows w ON w.id=x.closure_window_id "
            "JOIN visit_reservations r ON r.id=p.reservation_id "
            "LEFT JOIN worship_halls h ON h.id=r.hall_id" + where + " ORDER BY p.id DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._promotion_view(row) for row in rows]

    # ------------------------------------------------------------------ 内部

    def _promote_from_release(self, connection: sqlite3.Connection, slot: sqlite3.Row, release_id: int, now: str) -> list[int]:
        release = connection.execute("SELECT * FROM capacity_releases WHERE id=?", (release_id,)).fetchone()
        promoted: list[int] = []
        # 按固定优先顺序遍历候补队列：老人 → 法会 → 同入口 → 同殿堂 → 先到先得。
        # 递补只会增加占用，被跳过的候补者在本事务内不会变得可行，因此单遍扫描即可；
        # 分层名额容纳不下或殿堂封闭者保留原名次，等待下一笔释放。
        candidates = connection.execute(
            "SELECT * FROM visit_reservations WHERE slot_id=? AND status='waiting' ORDER BY "
            "CASE visitor_group WHEN 'elder' THEN 0 WHEN 'ceremony' THEN 1 ELSE 2 END,"
            "CASE WHEN ?=\'\' THEN 0 WHEN entrance_code=? THEN 0 ELSE 1 END,"
            "CASE WHEN hall_id=? THEN 0 WHEN ?=-1 THEN 0 ELSE 1 END,"
            "id",
            (slot["id"], release["entrance_code"], release["entrance_code"],
             release["hall_id"] if release["hall_id"] is not None else WILDCARD_HALL,
             release["hall_id"] if release["hall_id"] is not None else WILDCARD_HALL),
        ).fetchall()
        for candidate in candidates:
            layers = self._constraint_layers(connection, slot["id"], candidate["hall_id"],
                                             candidate["entrance_code"], candidate["visitor_group"])
            feasible = bool(layers) and all(
                int(layer["capacity"]) - self._layer_occupied(
                    connection, slot["id"], layer["hall_id"], layer["entrance_code"], layer["visitor_group"]
                ) >= int(candidate["party_size"])
                for layer in layers
            )
            closure = self._blocking_closure(connection, slot["temple_id"], candidate["hall_id"],
                                             slot["slot_start"], slot["slot_end"])
            if not feasible or closure is not None:
                continue
            rule_code, rule_label = self._rule_for(candidate, release)
            rank_before = int(candidate["waitlist_rank"])
            queue_depth = int(connection.execute(
                "SELECT COUNT(*) FROM visit_reservations WHERE slot_id=? AND status='waiting'", (slot["id"],)
            ).fetchone()[0])
            deadline = to_storage(from_storage(now) + timedelta(seconds=DEFAULT_CONFIRM_WINDOW_SECONDS))
            connection.execute(
                "UPDATE visit_reservations SET status='pending_confirmation',waitlist_rank=NULL,confirm_deadline=?,"
                "promoted_from_release_id=?,promotion_rule_code=?,promoted_at=?,decided_at=NULL,version=version+1,updated_at=? WHERE id=?",
                (deadline, release_id, rule_code, now, now, candidate["id"]),
            )
            connection.execute(
                "INSERT INTO waitlist_promotions(reservation_id,temple_id,slot_id,capacity_release_id,rule_code,rule_label,"
                "rank_before,queue_depth,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (candidate["id"], slot["temple_id"], slot["id"], release_id, rule_code, rule_label, rank_before, queue_depth,
                 json.dumps({
                     "release_kind": release["release_kind"],
                     "release_entrance_code": release["entrance_code"],
                     "release_visitor_group": release["visitor_group"],
                     "release_hall_id": release["hall_id"],
                     "release_quantity": int(release["quantity"]),
                 }, ensure_ascii=False, sort_keys=True), now),
            )
            self._resequence_waitlist(connection, candidate)
            self._event(connection, "visit_reservation", candidate["id"], "promoted", "waitlist-engine",
                        {"rule_code": rule_code, "rule_label": rule_label, "capacity_release_id": release_id,
                         "rank_before": rank_before}, now, reservation_id=candidate["id"])
            promoted.append(candidate["id"])
        return promoted

    @staticmethod
    def _rule_for(candidate: sqlite3.Row, release: sqlite3.Row) -> tuple[str, str]:
        group = candidate["visitor_group"]
        if group == "elder":
            return "elder_first", PROMOTION_RULES["elder_first"]
        if group == "ceremony":
            return "ceremony_next", PROMOTION_RULES["ceremony_next"]
        if release["entrance_code"] and candidate["entrance_code"] == release["entrance_code"]:
            return "same_entrance", PROMOTION_RULES["same_entrance"]
        if release["hall_id"] is not None and candidate["hall_id"] == release["hall_id"]:
            return "same_hall", PROMOTION_RULES["same_hall"]
        return "fifo", PROMOTION_RULES["fifo"]

    def _expire_reservation(self, connection: sqlite3.Connection, reservation: sqlite3.Row, reason: str, now: str, *,
                            kind: str, actor: str) -> int:
        connection.execute(
            "UPDATE visit_reservations SET status='expired',decided_at=?,end_reason=?,version=version+1,updated_at=? WHERE id=?",
            (now, reason, now, reservation["id"]),
        )
        slot = connection.execute("SELECT * FROM visit_slots WHERE id=?", (reservation["slot_id"],)).fetchone()
        release_id = self._record_release_for_reservation(connection, reservation, kind, reason, actor, now, slot=slot)
        self._event(connection, "visit_reservation", reservation["id"], "expired", actor, {"reason": reason}, now,
                    reservation_id=reservation["id"])
        return release_id

    def _record_release_for_reservation(self, connection: sqlite3.Connection, reservation: sqlite3.Row,
                                        kind: str, reason: str, actor: str, now: str,
                                        slot: sqlite3.Row | None = None) -> int:
        slot = slot or connection.execute("SELECT * FROM visit_slots WHERE id=?", (reservation["slot_id"],)).fetchone()
        return self._write_release(
            connection, slot, reservation["hall_id"], reservation["entrance_code"], reservation["visitor_group"],
            int(reservation["party_size"]), kind=kind, reason=reason, actor=actor, now=now,
            source_reservation_id=reservation["id"],
        )

    @staticmethod
    def _write_release(connection: sqlite3.Connection, slot: sqlite3.Row, hall_id: int | None, entrance: str, group: str,
                       quantity: int, *, kind: str, reason: str, actor: str, now: str,
                       source_reservation_id: int | None = None, closure_window_id: int | None = None) -> int:
        cursor = connection.execute(
            "INSERT INTO capacity_releases(temple_id,slot_id,release_kind,source_reservation_id,closure_window_id,"
            "hall_id,entrance_code,visitor_group,quantity,reason,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (slot["temple_id"], slot["id"], kind, source_reservation_id, closure_window_id, hall_id, entrance, group,
             quantity, reason, actor, now),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _resequence_waitlist(connection: sqlite3.Connection, removed: sqlite3.Row) -> None:
        rows = connection.execute(
            "SELECT id FROM visit_reservations WHERE slot_id=? AND hall_key=? AND entrance_code=? AND visitor_group=? "
            "AND status='waiting' ORDER BY waitlist_rank,id",
            (removed["slot_id"], removed["hall_key"], removed["entrance_code"], removed["visitor_group"]),
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            connection.execute("UPDATE visit_reservations SET waitlist_rank=? WHERE id=?", (index, row["id"]))

    @staticmethod
    def _layer_occupied(connection: sqlite3.Connection, slot_id: int, hall_id: int | None,
                        entrance: str, group: str) -> int:
        sql = ("SELECT COALESCE(SUM(party_size),0) FROM visit_reservations WHERE slot_id=? AND status IN "
               "('pending_confirmation','confirmed')")
        params: list[Any] = [slot_id]
        if hall_id is None:
            sql += " AND 1=1"
        else:
            sql += " AND hall_id=?"
            params.append(hall_id)
        if entrance:
            sql += " AND entrance_code=?"
            params.append(entrance)
        if group:
            sql += " AND visitor_group=?"
            params.append(group)
        return int(connection.execute(sql, params).fetchone()[0])

    @staticmethod
    def _constraint_layers(connection: sqlite3.Connection, slot_id: int, hall_id: int,
                           entrance: str, group: str) -> list[sqlite3.Row]:
        keys = {
            (hall_id, entrance, group),
            (hall_id, entrance, ""),
            (hall_id, "", group),
            (hall_id, "", ""),
            (WILDCARD_HALL, entrance, group),
            (WILDCARD_HALL, entrance, ""),
            (WILDCARD_HALL, "", group),
            (WILDCARD_HALL, "", ""),
        }
        rows = connection.execute("SELECT * FROM visit_slot_capacities WHERE slot_id=?", (slot_id,)).fetchall()
        return [row for row in rows if (row["hall_key"], row["entrance_code"], row["visitor_group"]) in keys]

    @staticmethod
    def _blocking_closure(connection: sqlite3.Connection, temple_id: int, hall_id: int,
                          slot_start: str, slot_end: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM hall_closure_windows WHERE temple_id=? AND (hall_id IS NULL OR hall_id=?) "
            "AND state IN ('scheduled','active') AND starts_at<? AND ends_at>? ORDER BY hall_id DESC,id LIMIT 1",
            (temple_id, hall_id, slot_end, slot_start),
        ).fetchone()

    @staticmethod
    def _resolve_hall_id(connection: sqlite3.Connection, temple_id: int, hall_code: str | None) -> int | None:
        if not hall_code:
            return None
        row = connection.execute("SELECT id FROM worship_halls WHERE temple_id=? AND code=?", (temple_id, hall_code)).fetchone()
        if row is None:
            raise NotFoundError("名额殿堂不存在")
        return int(row["id"])

    @staticmethod
    def _locked_reservation(connection: sqlite3.Connection, reservation_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM visit_reservations WHERE id=?", (reservation_id,)).fetchone()
        if row is None:
            raise NotFoundError("预约不存在")
        return row

    @staticmethod
    def _new_code(connection: sqlite3.Connection, prefix: str) -> str:
        for _ in range(5):
            token = f"{prefix}-{secrets.token_hex(6)}"
            if connection.execute("SELECT 1 FROM visit_reservations WHERE code=?", (token,)).fetchone() is None:
                return token
        raise ConflictError("预约编码生成冲突，请重试")

    def _temple(self, code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM temple_sites WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    @staticmethod
    def _promotion_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        if "detail_json" in item:
            detail = item.pop("detail_json")
            item["detail"] = json.loads(detail) if detail else {}
        release_kind = item.get("release_kind")
        if release_kind:
            if item.get("release_reservation_code"):
                source = {"type": "reservation", "code": item["release_reservation_code"],
                          "reservation_id": item.get("source_reservation_id")}
            elif item.get("closure_code"):
                source = {"type": "closure", "code": item["closure_code"], "closure_window_id": item.get("closure_window_id")}
            elif release_kind == "capacity_adjustment":
                source = {"type": "capacity_adjustment"}
            else:
                source = {"type": release_kind}
            item["release_source"] = source
            item["release"] = {
                "kind": release_kind,
                "quantity": item.pop("release_quantity", None),
                "reason": item.pop("release_reason", None),
                "source": source,
            }
        for redundant in ("release_reservation_code", "closure_code", "source_reservation_id", "closure_window_id"):
            item.pop(redundant, None)
        return item

    @staticmethod
    def _event(connection: sqlite3.Connection, resource_type: str, resource_id: int, event_type: str, actor: str,
               detail: dict[str, Any], now: str, *, reservation_id: int | None = None) -> None:
        connection.execute(
            "INSERT INTO visit_events(resource_type,resource_id,reservation_id,event_type,actor,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (resource_type, resource_id, reservation_id, event_type, actor,
             json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _events(connection: sqlite3.Connection, resource_type: str, resource_id: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM visit_events WHERE resource_type=? AND resource_id=? ORDER BY id",
            (resource_type, resource_id),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result
