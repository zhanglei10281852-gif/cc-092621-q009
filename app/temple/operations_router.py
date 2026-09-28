from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.booking import TempleBookingService
from app.temple.operations import TempleRestorationService
from app.temple.operations_schemas import ClosureAction, ClosureCreate, ClosureImpactCreate, RolloutRestorationCampaignCreate, RestorationCampaignAction

router = APIRouter(prefix="/api/temple/operations", tags=["修缮计划与殿堂封闭"])


def service() -> TempleRestorationService:
    return TempleRestorationService()


@router.post("/restoration_campaigns", status_code=201)
def create_restoration_campaign(payload: RolloutRestorationCampaignCreate):
    return service().create_restoration_campaign(payload.model_dump())


@router.get("/restoration_campaigns")
def list_restoration_campaigns(temple_code: str | None = None, state: str | None = None):
    return {"items": service().list_restoration_campaigns(temple_code, state)}


@router.get("/restoration_campaigns/{restoration_campaign_id}")
def restoration_campaign_detail(restoration_campaign_id: int):
    return service().restoration_campaign_detail(restoration_campaign_id)


@router.post("/restoration_campaigns/{restoration_campaign_id}/start")
def start_restoration_campaign(restoration_campaign_id: int, payload: RestorationCampaignAction):
    return service().start_restoration_campaign(restoration_campaign_id, payload.actor, payload.reason)


@router.post("/restoration_campaigns/{restoration_campaign_id}/pause")
def pause_restoration_campaign(restoration_campaign_id: int, payload: RestorationCampaignAction):
    return service().pause_restoration_campaign(restoration_campaign_id, payload.actor, payload.reason)


@router.post("/restoration_campaigns/{restoration_campaign_id}/complete")
def complete_restoration_campaign(restoration_campaign_id: int, payload: RestorationCampaignAction):
    return service().complete_restoration_campaign(restoration_campaign_id, payload.actor, payload.reason)


@router.post("/closure", status_code=201)
def create_closure(payload: ClosureCreate):
    return service().create_closure(payload.model_dump())


@router.get("/closure/{window_id}")
def closure_detail(window_id: int):
    return service().closure_detail(window_id)


@router.post("/closure/{window_id}/capacity-impacts", status_code=201)
def add_closure_impacts(window_id: int, payload: ClosureImpactCreate):
    """为封闭窗口登记对相关时段/入口/人群的容量收紧。"""
    return TempleBookingService().add_closure_impacts(window_id, [item.model_dump() for item in payload.impacts], payload.actor)


@router.post("/closure/{window_id}/cancel")
def cancel_closure(window_id: int, payload: ClosureAction):
    """取消封闭窗口：释放收紧名额并按规则递补候补。"""
    return service().cancel_closure(window_id, payload.actor, payload.reason)


@router.post("/closure/advance")
def advance_closure(actor: str = Query(default="closure-scheduler", min_length=1)):
    return service().activate_due_closure(actor)
