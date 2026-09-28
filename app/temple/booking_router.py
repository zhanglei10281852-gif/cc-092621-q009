from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.booking import TempleBookingService
from app.temple.booking_schemas import GateCreate, ReservationActor, ReservationCreate, SegmentCreate

router = APIRouter(prefix="/api/temple/visit", tags=["分时入寺预约与候补"])


def service() -> TempleBookingService:
    return TempleBookingService()


@router.post("/gates", status_code=201)
def create_gate(payload: GateCreate):
    return service().create_gate(payload.model_dump())


@router.get("/gates")
def list_gates(temple_code: str = Query(min_length=2, max_length=64)):
    return {"items": service().list_gates(temple_code)}


@router.post("/segments", status_code=201)
def create_segment(payload: SegmentCreate):
    return service().create_segment(payload.model_dump())


@router.get("/segments")
def list_segments(temple_code: str = Query(min_length=2, max_length=64)):
    return {"items": service().list_segments(temple_code)}


@router.get("/segments/{segment_id}")
def segment_detail(segment_id: int):
    return service().segment_detail(segment_id)


@router.get("/segments/{segment_id}/waitlist")
def segment_waitlist(segment_id: int):
    return {"items": service().waitlist(segment_id)}


@router.get("/segments/{segment_id}/promotions")
def segment_promotions(segment_id: int):
    """管理接口：列出该时段每一次候补递补使用的优先规则与消耗的释放容量。"""
    return {"items": service().list_segment_promotions(segment_id)}


@router.post("/reservations", status_code=201)
def create_reservation(payload: ReservationCreate):
    return service().create_reservation(payload.model_dump())


@router.get("/reservations/{reservation_no}")
def reservation_detail(reservation_no: str):
    return service().reservation_detail(reservation_no)


@router.post("/reservations/{reservation_no}/confirm")
def confirm_reservation(reservation_no: str, payload: ReservationActor):
    return service().confirm_reservation(reservation_no, payload.actor)


@router.post("/reservations/{reservation_no}/cancel")
def cancel_reservation(reservation_no: str, payload: ReservationActor):
    return service().cancel_reservation(reservation_no, payload.actor, payload.reason)


@router.post("/reservations/expire")
def expire_unconfirmed(actor: str = Query(default="confirmation-reaper", min_length=1)):
    return service().expire_unconfirmed(actor)
