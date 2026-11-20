"""报表订阅领域服务：订阅配置、推送管理、看板导出链消费。

订阅工作台不自己判断“报表有没有生成”，而是读取即席导出的签收账本：
只有**已签收**的产物才能被交付，且同一份产物在同一周期只消费一次。
文件写完但队列还没签收时，工作台即使到点也必须等下一次签收，不能抢读半成品。
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.services.export_receipts import (
    DELIVERY_DONE,
    STATE_SIGNED,
    current_period,
    receipt_book,
)
from app.store import store


class ReportSubscriptionService(BaseModuleService):
    MODULE = "report_subscription"
    ENTITY = "订阅配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["订阅名称", "报表对象", "推送周期", "推送渠道", "接收人", "推送格式", "最近推送时间"]
    REQUIRED_FIELDS = ["订阅名称", "报表对象", "推送周期"]
    STATUS_ORDER = ["草稿", "已启用", "已暂停", "已停用"]
    ACTION_RULES = {"启用订阅": "已启用", "暂停订阅": "已暂停", "停用订阅": "已停用"}
    NEGATIVE_ACTIONS = ["停用订阅"]
    # 只有已启用会参与月结交付；暂停/停用都不应该继续消费报表。
    PENDING_STATUSES = ["草稿", "已暂停"]

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()

    # ---- 交付状态切换：先核对签收数据，再决定能否继续消费 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        # 状态一变，先按签收账本核对实际生成的报表及周期，避免把已失效的产物
        # 当成“已交付”，也避免暂停后恢复时重复消费同一份文件。
        check = self.reconcile_deliveries(entry_id)
        if check["invalidated"]:
            message += f"；核对发现 {check['invalidated']} 份交付已失效，需重新交付"
        return entry, message

    def reconcile_deliveries(self, subscription_id: int) -> dict[str, Any]:
        """把订阅的交付记录与签收账本逐条核对。

        核对规则：交付记录里的凭证必须仍然存在、仍处于已签收、报表对象一致、
        周期一致，且文件校验值未变；否则这条交付不算数，需要重新交付。
        """
        with self._lock:
            sub = store.find(self.MODULE, subscription_id)
            if sub is None:
                return {"ok": False, "invalidated": 0, "message": "订阅不存在"}
            records = list(sub.get("_deliveries") or [])
            invalidated = 0
            for record in records:
                receipt = receipt_book.get(int(record.get("receipt_id", 0)))
                reason = ""
                if receipt is None:
                    reason = "签收凭证不存在"
                elif receipt.state != STATE_SIGNED or receipt.sign_off_count < 1:
                    reason = f"凭证状态为「{receipt.state}」"
                elif receipt.report_object != str(sub.get("报表对象", "")):
                    reason = "报表对象不一致"
                elif receipt.report_period != record.get("period"):
                    reason = "交付周期与签收周期不一致"
                elif receipt.checksum != record.get("checksum"):
                    reason = "产物已重新生成，原交付文件失效"
                if reason:
                    if record.get("status") != "已失效":
                        invalidated += 1
                    record["status"] = "已失效"
                    record["invalid_reason"] = reason
                elif record.get("status") == "已失效":
                    record["status"] = DELIVERY_DONE
                    record.pop("invalid_reason", None)
            sub["_deliveries"] = records
            sub["_delivery_check"] = {
                "period": current_period(),
                "records": len(records),
                "invalidated": sum(1 for r in records if r.get("status") == "已失效"),
            }
            return {
                "ok": True,
                "invalidated": invalidated,
                "records": len(records),
                "stale": sum(1 for r in records if r.get("status") == "已失效"),
            }

    # ---- 读取签收数据 ----

    def export_receipts(
        self, *, subscription_id: int | None = None,
        report_object: str | None = None, period: str | None = None,
    ) -> dict[str, Any]:
        """订阅工作台读取导出签收数据：按报表对象/周期列出凭证与交付状态。"""
        if subscription_id is not None:
            sub = store.find(self.MODULE, subscription_id)
            if sub is None:
                return {"ok": False, "message": f"订阅 {subscription_id} 不存在"}
            report_object = str(sub.get("报表对象", ""))
        if not report_object:
            return {"ok": False, "message": "缺少报表对象"}

        deliveries = self._delivery_index(subscription_id) if subscription_id is not None else {}
        items: list[dict[str, Any]] = []
        for receipt in receipt_book.for_report(report_object, period=period):
            record = deliveries.get(receipt.receipt_id)
            items.append({
                **receipt.to_dict(),
                "signed": receipt.state == STATE_SIGNED and receipt.sign_off_count > 0,
                "consumed": record is not None and record.get("status") == DELIVERY_DONE,
                "delivery_record_status": (record or {}).get("status"),
            })
        items.sort(key=lambda item: (item["report_period"], item["receipt_id"]))
        return {
            "ok": True,
            "report_object": report_object,
            "period": period or current_period(),
            "items": items,
            "total": len(items),
            "signed_count": sum(1 for item in items if item["signed"]),
            "pending_sign_off": sum(1 for item in items if not item["signed"]),
        }

    def delivery_board(self, subscription_id: int, period: str | None = None) -> dict[str, Any]:
        """工作台面板：本期签收次数、可交付/已交付、实际读取过的文件。"""
        with self._lock:
            sub = store.find(self.MODULE, subscription_id)
            if sub is None:
                return {"ok": False, "message": f"订阅 {subscription_id} 不存在"}
            cycle = period or current_period()
            receipts = receipt_book.for_report(str(sub.get("报表对象", "")), period=cycle)
            records = [
                r for r in (sub.get("_deliveries") or []) if r.get("period") == cycle
            ]
            sign_off_total = sum(r.sign_off_count for r in receipts)
            return {
                "ok": True,
                "subscription": {
                    "id": sub.get("id"),
                    "订阅名称": sub.get("订阅名称"),
                    "报表对象": sub.get("报表对象"),
                    "status": sub.get("status"),
                    "推送周期": sub.get("推送周期"),
                },
                "period": cycle,
                "receipts": [r.to_dict() for r in receipts],
                "sign_off_total": sign_off_total,
                "sign_off_max": max((r.sign_off_count for r in receipts), default=0),
                "deliveries": records,
                "delivery_count": len(records),
                "consumption_total": sum(int(r.get("read_bytes") or 0) > 0 for r in records),
            }

    # ---- 交付（消费） ----

    def deliver(
        self, subscription_id: int, *, period: str | None = None,
        trigger: str = "手动",
    ) -> dict[str, Any]:
        """交付一次报表：只读已签收的产物，同一份产物不重复消费。"""
        with self._lock:
            sub = store.find(self.MODULE, subscription_id)
            if sub is None:
                return {"ok": False, "message": f"订阅 {subscription_id} 不存在", "consumed": False}

            if sub.get("status") != "已启用":
                return {
                    "ok": False,
                    "message": f"订阅当前为「{sub.get('status')}」，不参与交付",
                    "consumed": False,
                    "reason": "not_active",
                }

            cycle = period or current_period()
            report_object = str(sub.get("报表对象", ""))

            # 关键边界：文件写完但未签收的凭证不能交付，等签收完成再来。
            signed = [
                r for r in receipt_book.for_report(report_object, period=cycle)
                if r.state == STATE_SIGNED and r.sign_off_count > 0
            ]
            if not signed:
                unsigned = receipt_book.for_report(report_object, period=cycle)
                reason = "waiting_sign_off" if unsigned else "no_artifact"
                return {
                    "ok": False,
                    "message": (
                        "本期产物尚未签收，工作台不读取未签收文件"
                        if unsigned else "本期还没有生成报表产物"
                    ),
                    "consumed": False,
                    "reason": reason,
                    "waiting_receipts": [r.receipt_id for r in unsigned],
                }

            record = self._delivery_index(subscription_id)
            for receipt in sorted(signed, key=lambda r: r.receipt_id):
                existing = record.get(receipt.receipt_id)
                if existing is not None and existing.get("status") == DELIVERY_DONE                         and existing.get("checksum") == receipt.checksum:
                    # 同一份签收产物已交付：不再读文件、不再累加签收次数。
                    continue
                ok, reason = _read_artifact(receipt)
                if not ok:
                    return {
                        "ok": False,
                        "message": f"产物不可读：{reason}",
                        "consumed": False,
                        "reason": "artifact_unreadable",
                        "receipt_id": receipt.receipt_id,
                    }
                entry = {
                    "period": cycle,
                    "receipt_id": receipt.receipt_id,
                    "task_id": receipt.task_id,
                    "checksum": receipt.checksum,
                    "read_bytes": receipt.file_size_bytes,
                    "artifact_path": receipt.artifact_path,
                    "sign_off_count": receipt.sign_off_count,
                    "delivered_at": _now_iso(),
                    "trigger": trigger,
                    "status": DELIVERY_DONE,
                }
                self._store_delivery(sub, entry)
                receipt_book.mark_delivered(
                    receipt, cycle=cycle, subscription_id=subscription_id,
                    read_bytes=receipt.file_size_bytes, checksum_ok=True,
                )
                sub["最近推送时间"] = entry["delivered_at"][:10]
                return {
                    "ok": True,
                    "message": f"已交付本期报表（周期 {cycle}，签收次数 {receipt.sign_off_count}）",
                    "consumed": True,
                    "period": cycle,
                    "receipt_id": receipt.receipt_id,
                    "task_id": receipt.task_id,
                    "artifact_path": receipt.artifact_path,
                    "read_bytes": receipt.file_size_bytes,
                    "sign_off_count": receipt.sign_off_count,
                    "delivery": entry,
                }

            # 本期所有已签收产物都已交付过：本次一个文件都没读，只回指原交付。
            delivered = self._delivery_index(subscription_id)
            consumed = [delivered[r.receipt_id] for r in signed if r.receipt_id in delivered]
            return {
                "ok": True,
                "message": f"本期报表已交付过，未再次消费（周期 {cycle}）",
                "consumed": False,
                "reason": "already_delivered",
                "period": cycle,
                "receipt_ids": [r.receipt_id for r in signed],
                "read_files": [],
                "previously_delivered": sorted(
                    {str(record.get("artifact_path", "")) for record in consumed}
                ),
                "sign_off_count": max((r.sign_off_count for r in signed), default=0),
            }

    def month_close(self, period: str | None = None) -> dict[str, Any]:
        """月结：对全部已启用订阅按周期交付。重复触发是幂等的。"""
        cycle = period or current_period()
        results: list[dict[str, Any]] = []
        for sub in store.rows(self.MODULE):
            if sub.get("status") != "已启用":
                continue
            result = self.deliver(int(sub.get("id", 0)), period=cycle, trigger="月结")
            results.append({
                "subscription_id": int(sub.get("id", 0)),
                "订阅名称": sub.get("订阅名称"),
                "报表对象": sub.get("报表对象"),
                **result,
            })
        return {
            "ok": True,
            "period": cycle,
            "triggered": len(results),
            "delivered": sum(1 for r in results if r.get("consumed")),
            "skipped": sum(1 for r in results if not r.get("consumed")),
            "results": results,
        }

    # ---- 旧接口保留 ----

    def get_active_subscriptions(self) -> list[dict[str, Any]]:
        """获取所有已启用订阅。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "已启用"
        ]

    def get_subscriptions_for_dashboard(self, dashboard_name: str) -> list[dict[str, Any]]:
        """获取某看板的所有订阅配置（看板布局→报表订阅消费）。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("报表对象") == dashboard_name
        ]

    def simulate_push(self, subscription_id: int) -> dict[str, Any]:
        """模拟推送订阅内容。"""
        sub = store.find(self.MODULE, subscription_id)
        if sub is None:
            return {"ok": False, "message": f"订阅 {subscription_id} 不存在"}
        channel = sub.get("推送渠道", "邮件")
        recipients = str(sub.get("接收人", "")).split(",")
        return {
            "ok": True,
            "subscription_name": sub.get("订阅名称"),
            "report_object": sub.get("报表对象"),
            "channel": channel,
            "recipient_count": len(recipients),
            "recipients": recipients[:3],
            "format": sub.get("推送格式"),
            "message": f"模拟通过{channel}向{len(recipients)}位接收人推送报表",
        }

    def bulk_toggle(self, subscription_ids: list[int], action: str) -> dict[str, Any]:
        """批量启停订阅。"""
        results: list[dict[str, Any]] = []
        for sid in subscription_ids:
            entry, msg = self.run_action(sid, action)
            results.append({"id": sid, "ok": entry is not None, "message": msg})
        return {
            "ok": True,
            "total": len(subscription_ids),
            "results": results,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        base["artifact"] = "报表订阅数据集"
        base["active_count"] = sum(1 for row in rows if row.get("status") == "已启用")
        base["delivery_records"] = sum(len(row.get("_deliveries") or []) for row in rows)
        return base

    # ---- 内部 ----

    def _delivery_index(self, subscription_id: int | None) -> dict[int, dict[str, Any]]:
        if subscription_id is None:
            return {}
        sub = store.find(self.MODULE, subscription_id)
        if sub is None:
            return {}
        return {
            int(record.get("receipt_id", 0)): record
            for record in (sub.get("_deliveries") or [])
        }

    def _store_delivery(self, sub: dict[str, Any], entry: dict[str, Any]) -> None:
        records = list(sub.get("_deliveries") or [])
        # 同一凭证只保留一条交付记录，重复交付是覆盖而不是追加。
        records = [r for r in records if int(r.get("receipt_id", 0)) != entry["receipt_id"]]
        records.append(entry)
        sub["_deliveries"] = records


def _read_artifact(receipt) -> tuple[bool, str]:
    """按签收凭证里的路径读文件，并校验大小与摘要，确保读的是签过的那份。"""
    path = Path(receipt.artifact_path or "")
    if not receipt.artifact_path or not path.exists():
        return False, "文件不存在"
    data = path.read_bytes()
    if len(data) != receipt.file_size_bytes:
        return False, "文件大小与签收记录不一致"
    if hashlib.sha256(data).hexdigest() != receipt.checksum:
        return False, "文件校验值与签收记录不一致"
    return True, "ok"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
