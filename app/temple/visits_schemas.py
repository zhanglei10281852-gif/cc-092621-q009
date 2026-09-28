from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

VisitorGroup = Literal["elder", "ceremony", "general"]
CapacityGroup = Literal["", "elder", "ceremony", "general"]


class SlotCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    starts_at: str
    ends_at: str
    note: str = Field(default="", max_length=300)
    actor: str = Field(default="admin", min_length=1, max_length=120)


class SlotClose(BaseModel):
    reason: str = Field(default="时段关闭", min_length=2, max_length=300)
    actor: str = Field(default="admin", min_length=1, max_length=120)


class CapacitySet(BaseModel):
    hall_code: str | None = Field(default=None, max_length=64)
    entrance_code: str = Field(default="", max_length=64)
    visitor_group: CapacityGroup = ""
    capacity: int = Field(ge=0, le=10_000_000)
    reason: str = Field(default="", max_length=300)
    actor: str = Field(default="admin", min_length=1, max_length=120)


class ReservationCreate(BaseModel):
    slot_id: int = Field(gt=0)
    hall_code: str = Field(min_length=1, max_length=64)
    entrance_code: str = Field(min_length=1, max_length=64)
    visitor_group: VisitorGroup = "general"
    visitor_hash: str = Field(min_length=8, max_length=128)
    party_size: int = Field(default=1, ge=1, le=500)
    contact: str = Field(default="", max_length=200)
    confirm_window_seconds: int = Field(default=900, ge=30, le=86_400)
    request_id: str | None = Field(default=None, min_length=8, max_length=160)
    actor: str = Field(default="visitor", min_length=1, max_length=120)


class ReservationConfirm(BaseModel):
    request_id: str | None = Field(default=None, min_length=8, max_length=160)
    actor: str = Field(default="visitor", min_length=1, max_length=120)


class ReservationCancel(BaseModel):
    reason: str = Field(default="访客主动取消", min_length=2, max_length=500)
    request_id: str | None = Field(default=None, min_length=8, max_length=160)
    actor: str = Field(default="visitor", min_length=1, max_length=120)
