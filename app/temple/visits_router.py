from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.visits import VisitBookingService
from app.temple.visits_schemas import CapacitySet, ReservationCancel, ReservationConfirm, ReservationCreate, SlotClose, SlotCreate

router = APIRouter(prefix="/api/temple/visits", tags=["分时入寺预约与候补"])


def service() -> VisitBookingService:
    return VisitBookingService()


@router.post("/slots", status_code=201)
def create_slot(payload: SlotCreate):
    return service().create_slot(payload.model_dump())


@router.get("/slots")
def list_slots(temple_code: str | None = None, slot_date: str | None = None):
    return {"items": service().list_slots(temple_code, slot_date)}


@router.get("/slots/{slot_id}")
def slot_detail(slot_id: int):
    return service().slot_detail(slot_id)


@router.put("/slots/{slot_id}/capacities")
def set_capacity(slot_id: int, payload: CapacitySet):
    return service().set_capacity(slot_id, payload.model_dump())


@router.post("/slots/{slot_id}/close")
def close_slot(slot_id: int, payload: SlotClose):
    return service().close_slot(slot_id, payload.model_dump())


@router.post("/reservations", status_code=201)
def reserve(payload: ReservationCreate):
    return service().reserve(payload.model_dump())


@router.get("/reservations/{reservation_id}")
def reservation_detail(reservation_id: int):
    return service().reservation_detail(reservation_id)


@router.get("/reservations/by-token/{confirm_token}")
def reservation_by_token(confirm_token: str):
    return service().reservation_by_token(confirm_token)


@router.post("/reservations/{reservation_id}/confirm")
def confirm_reservation(reservation_id: int, payload: ReservationConfirm):
    return service().confirm_reservation(reservation_id, payload.model_dump())


@router.post("/reservations/{reservation_id}/cancel")
def cancel_reservation(reservation_id: int, payload: ReservationCancel):
    return service().cancel_reservation(reservation_id, payload.model_dump())


@router.post("/reservations/expire-unconfirmed")
def expire_unconfirmed(actor: str = Query(default="visit-confirmation-reaper", min_length=1)):
    return service().expire_unconfirmed(actor)


@router.post("/closures/apply")
def apply_closure_changes(actor: str = Query(default="closure-scheduler", min_length=1)):
    return service().apply_closure_changes(actor)


@router.get("/promotions")
def promotion_log(slot_id: int | None = None, reservation_id: int | None = None,
                  limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().promotion_log(slot_id, reservation_id, limit)}
