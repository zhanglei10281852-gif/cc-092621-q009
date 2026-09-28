from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import transaction
from app.services.idempotency import IdempotencyService
from app.temple.schema import ensure_temple_schema

ACTIVE_STATES = ("held", "confirmed")
DEFAULT_COHORT_PRIORITY = {"elderly": 10, "ceremony": 20, "general": 30}
COHORT_LABELS = {"elderly": "老人团队", "ceremony": "预约法会", "general": "普通访客"}
RULE_LABELS = {
    "cohort_priority": "人群优先（老人团队 > 预约法会 > 普通访客）",
    "gate_affinity": "同入口优先",
    "fifo": "先到先得（候补登记顺序）",
}
Bucket = tuple[int | None, str | None]


class TempleBookingService:
    """分时入寺预约、分层配额、持久化候补与可解释递补。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        from app.database import get_connection

        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础数据

    def create_gate(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO visit_gates(temple_id,code,name,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (temple["id"], payload["code"], payload["name"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("入口编码已存在") from exc
            return self._gate_dict(connection.execute("SELECT * FROM visit_gates WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_gates(self, temple_code: str) -> list[dict[str, Any]]:
        temple = self._temple(temple_code)
        rows = self.connection.execute("SELECT * FROM visit_gates WHERE temple_id=? ORDER BY code,id", (temple["id"],)).fetchall()
        return [self._gate_dict(row) for row in rows]

    def create_segment(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        starts_at = self._time(payload["starts_at"], "时段开始时间")
        ends_at = self._time(payload["ends_at"], "时段结束时间")
        if ends_at <= starts_at:
            raise ValidationError("时段结束时间必须晚于开始时间")
        priority = payload.get("cohort_priority") or DEFAULT_COHORT_PRIORITY
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO visit_segments(temple_id,code,starts_at,ends_at,total_capacity,confirm_timeout_seconds,cohort_priority_json,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (temple["id"], payload["code"], starts_at, ends_at, payload["total_capacity"], payload["confirm_timeout_seconds"], json.dumps(priority, ensure_ascii=False, sort_keys=True), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("分时时段编码或时间区间已存在") from exc
            segment_id = cursor.lastrowid
            for quota in payload["quotas"]:
                gate_id = None
                if quota["gate_code"]:
                    gate = connection.execute("SELECT id FROM visit_gates WHERE temple_id=? AND code=?", (temple["id"], quota["gate_code"])).fetchone()
                    if gate is None:
                        raise NotFoundError(f"入口不存在：{quota['gate_code']}")
                    gate_id = gate["id"]
                duplicate_quota = connection.execute(
                    "SELECT id FROM visit_segment_quotas WHERE segment_id=? AND gate_id IS ? AND cohort IS ?",
                    (segment_id, gate_id, quota["cohort"]),
                ).fetchone()
                if duplicate_quota is not None:
                    raise ConflictError("同一入口与人群的分层配额重复")
                try:
                    connection.execute(
                        "INSERT INTO visit_segment_quotas(segment_id,gate_id,cohort,capacity,created_at) VALUES(?,?,?,?,?)",
                        (segment_id, gate_id, quota["cohort"], quota["capacity"], now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("同一入口与人群的分层配额重复") from exc
            return self.segment_detail(segment_id, connection)

    def list_segments(self, temple_code: str) -> list[dict[str, Any]]:
        temple = self._temple(temple_code)
        rows = self.connection.execute("SELECT * FROM visit_segments WHERE temple_id=? ORDER BY starts_at,id", (temple["id"],)).fetchall()
        return [self._segment_dict(row) for row in rows]

    def segment_detail(self, segment_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute("SELECT * FROM visit_segments WHERE id=?", (segment_id,)).fetchone()
        if row is None:
            raise NotFoundError("分时时段不存在")
        result = self._segment_dict(row)
        result["quotas"] = [self._quota_dict(connection, quota) for quota in connection.execute("SELECT * FROM visit_segment_quotas WHERE segment_id=? ORDER BY id", (segment_id,)).fetchall()]
        result["snapshot"] = self._snapshot(connection, segment_id)
        result["waiting"] = self.waitlist(segment_id, connection=connection)
        return result

    def segment_detail_by_code(self, temple_code: str, segment_code: str) -> dict[str, Any]:
        temple = self._temple(temple_code)
        row = self.connection.execute("SELECT id FROM visit_segments WHERE temple_id=? AND code=?", (temple["id"], segment_code)).fetchone()
        if row is None:
            raise NotFoundError("分时时段不存在")
        return self.segment_detail(row["id"])

    def waitlist(self, segment_id: int, *, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        connection = connection or self.connection
        segment = connection.execute("SELECT * FROM visit_segments WHERE id=?", (segment_id,)).fetchone()
        if segment is None:
            raise NotFoundError("分时时段不存在")
        priority = self._cohort_priority(segment)
        rows = connection.execute(
            "SELECT r.*,g.code AS gate_code FROM visit_reservations r LEFT JOIN visit_gates g ON g.id=r.gate_id WHERE r.segment_id=? AND r.state='waiting' ORDER BY r.queue_seq,id",
            (segment_id,),
        ).fetchall()
        ordered = sorted(rows, key=lambda item: (priority[item["cohort"]], item["queue_seq"]))
        result = []
        for position, item in enumerate(ordered, start=1):
            data = self._reservation_dict(item)
            data["queue_position"] = position
            result.append(data)
        return result

    # ------------------------------------------------------------------ 预约

    def create_reservation(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        segment = self.connection.execute(
            "SELECT * FROM visit_segments WHERE temple_id=? AND code=?", (temple["id"], payload["segment_code"])
        ).fetchone()
        if segment is None:
            raise NotFoundError("分时时段不存在")
        gate_id: int | None = None
        gate_code = payload.get("gate_code")
        if gate_code:
            gate = self.connection.execute("SELECT id FROM visit_gates WHERE temple_id=? AND code=?", (temple["id"], gate_code)).fetchone()
            if gate is None:
                raise NotFoundError("入寺入口不存在")
            gate_id = gate["id"]
        now_value = self.clock.now()
        now = to_storage(now_value)
        if now >= segment["ends_at"]:
            raise ConflictError("该分时时段已结束，不能再预约")
        if segment["state"] != "open":
            raise ConflictError("该分时时段已关闭预约")
        idem_payload = {key: value for key, value in payload.items() if key != "idempotency_key"}
        idem_key = payload.get("idempotency_key")
        with transaction(immediate=True) as connection:
            if idem_key:
                stored = IdempotencyService(connection, self.clock).lookup("visit.reservations", idem_key, idem_payload)
                if stored is not None:
                    return {**stored.body, "replayed": True}
            # 无幂等键的网络重放：同一身份在同一时段已有占位直接返回原预约
            duplicate = connection.execute(
                "SELECT * FROM visit_reservations WHERE segment_id=? AND identity_hash=? AND state IN ('waiting','held','confirmed') ORDER BY id LIMIT 1",
                (segment["id"], payload["identity_hash"]),
            ).fetchone()
            if duplicate is not None:
                return {**self.reservation_detail(duplicate["reservation_no"], connection), "replayed": True}
            overlap = connection.execute(
                "SELECT r.id,s.code AS segment_code FROM visit_reservations r JOIN visit_segments s ON s.id=r.segment_id "
                "WHERE r.identity_hash=? AND s.temple_id=? AND r.state IN ('waiting','held','confirmed') "
                "AND s.starts_at<? AND s.ends_at>?",
                (payload["identity_hash"], temple["id"], segment["ends_at"], segment["starts_at"]),
            ).fetchone()
            if overlap is not None:
                raise ConflictError("同一身份在重叠时段已有预约或候补，不能重复占位", context={"overlap_segment": overlap["segment_code"]})
            view = self._capacity_view(connection, segment["id"])
            fits = self._fits(view, gate_id, payload["cohort"], payload["party_size"])
            cursor = connection.execute(
                "INSERT INTO visit_reservations(reservation_no,temple_id,segment_id,gate_id,identity_hash,identity_label,cohort,party_size,state,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("PENDING", temple["id"], segment["id"], gate_id, payload["identity_hash"], payload.get("identity_label", ""), payload["cohort"], payload["party_size"], "held" if fits else "waiting", now, now),
            )
            reservation_id = cursor.lastrowid
            reservation_no = f"VR-{reservation_id:08d}"
            if fits:
                deadline = min(
                    from_storage(now) + self._timeout_delta(int(segment["confirm_timeout_seconds"])),
                    from_storage(segment["starts_at"]),
                )
                connection.execute(
                    "UPDATE visit_reservations SET reservation_no=?,confirm_deadline=?,version=version+1 WHERE id=?",
                    (reservation_no, to_storage(deadline), reservation_id),
                )
            else:
                next_seq = int(connection.execute("SELECT COALESCE(MAX(queue_seq),0)+1 FROM visit_reservations WHERE segment_id=?", (segment["id"],)).fetchone()[0])
                connection.execute(
                    "UPDATE visit_reservations SET reservation_no=?,queue_seq=? WHERE id=?",
                    (reservation_no, next_seq, reservation_id),
                )
            self._event(connection, reservation_id, "allocated" if fits else "waitlisted", payload.get("identity_label") or "visitor", {"fits": fits}, now)
            body = self.reservation_detail(reservation_no, connection)
            if idem_key:
                IdempotencyService(connection, self.clock).save("visit.reservations", idem_key, idem_payload, body, 201)
            return body

    def confirm_reservation(self, reservation_no: str, actor: str = "visitor") -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._locked_reservation(connection, reservation_no)
            now = to_storage(self.clock.now())
            if row["state"] == "confirmed":
                return self.reservation_detail(reservation_no, connection)
            if row["state"] != "held":
                raise ConflictError(f"当前状态（{row['state']}）不能确认预约")
            if row["confirm_deadline"] is not None and now > row["confirm_deadline"]:
                raise ConflictError("预约确认已超过截止时间，名额已释放", context={"confirm_deadline": row["confirm_deadline"]})
            connection.execute(
                "UPDATE visit_reservations SET state='confirmed',confirmed_at=?,updated_at=?,version=version+1 WHERE id=?",
                (now, now, row["id"]),
            )
            self._event(connection, row["id"], "confirmed", actor, {}, now)
            return self.reservation_detail(reservation_no, connection)

    def cancel_reservation(self, reservation_no: str, actor: str = "visitor", reason: str = "visitor_cancelled") -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            row = self._locked_reservation(connection, reservation_no)
            now = to_storage(self.clock.now())
            if row["state"] in ("cancelled", "expired", "declined"):
                return self.reservation_detail(reservation_no, connection)
            releasing = row["state"] in ACTIVE_STATES
            connection.execute(
                "UPDATE visit_reservations SET state='cancelled',cancelled_at=?,end_reason=?,updated_at=?,version=version+1 WHERE id=?",
                (now, reason, now, row["id"]),
            )
            self._event(connection, row["id"], "cancelled", actor, {"reason": reason, "released": releasing}, now)
            promotions: list[dict[str, Any]] = []
            if releasing:
                release_id = self._insert_release(connection, row["segment_id"], row["gate_id"], row["cohort"], row["party_size"], "cancelled", source_reservation_id=row["id"], now=now)
                promotions = self._promote_waitlist(connection, row["segment_id"], release_id, now)
            result = self.reservation_detail(reservation_no, connection)
            result["promotions"] = promotions
            return result

    def expire_unconfirmed(self, actor: str = "confirmation-reaper") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        rows = self.connection.execute(
            "SELECT id FROM visit_reservations WHERE state='held' AND confirm_deadline IS NOT NULL AND confirm_deadline<=? ORDER BY confirm_deadline,id",
            (now,),
        ).fetchall()
        expired: list[str] = []
        promotions: list[dict[str, Any]] = []
        for item in rows:
            with transaction(immediate=True) as connection:
                row = connection.execute("SELECT * FROM visit_reservations WHERE id=? AND state='held'", (item["id"],)).fetchone()
                if row is None or row["confirm_deadline"] is None or row["confirm_deadline"] > now:
                    continue
                connection.execute(
                    "UPDATE visit_reservations SET state='expired',end_reason='confirm_timeout',updated_at=?,version=version+1 WHERE id=?",
                    (now, row["id"]),
                )
                self._event(connection, row["id"], "expired", actor, {"reason": "confirm_timeout"}, now)
                release_id = self._insert_release(connection, row["segment_id"], row["gate_id"], row["cohort"], row["party_size"], "confirm_timeout", source_reservation_id=row["id"], now=now)
                expired.append(row["reservation_no"])
                promotions.extend(self._promote_waitlist(connection, row["segment_id"], release_id, now))
        return {"expired": expired, "promotions": promotions}

    def reservation_detail(self, reservation_no: str, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT r.*,g.code AS gate_code,s.code AS segment_code FROM visit_reservations r LEFT JOIN visit_gates g ON g.id=r.gate_id JOIN visit_segments s ON s.id=r.segment_id WHERE r.reservation_no=?",
            (reservation_no,),
        ).fetchone()
        if row is None:
            raise NotFoundError("预约不存在")
        result = self._reservation_dict(row)
        result["cohort_label"] = COHORT_LABELS[result["cohort"]]
        if result["state"] == "waiting":
            result["queue_position"] = self._queue_position(connection, row)
        promotion = connection.execute(
            "SELECT p.*,r.reservation_no AS promoted_reservation FROM visit_waitlist_promotions p JOIN visit_reservations r ON r.id=p.reservation_id WHERE p.reservation_id=? ORDER BY p.id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if promotion is not None:
            release = connection.execute("SELECT * FROM visit_quota_releases WHERE id=?", (promotion["release_id"],)).fetchone()
            result["promotion"] = {
                "rule_code": promotion["rule_code"],
                "rule_label": RULE_LABELS[promotion["rule_code"]],
                "rule_detail": json.loads(promotion["rule_detail_json"]),
                "release_id": promotion["release_id"],
                "release": self._release_dict(connection, release) if release else None,
                "promoted_at": promotion["promoted_at"],
            }
        result["events"] = self._events(connection, row["id"])
        return result

    # ------------------------------------------------------------------ 封闭窗口联动

    def add_closure_impacts(self, window_id: int, impacts: list[dict[str, Any]], actor: str) -> dict[str, Any]:
        window = self.connection.execute("SELECT * FROM hall_closure_windows WHERE id=?", (window_id,)).fetchone()
        if window is None:
            raise NotFoundError("封闭窗口不存在")
        if window["state"] not in ("scheduled", "active"):
            raise ConflictError("封闭窗口已结束，不能再登记容量影响")
        now = to_storage(self.clock.now())
        created: list[dict[str, Any]] = []
        with transaction(immediate=True) as connection:
            for impact in impacts:
                segment = connection.execute(
                    "SELECT * FROM visit_segments WHERE temple_id=? AND code=?", (window["temple_id"], impact["segment_code"])
                ).fetchone()
                if segment is None:
                    raise NotFoundError(f"分时时段不存在：{impact['segment_code']}")
                if segment["starts_at"] >= window["ends_at"] or segment["ends_at"] <= window["starts_at"]:
                    raise ValidationError(f"时段 {impact['segment_code']} 与封闭窗口没有重叠")
                gate_id = None
                if impact.get("gate_code"):
                    gate = connection.execute("SELECT id FROM visit_gates WHERE temple_id=? AND code=?", (window["temple_id"], impact["gate_code"])).fetchone()
                    if gate is None:
                        raise NotFoundError(f"入口不存在：{impact['gate_code']}")
                    gate_id = gate["id"]
                duplicate_hold = connection.execute(
                    "SELECT id FROM visit_capacity_holds WHERE closure_window_id=? AND segment_id=? AND gate_id IS ? AND cohort IS ?",
                    (window_id, segment["id"], gate_id, impact.get("cohort")),
                ).fetchone()
                if duplicate_hold is not None:
                    raise ConflictError("该封闭窗口对同一时段/入口/人群的容量收紧已存在")
                try:
                    cursor = connection.execute(
                        "INSERT INTO visit_capacity_holds(closure_window_id,segment_id,gate_id,cohort,seats,created_at) VALUES(?,?,?,?,?,?)",
                        (window_id, segment["id"], gate_id, impact.get("cohort"), impact["seats"], now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("该封闭窗口对同一时段/入口/人群的容量收紧已存在") from exc
                connection.execute(
                    "INSERT INTO restoration_events(resource_type,resource_id,event_type,actor,detail_json,created_at) VALUES('closure',?,?,?,?,?)",
                    (window_id, "capacity_held", actor, json.dumps({"segment": impact["segment_code"], "gate": impact.get("gate_code"), "cohort": impact.get("cohort"), "seats": impact["seats"]}, ensure_ascii=False, sort_keys=True), now),
                )
                created.append(self._hold_dict(connection, connection.execute("SELECT * FROM visit_capacity_holds WHERE id=?", (cursor.lastrowid,)).fetchone()))
        return {"window_id": window_id, "holds": created}

    def release_holds_for_windows(self, window_ids: list[int], actor: str = "closure-scheduler") -> dict[str, Any]:
        releases: list[dict[str, Any]] = []
        promotions: list[dict[str, Any]] = []
        for window_id in window_ids:
            with transaction(immediate=True) as connection:
                window_releases, window_promotions = self.release_window_holds(connection, window_id, actor)
                releases.extend(window_releases)
                promotions.extend(window_promotions)
        return {"releases": releases, "promotions": promotions}

    def release_window_holds(self, connection: sqlite3.Connection, window_id: int, actor: str, now: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        now = now or to_storage(self.clock.now())
        holds = connection.execute(
            "SELECT * FROM visit_capacity_holds WHERE closure_window_id=? AND state='held' ORDER BY id", (window_id,)
        ).fetchall()
        if not holds:
            return [], []
        releases: list[dict[str, Any]] = []
        promotions: list[dict[str, Any]] = []
        for hold in holds:
            connection.execute("UPDATE visit_capacity_holds SET state='released',released_at=? WHERE id=?", (now, hold["id"]))
            release_id = self._insert_release(
                connection, hold["segment_id"], hold["gate_id"], hold["cohort"], hold["seats"], "closure_released", closure_window_id=window_id, now=now
            )
            releases.append(self._release_dict(connection, connection.execute("SELECT * FROM visit_quota_releases WHERE id=?", (release_id,)).fetchone()))
            promotions.extend(self._promote_waitlist(connection, hold["segment_id"], release_id, now))
        connection.execute(
            "INSERT INTO restoration_events(resource_type,resource_id,event_type,actor,detail_json,created_at) VALUES('closure',?,?,?,?,?)",
            (window_id, "capacity_released", actor, json.dumps({"holds": len(holds)}, ensure_ascii=False), now),
        )
        return releases, promotions

    def list_segment_promotions(self, segment_id: int) -> list[dict[str, Any]]:
        if self.connection.execute("SELECT id FROM visit_segments WHERE id=?", (segment_id,)).fetchone() is None:
            raise NotFoundError("分时时段不存在")
        rows = self.connection.execute("SELECT * FROM visit_waitlist_promotions WHERE segment_id=? ORDER BY id", (segment_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["rule_label"] = RULE_LABELS[row["rule_code"]]
            item["rule_detail"] = json.loads(row["rule_detail_json"])
            item["skipped"] = json.loads(row["skipped_json"])
            item["release"] = self._release_dict(self.connection, self.connection.execute("SELECT * FROM visit_quota_releases WHERE id=?", (row["release_id"],)).fetchone())
            reservation = self.connection.execute("SELECT * FROM visit_reservations WHERE id=?", (row["reservation_id"],)).fetchone()
            item["reservation"] = dict(reservation) if reservation else None
            result.append(item)
        return result

    # ------------------------------------------------------------------ 容量与递补引擎

    def _promote_waitlist(self, connection: sqlite3.Connection, segment_id: int, release_id: int, now: str) -> list[dict[str, Any]]:
        """依据释放名额递补；每次递补记录触发它的释放批次与决定性优先规则。"""
        segment = connection.execute("SELECT * FROM visit_segments WHERE id=?", (segment_id,)).fetchone()
        release = connection.execute("SELECT * FROM visit_quota_releases WHERE id=?", (release_id,)).fetchone()
        priority = self._cohort_priority(segment)
        promotions: list[dict[str, Any]] = []
        while True:
            view = self._capacity_view(connection, segment_id)
            rows = connection.execute(
                "SELECT r.*,g.code AS gate_code FROM visit_reservations r LEFT JOIN visit_gates g ON g.id=r.gate_id WHERE r.segment_id=? AND r.state='waiting'",
                (segment_id,),
            ).fetchall()
            candidates = [dict(row) for row in rows]
            if not candidates:
                break

            def affinity(item: dict[str, Any]) -> int:
                if release["gate_id"] is None:
                    return 0
                return 0 if item["gate_id"] == release["gate_id"] else 1

            ordered = sorted(candidates, key=lambda item: (priority[item["cohort"]], affinity(item), item["queue_seq"]))
            eligible: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            for item in ordered:
                if self._fits(view, item["gate_id"], item["cohort"], item["party_size"]):
                    eligible.append(item)
                else:
                    skipped.append({"reservation_no": item["reservation_no"], "cohort": item["cohort"], "reason": "capacity"})
            if not eligible:
                break
            chosen = eligible[0]
            rule_code = self._decide_rule(chosen, eligible[1:], priority, affinity)
            deadline = min(
                from_storage(now) + self._timeout_delta(int(segment["confirm_timeout_seconds"])),
                from_storage(segment["starts_at"]),
            )
            connection.execute(
                "UPDATE visit_reservations SET state='held',confirm_deadline=?,updated_at=?,version=version+1 WHERE id=?",
                (to_storage(deadline), now, chosen["id"]),
            )
            # 递补者立即占用容量，保证后续判定不超卖
            view.consumed[(chosen["gate_id"], chosen["cohort"])] = view.consumed.get((chosen["gate_id"], chosen["cohort"]), 0) + chosen["party_size"]
            rule_detail = {
                "ordering": ["cohort_priority", "gate_affinity", "fifo"],
                "cohort_priority": priority,
                "release_gate_id": release["gate_id"],
                "chosen": {"reservation_no": chosen["reservation_no"], "cohort": chosen["cohort"], "gate_id": chosen["gate_id"], "queue_seq": chosen["queue_seq"]},
            }
            cursor = connection.execute(
                "INSERT INTO visit_waitlist_promotions(segment_id,reservation_id,release_id,seats,rule_code,rule_detail_json,skipped_json,promoted_at) VALUES(?,?,?,?,?,?,?,?)",
                (segment_id, chosen["id"], release_id, chosen["party_size"], rule_code, json.dumps(rule_detail, ensure_ascii=False, sort_keys=True), json.dumps(skipped[:20], ensure_ascii=False), now),
            )
            self._event(connection, chosen["id"], "promoted", "waitlist-promoter", {"release_id": release_id, "rule_code": rule_code}, now)
            promotions.append(
                {
                    "id": cursor.lastrowid,
                    "reservation_no": chosen["reservation_no"],
                    "cohort": chosen["cohort"],
                    "seats": chosen["party_size"],
                    "rule_code": rule_code,
                    "rule_label": RULE_LABELS[rule_code],
                    "release_id": release_id,
                }
            )
        connection.execute(
            "UPDATE visit_quota_releases SET remaining_seats=? WHERE id=?",
            (self._remaining_in_bucket(self._capacity_view(connection, segment_id), release["gate_id"], release["cohort"]), release_id),
        )
        return promotions

    @staticmethod
    def _decide_rule(chosen: dict[str, Any], candidates: list[dict[str, Any]], priority: dict[str, int], affinity) -> str:
        chosen_rank = priority[chosen["cohort"]]
        chosen_affinity = affinity(chosen)
        used_cohort = any(priority[item["cohort"]] > chosen_rank for item in candidates if item["id"] != chosen["id"])
        if used_cohort:
            return "cohort_priority"
        used_affinity = any(
            priority[item["cohort"]] == chosen_rank and affinity(item) > chosen_affinity
            for item in candidates
            if item["id"] != chosen["id"]
        )
        if used_affinity:
            return "gate_affinity"
        return "fifo"

    @staticmethod
    def _buckets(gate_id: int | None, cohort: str | None) -> tuple[Bucket, Bucket, Bucket, Bucket]:
        return (None, None), (gate_id, None), (None, cohort), (gate_id, cohort)

    def _fits(self, view: "_CapacityView", gate_id: int | None, cohort: str, party_size: int) -> bool:
        for bucket_gate, bucket_cohort in self._buckets(gate_id, cohort):
            capacity = view.capacity(bucket_gate, bucket_cohort)
            used = view.used(bucket_gate, bucket_cohort)
            held_seats = view.holds(bucket_gate, bucket_cohort)
            if used + held_seats + party_size > capacity:
                return False
        return True

    def _capacity_view(self, connection: sqlite3.Connection, segment_id: int) -> "_CapacityView":
        segment = connection.execute("SELECT total_capacity FROM visit_segments WHERE id=?", (segment_id,)).fetchone()
        capacities = {
            (row["gate_id"], row["cohort"]): int(row["capacity"])
            for row in connection.execute("SELECT gate_id,cohort,capacity FROM visit_segment_quotas WHERE segment_id=?", (segment_id,)).fetchall()
        }
        consumed: dict[Bucket, int] = {}
        for row in connection.execute(
            "SELECT gate_id,cohort,SUM(party_size) AS seats FROM visit_reservations WHERE segment_id=? AND state IN ('held','confirmed') GROUP BY gate_id,cohort",
            (segment_id,),
        ).fetchall():
            consumed[(row["gate_id"], row["cohort"])] = int(row["seats"])
        held: dict[Bucket, int] = {}
        for row in connection.execute(
            "SELECT gate_id,cohort,SUM(seats) AS seats FROM visit_capacity_holds WHERE segment_id=? AND state='held' GROUP BY gate_id,cohort",
            (segment_id,),
        ).fetchall():
            held[(row["gate_id"], row["cohort"])] = int(row["seats"])
        return _CapacityView(int(segment["total_capacity"]), capacities, consumed, held)

    def _remaining_in_bucket(self, view: "_CapacityView", gate_id: int | None, cohort: str | None) -> int:
        return max(0, view.capacity(gate_id, cohort) - view.used(gate_id, cohort) - view.holds(gate_id, cohort))

    def _snapshot(self, connection: sqlite3.Connection, segment_id: int) -> dict[str, Any]:
        view = self._capacity_view(connection, segment_id)
        waiting = int(connection.execute("SELECT COUNT(*) FROM visit_reservations WHERE segment_id=? AND state='waiting'", (segment_id,)).fetchone()[0])

        def bucket(gate_id: int | None, cohort: str | None) -> dict[str, Any]:
            return {
                "gate_id": gate_id,
                "cohort": cohort,
                "capacity": view.capacity(gate_id, cohort),
                "used": view.used(gate_id, cohort),
                "closure_held": view.holds(gate_id, cohort),
                "available": max(0, view.capacity(gate_id, cohort) - view.used(gate_id, cohort) - view.holds(gate_id, cohort)),
            }

        return {
            "total": bucket(None, None),
            "by_gate": [bucket(gate_id, None) for gate_id in sorted({row["gate_id"] for row in connection.execute("SELECT DISTINCT gate_id FROM visit_segment_quotas WHERE segment_id=? AND gate_id IS NOT NULL", (segment_id,)).fetchall()})],
            "by_cohort": [bucket(None, cohort) for cohort in ("elderly", "ceremony", "general")],
            "layers": [bucket(gate_id, cohort) for gate_id, cohort in sorted(view.capacities.keys(), key=lambda item: (item[0] is None, item[0], item[1] is None, item[1]))],
            "waiting_count": waiting,
        }

    # ------------------------------------------------------------------ 组装与工具

    def _insert_release(
        self,
        connection: sqlite3.Connection,
        segment_id: int,
        gate_id: int | None,
        cohort: str | None,
        seats: int,
        reason: str,
        *,
        source_reservation_id: int | None = None,
        closure_window_id: int | None = None,
        now: str,
    ) -> int:
        view = self._capacity_view(connection, segment_id)
        remaining = self._remaining_in_bucket(view, gate_id, cohort)
        cursor = connection.execute(
            "INSERT INTO visit_quota_releases(segment_id,gate_id,cohort,seats,remaining_seats,reason,source_reservation_id,closure_window_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (segment_id, gate_id, cohort, seats, remaining, reason, source_reservation_id, closure_window_id, now),
        )
        return int(cursor.lastrowid)

    def _queue_position(self, connection: sqlite3.Connection, row: sqlite3.Row) -> int:
        segment = connection.execute("SELECT cohort_priority_json FROM visit_segments WHERE id=?", (row["segment_id"],)).fetchone()
        priority = self._cohort_priority(segment)
        peers = connection.execute("SELECT cohort,queue_seq FROM visit_reservations WHERE segment_id=? AND state='waiting'", (row["segment_id"],)).fetchall()
        rank = sum(
            1
            for peer in peers
            if (priority[peer["cohort"]], peer["queue_seq"]) < (priority[row["cohort"]], row["queue_seq"])
        )
        return rank + 1

    def _temple(self, code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM temple_sites WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    def _locked_reservation(self, connection: sqlite3.Connection, reservation_no: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM visit_reservations WHERE reservation_no=?", (reservation_no,)).fetchone()
        if row is None:
            raise NotFoundError("预约不存在")
        return row

    @staticmethod
    def _time(value: str, label: str) -> str:
        try:
            return to_storage(from_storage(value))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{label}格式不正确") from exc

    @staticmethod
    def _timeout_delta(seconds: int):
        from datetime import timedelta

        return timedelta(seconds=seconds)

    @staticmethod
    def _cohort_priority(segment: sqlite3.Row) -> dict[str, int]:
        raw = json.loads(segment["cohort_priority_json"])
        return {key: int(raw[key]) for key in ("elderly", "ceremony", "general")}

    @staticmethod
    def _event(connection: sqlite3.Connection, reservation_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO visit_reservation_events(reservation_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (reservation_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _events(connection: sqlite3.Connection, reservation_id: int) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM visit_reservation_events WHERE reservation_id=? ORDER BY id", (reservation_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    def _gate_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def _hold_dict(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        if row["gate_id"] is not None:
            gate = connection.execute("SELECT code FROM visit_gates WHERE id=?", (row["gate_id"],)).fetchone()
            result["gate_code"] = gate["code"] if gate else None
        segment = connection.execute("SELECT code FROM visit_segments WHERE id=?", (row["segment_id"],)).fetchone()
        result["segment_code"] = segment["code"] if segment else None
        return result

    def _release_dict(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["gate_code"] = None
        if row["gate_id"] is not None:
            gate = connection.execute("SELECT code FROM visit_gates WHERE id=?", (row["gate_id"],)).fetchone()
            result["gate_code"] = gate["code"] if gate else None
        result["cohort_label"] = COHORT_LABELS.get(row["cohort"]) if row["cohort"] else None
        result["reason_label"] = {
            "cancelled": "预约取消",
            "confirm_timeout": "超时未确认",
            "segment_purge": "时段清理",
            "closure_released": "封闭窗口解除",
            "admin_adjustment": "管理员调整",
        }[row["reason"]]
        return result

    def _segment_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["cohort_priority"] = json.loads(result.pop("cohort_priority_json"))
        return result

    def _quota_dict(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["gate_code"] = None
        if row["gate_id"] is not None:
            gate = connection.execute("SELECT code FROM visit_gates WHERE id=?", (row["gate_id"],)).fetchone()
            result["gate_code"] = gate["code"] if gate else None
        result["cohort_label"] = COHORT_LABELS.get(row["cohort"]) if row["cohort"] else "全部人群"
        return result

    def _reservation_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result.pop("segment_simple_code", None)
        result.pop("temple_id", None)
        return result


class _CapacityView:
    """某一时段的分层容量快照：总量/入口/人群/入口×人群 四个层级联动校验。"""

    def __init__(self, total: int, capacities: dict[Bucket, int], consumed: dict[Bucket, int], holds: dict[Bucket, int]) -> None:
        self.total = total
        self.capacities = capacities
        self.consumed = consumed
        self.holds_map = holds

    def _covers(self, bucket_gate: int | None, bucket_cohort: str | None, leaf_gate: int | None, leaf_cohort: str | None) -> bool:
        gate_ok = bucket_gate is None or leaf_gate == bucket_gate
        cohort_ok = bucket_cohort is None or leaf_cohort == bucket_cohort
        return gate_ok and cohort_ok

    def _sum(self, leaves: dict[Bucket, int], gate_id: int | None, cohort: str | None) -> int:
        return sum(seats for (leaf_gate, leaf_cohort), seats in leaves.items() if self._covers(gate_id, cohort, leaf_gate, leaf_cohort))

    def capacity(self, gate_id: int | None, cohort: str | None) -> int:
        return self.capacities.get((gate_id, cohort), self.total)

    def used(self, gate_id: int | None, cohort: str | None) -> int:
        return self._sum(self.consumed, gate_id, cohort)

    def holds(self, gate_id: int | None, cohort: str | None) -> int:
        return self._sum(self.holds_map, gate_id, cohort)
