"""即席导出与报表订阅的编排接口：详情、恢复、签收数据、月结交付。

资源路由只做通用增删改查；这里的接口表达的是“产物 → 签收 → 交付”这条跨模块链路，
所以单独挂载，并按领域服务完成业务判断。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api/workflow", tags=["导出签收与订阅交付"])


def _export_service():
    service = service_for("adhoc_export")
    if service is None:
        raise HTTPException(status_code=503, detail="即席导出服务未就绪")
    return service


def _subscription_service():
    service = service_for("report_subscription")
    if service is None:
        raise HTTPException(status_code=503, detail="报表订阅服务未就绪")
    return service


class ExportRunPayload(BaseModel):
    """执行一次导出。query_data 为空时由任务生成可复现示例数据。"""

    task_id: int
    query_data: list[dict[str, Any]] | None = None
    report_object: str | None = None
    report_period: str | None = None
    # 仅用于验证/演练：after_write 会把执行停在“文件已写完、尚未签收”。
    fail_point: str | None = None


class InvalidatPayload(BaseModel):
    reason: str = Field(default="产物校验失败")


class DeliverPayload(BaseModel):
    subscription_id: int
    period: str | None = None
    trigger: str = "手动"


class MonthClosePayload(BaseModel):
    period: str | None = None


@router.post("/adhoc-export/run")
def run_export(payload: ExportRunPayload) -> dict[str, Any]:
    service = _export_service()
    entry, message = service.start_export(
        payload.task_id,
        payload.query_data,
        report_object=payload.report_object,
        report_period=payload.report_period,
        fail_point=payload.fail_point,
    )
    return {"ok": entry is not None, "message": message, "task": entry}


@router.get("/adhoc-export/{task_id}/detail")
def export_detail(task_id: int) -> dict[str, Any]:
    """导出详情页：产物、签收次数、待处理项、队列位置。"""
    return _export_service().detail(task_id)


@router.post("/adhoc-export/{task_id}/sign-off")
def sign_off(
    task_id: int,
    receipt_id: int | None = Query(default=None),
    period: str | None = Query(default=None),
) -> dict[str, Any]:
    service = _export_service()
    entry, message = service.sign_off(task_id, receipt_id, period=period)
    return {"ok": entry is not None, "message": message, "task": entry}


@router.post("/adhoc-export/{task_id}/invalidate/{receipt_id}")
def invalidate(task_id: int, receipt_id: int, payload: InvalidatPayload) -> dict[str, Any]:
    service = _export_service()
    entry, message = service.invalidate(task_id, receipt_id, payload.reason)
    return {"ok": entry is not None, "message": message, "task": entry}


@router.post("/adhoc-export/recover")
def recover() -> dict[str, Any]:
    """中断恢复：核对原签收、补签收、清理残留队列、列出真正需要补跑的项。"""
    return _export_service().recover()


@router.get("/adhoc-export/receipts")
def receipts(
    task_id: int | None = Query(default=None),
    period: str | None = Query(default=None),
) -> dict[str, Any]:
    """导出签收数据：供报表订阅工作台读取。"""
    return _export_service().receipts(task_id=task_id, period=period)


@router.get("/adhoc-export/queue")
def queue_status() -> dict[str, Any]:
    return _export_service().queue_status()


@router.get("/report-subscription/{subscription_id}/receipts")
def subscription_receipts(
    subscription_id: int, period: str | None = Query(default=None),
) -> dict[str, Any]:
    """订阅工作台读取对应报表的签收数据，核对实际生成的文件及周期。"""
    return _subscription_service().export_receipts(
        subscription_id=subscription_id, period=period)


@router.get("/report-subscription/{subscription_id}/board")
def subscription_board(
    subscription_id: int, period: str | None = Query(default=None),
) -> dict[str, Any]:
    """工作台面板：本期签收次数、交付记录、读取过的文件。"""
    return _subscription_service().delivery_board(subscription_id, period)


@router.post("/report-subscription/deliver")
def deliver(payload: DeliverPayload) -> dict[str, Any]:
    return _subscription_service().deliver(
        payload.subscription_id, period=payload.period, trigger=payload.trigger)


@router.post("/report-subscription/month-close")
def month_close(payload: MonthClosePayload | None = None) -> dict[str, Any]:
    """月结：对全部已启用订阅交付本期报表；重复触发幂等。"""
    return _subscription_service().month_close((payload.period if payload else None))
