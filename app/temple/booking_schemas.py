from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Cohort = Literal["elderly", "ceremony", "general"]


class GateCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)


class QuotaSpec(BaseModel):
    # gate_code 为 None 表示不区分入口；cohort 为 None 表示不区分人群
    gate_code: str | None = Field(default=None, max_length=64)
    cohort: Cohort | None = None
    capacity: int = Field(ge=0, le=1_000_000)


class SegmentCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    starts_at: str
    ends_at: str
    total_capacity: int = Field(ge=1, le=1_000_000)
    confirm_timeout_seconds: int = Field(default=900, ge=60, le=86_400)
    # 人群优先值，数值越小越先递补；缺省 老人 < 法会 < 普通
    cohort_priority: dict[str, int] | None = None
    quotas: list[QuotaSpec] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_quotas(self) -> "SegmentCreate":
        seen: set[tuple[str | None, str | None]] = set()
        for quota in self.quotas:
            key = (quota.gate_code, quota.cohort)
            if key in seen:
                raise ValueError("同一入口与人群的分层配额不能重复")
            seen.add(key)
        if self.cohort_priority is not None:
            if set(self.cohort_priority) != {"elderly", "ceremony", "general"}:
                raise ValueError("cohort_priority 必须同时包含 elderly、ceremony、general")
            if any(value < 0 for value in self.cohort_priority.values()):
                raise ValueError("人群优先值必须是非负整数")
        return self


class ReservationCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    segment_code: str = Field(min_length=2, max_length=64)
    identity_hash: str = Field(min_length=8, max_length=128)
    identity_label: str = Field(default="", max_length=120)
    cohort: Cohort
    gate_code: str | None = Field(default=None, max_length=64)
    party_size: int = Field(default=1, ge=1, le=20)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class ReservationActor(BaseModel):
    actor: str = Field(default="visitor", min_length=1, max_length=120)
    reason: str = Field(default="visitor_cancelled", min_length=2, max_length=500)


class SegmentStateAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class ClosureCapacityImpact(BaseModel):
    segment_code: str = Field(min_length=2, max_length=64)
    gate_code: str | None = Field(default=None, max_length=64)
    cohort: Cohort | None = None
    seats: int = Field(ge=1, le=1_000_000)
