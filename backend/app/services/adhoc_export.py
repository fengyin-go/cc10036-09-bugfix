"""即席导出领域服务：异步任务调度、文件生成、格式转换、签收与恢复。"""
from __future__ import annotations

import csv
import io
import json
import threading
from typing import Any

from app.analytics_engine import ExportScheduler
from app.services.base import BaseModuleService
from app.services.export_receipts import (
    STATE_EXPIRED,
    STATE_OPEN,
    STATE_RERUN,
    STATE_SIGNED,
    STATE_WRITTEN,
    artifact_is_intact,
    current_period,
    receipt_book,
)
from app.store import store


class AdhocExportService(BaseModuleService):
    MODULE = "adhoc_export"
    ENTITY = "导出任务"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["任务名称", "查询SQL", "导出格式", "行数限制", "是否压缩", "分区过滤", "创建时间"]
    REQUIRED_FIELDS = ["任务名称", "查询SQL", "导出格式"]
    STATUS_ORDER = ["排队中", "执行中", "已完成", "导出失败", "已过期"]
    ACTION_RULES = {"开始导出": "执行中", "完成导出": "已完成", "标记失败": "导出失败", "清理过期": "已过期"}
    NEGATIVE_ACTIONS = ["标记失败"]
    # 已完成/已过期是终态，不能再出现在待处理队列里；
    # 导出失败保留待处理，因为它还需要补跑。
    PENDING_STATUSES = ["排队中", "执行中", "导出失败"]

    def __init__(self) -> None:
        super().__init__()
        self._scheduler = ExportScheduler(max_concurrent=3)
        # 认领、写文件、签收是一个跨多步的业务动作，串行化后再放开边界，
        # 避免两个调用方同时把同一份产物写两遍或签两次。
        self._lock = threading.RLock()

    # ---- 动作入口 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """把通用动作接到产物/签收流程上，避免直接改状态绕过文件边界。

        「开始导出」只负责生成文件，任务停在执行中、凭证停在“文件已写完”；
        「完成导出」才做签收并释放队列。两个动作分开，队列就不会在文件写完时
        悄悄把同一笔工作留在里面。
        """
        if action == "开始导出":
            return self.start_export(entry_id, auto_sign_off=False)
        if action == "完成导出":
            return self.sign_off(entry_id)
        if action == "标记失败":
            return self.fail_export(entry_id)
        if action == "清理过期":
            task = self.get_entry(entry_id)
            if task is None:
                return None, f"导出任务 {entry_id} 不存在"
            self._apply_expiry(task)
            return task, "导出任务已清理过期，未签收产物不再补跑"
        return super().run_action(entry_id, action)

    # ---- 执行：认领 → 生成产物 →（可选）签收 ----

    def start_export(
        self,
        task_id: int,
        query_data: list[dict[str, Any]] | None = None,
        *,
        report_object: str | None = None,
        report_period: str | None = None,
        auto_sign_off: bool = True,
        fail_point: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """执行一次导出。返回 (任务, 说明)。

        ``auto_sign_off=False`` 时文件写完就停手，返回“待签收”的执行状态。
        ``fail_point="after_write"`` 额外模拟进程中断（租约不释放），用于验证恢复。
        """
        with self._lock:
            return self._start_export_locked(
                task_id, query_data,
                report_object=report_object,
                report_period=report_period,
                auto_sign_off=auto_sign_off,
                fail_point=fail_point,
            )

    def _start_export_locked(
        self,
        task_id: int,
        query_data: list[dict[str, Any]] | None,
        *,
        report_object: str | None,
        report_period: str | None,
        auto_sign_off: bool,
        fail_point: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        task = store.find(self.MODULE, task_id)
        if task is None:
            return None, f"导出任务 {task_id} 不存在"

        period = report_period or str(task.get("报表周期") or "") or current_period()
        task_name = str(task.get("任务名称", ""))
        report = report_object or str(task.get("报表对象") or task_name)

        # 同一任务同一周期已经签收：完成项不再排队，顺手清掉任何残留 ticket。
        for receipt in receipt_book.for_task(task_id, period=period):
            if receipt.state == STATE_SIGNED:
                self._sync_task_signed(task)
                self._scheduler.complete(str(task_id))
                self._remember_task_report(task, report)
                return task, (
                    f"本期已签收，签收次数 {receipt.sign_off_count}，无需重复导出"
                )

        # 已经有写完但未签收的产物：不再生成第二份，等待签收即可。
        waiting = [
            receipt for receipt in receipt_book.for_task(task_id, period=period)
            if receipt.state == STATE_WRITTEN
        ]
        if waiting:
            receipt = waiting[-1]
            ok, reason = artifact_is_intact(receipt)
            if ok:
                if auto_sign_off:
                    return self._sign_off_receipt(task, receipt)
                self._mark_task_awaiting_sign_off(task)
                self._scheduler.await_sign_off(str(task_id))
                return task, f"产物已写完，等待签收（凭证 {receipt.receipt_id}）"
            receipt_book.mark_rerun(receipt, f"产物不可用：{reason}")

        if self._scheduler.is_running(str(task_id)):
            return task, "导出任务正在执行，未重复入队"

        export_format = str(task.get("导出格式", "csv")).lower()
        row_limit = _parse_int(task.get("行数限制"), 1000)
        compressed = str(task.get("是否压缩", "否")).lower() in ("是", "true", "yes", "1")

        receipt = receipt_book.open(
            task_id=task_id,
            task_name=task_name,
            report_object=report,
            report_period=period,
            export_format=export_format,
            row_limit=row_limit,
        )

        self._scheduler.enqueue(
            task_id=str(task_id),
            priority=1,
            sql=str(task.get("查询SQL", "")),
            format=export_format,
        )
        ticket = self._scheduler.claim(str(task_id))
        if ticket is None:
            return task, "导出队列已满或任务已被认领，本次未执行"

        self._remember_task_report(task, report)
        super().run_action(task_id, "开始导出")

        rows = query_data if query_data is not None else _sample_rows(task)
        try:
            file_content = _generate_export_file(rows[:row_limit], export_format, compressed)
        except Exception as exc:  # 生成失败：释放租约，标记需补跑
            self._scheduler.abandon(str(task_id))
            receipt_book.mark_rerun(receipt, f"文件生成失败：{exc}")
            self._mark_task_failed(task)
            return task, f"导出失败：{exc}"

        receipt_book.mark_written(
            receipt, file_content,
            compressed=compressed,
            row_count=min(len(rows), row_limit),
        )

        if fail_point in ("after_write", "written", "before_sign_off"):
            # 模拟进程中断：租约不释放，任务停在执行中，等待恢复对账。
            receipt.history.append({
                "event": "执行中断",
                "stage": "文件已写完、尚未签收",
                "period": receipt.report_period,
            })
            return task, "执行中断：产物已写完，等待恢复流程核对后签收"

        if not auto_sign_off:
            # 正常的两段式：文件写完，租约转为待签收，任务停在执行中。
            self._scheduler.await_sign_off(str(task_id))
            self._mark_task_awaiting_sign_off(task)
            return task, (
                f"产物已写完（{receipt.file_size_bytes} 字节，周期 {receipt.report_period}），"
                "等待签收"
            )

        return self._sign_off_receipt(task, receipt)

    def sign_off(
        self, task_id: int, receipt_id: int | None = None, *,
        period: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """显式签收：只接受已经写完文件的凭证。"""
        with self._lock:
            return self._sign_off_locked(task_id, receipt_id, period=period)

    def _sign_off_locked(
        self, task_id: int, receipt_id: int | None, *, period: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        task = store.find(self.MODULE, task_id)
        if task is None:
            return None, f"导出任务 {task_id} 不存在"

        receipt = self._pick_receipt(task_id, receipt_id=receipt_id, period=period)
        if receipt is None:
            return task, "没有可签收的产物：请先执行导出，文件写完后再签收"
        if receipt.state == STATE_SIGNED:
            self._sync_task_signed(task)
            self._scheduler.complete(str(task_id))
            return task, f"该产物已于 {receipt.signed_off_at} 签收，本次未重复签收"
        if receipt.state != STATE_WRITTEN:
            return task, f"产物状态为「{receipt.state}」，不能签收"
        return self._sign_off_receipt(task, receipt)

    def fail_export(self, task_id: int) -> tuple[dict[str, Any] | None, str]:
        """标记失败：释放租约，已有产物作废并转为需补跑。"""
        with self._lock:
            return self._fail_export_locked(task_id)

    def _fail_export_locked(self, task_id: int) -> tuple[dict[str, Any] | None, str]:
        task = store.find(self.MODULE, task_id)
        if task is None:
            return None, f"导出任务 {task_id} 不存在"
        for receipt in receipt_book.for_task(task_id):
            if receipt.state in (STATE_OPEN, STATE_WRITTEN):
                receipt_book.mark_rerun(receipt, "人工标记失败")
        self._scheduler.abandon(str(task_id))
        self._mark_task_failed(task)
        return task, "导出任务已标记失败，产物需补跑"

    # ---- 恢复与对账 ----

    def recover(self) -> dict[str, Any]:
        """中断后的对账入口：核对产物、补签收、清理残留队列、标出真正要补跑的工作。"""
        with self._lock:
            return self._recover_locked()

    def invalidate(
        self, task_id: int, receipt_id: int, reason: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """把一张凭证的产物判为不可用，任务转为需补跑。"""
        with self._lock:
            task = store.find(self.MODULE, task_id)
            if task is None:
                return None, f"导出任务 {task_id} 不存在"
            receipt = receipt_book.get(receipt_id)
            if receipt is None or receipt.task_id != task_id:
                return task, f"凭证 {receipt_id} 不属于导出任务 {task_id}"
            self._mark_receipt_rerun(receipt, task, reason)
            return task, f"产物已作废：{reason}，任务转为需补跑"

    def _recover_locked(self) -> dict[str, Any]:
        dispositions: list[dict[str, Any]] = []

        # 先清理残留队列，再对账；否则签收时顺手释放的 ticket 会掩盖“完成项
        # 曾经留在队列里”这个事实，恢复报告就说不清自己修过什么。
        dispositions.extend(self._sweep_queue())

        for receipt in receipt_book.all():
            task = store.find(self.MODULE, receipt.task_id)
            if task is None:
                dispositions.append(_disposition(receipt, "任务缺失", "等待人工核对"))
                continue

            if receipt.state == STATE_SIGNED:
                ok, reason = artifact_is_intact(receipt)
                if ok:
                    self._sync_task_signed(task)
                    self._scheduler.complete(str(receipt.task_id))
                    dispositions.append(_disposition(receipt, "签收有效", "已完成"))
                else:
                    self._mark_receipt_rerun(receipt, task, f"已签收但产物不可用：{reason}")
                    dispositions.append(_disposition(receipt, "签收无效", "需补跑"))
                continue

            if receipt.state == STATE_WRITTEN:
                ok, reason = artifact_is_intact(receipt)
                if ok:
                    _, message = self._sign_off_receipt(task, receipt)
                    dispositions.append(_disposition(
                        receipt, "补签收", "已完成", message,
                    ))
                else:
                    self._mark_receipt_rerun(receipt, task, f"产物不可用：{reason}")
                    dispositions.append(_disposition(receipt, "产物不可用", "需补跑"))
                continue

            if receipt.state in (STATE_RERUN, STATE_EXPIRED):
                # 已经核过的问题项：只释放租约，不重复计数、不再排队。
                self._scheduler.abandon(str(receipt.task_id))
                self._sync_task_after_rerun(task)
                dispositions.append(_disposition(receipt, "等待补跑", "需补跑"))
                continue

            # STATE_OPEN：没有产物。仍在执行的保留租约，其余视为中断。
            if self._scheduler.is_running(str(receipt.task_id)):
                dispositions.append(_disposition(receipt, "仍在执行", "执行中"))
                continue
            self._mark_receipt_rerun(receipt, task, "执行中断且没有产物文件")
            dispositions.append(_disposition(receipt, "无产物", "需补跑"))

        # 已完成但没有签收凭证的任务：只报告，不重新排队。
        for task in store.rows(self.MODULE):
            if task.get("status") != "已完成":
                continue
            if any(r.state == STATE_SIGNED for r in receipt_book.for_task(int(task.get("id", 0)))):
                continue
            dispositions.append({
                "task_id": int(task.get("id", 0)),
                "task_name": str(task.get("任务名称", "")),
                "receipt_id": None,
                "state": "已完成",
                "disposition": "完成但缺少签收凭证",
                "detail": "不重新排队，请核对历史产物来源",
            })

        snapshot = self._scheduler.inspect()
        return {
            "ok": True,
            "period": current_period(),
            "dispositions": dispositions,
            "signed_off": sum(1 for d in dispositions if d["state"] == "补签收"),
            "needs_rerun": sum(1 for d in dispositions if d.get("action") == "需补跑"),
            "queue_size": snapshot["queue_size"],
            "running_count": snapshot["running_count"],
        }

    def _sweep_queue(self) -> list[dict[str, Any]]:
        """清掉不该继续排队/占租约的 ticket，完成项不能再排队。"""
        swept: list[dict[str, Any]] = []
        seen: set[int] = set()
        snapshot = self._scheduler.inspect()
        for item in snapshot["queue"] + snapshot["running"] + snapshot["awaiting"]:
            task_id = int(item["task_id"])
            if task_id in seen:
                continue
            seen.add(task_id)
            task = store.find(self.MODULE, task_id)
            signed = any(
                r.state == STATE_SIGNED for r in receipt_book.for_task(task_id)
            )
            terminal = task is not None and task.get("status") in ("已完成", "已过期")
            if signed or terminal or task is None:
                self._scheduler.complete(str(task_id))
                swept.append({
                    "task_id": task_id,
                    "task_name": str((task or {}).get("任务名称", "")),
                    "receipt_id": None,
                    "state": "Queue",
                    "disposition": "清理残留队列",
                    "action": "已清理",
                    "detail": "任务已签收或已终态，不再排队",
                })
        return swept

    # ---- 详情与队列 ----

    def detail(self, task_id: int) -> dict[str, Any]:
        """导出详情：产物、签收、待处理项和队列位置放在一起。"""
        with self._lock:
            return self._detail_locked(task_id)

    def _detail_locked(self, task_id: int) -> dict[str, Any]:
        task = self.get_entry(task_id)
        if task is None:
            return {"ok": False, "message": f"导出任务 {task_id} 不存在"}

        receipts = receipt_book.for_task(task_id)
        artifacts = []
        for receipt in receipts:
            intact, reason = artifact_is_intact(receipt) if receipt.artifact_path else (False, "尚未生成产物")
            data = receipt.to_dict()
            data["artifact_intact"] = intact
            data["artifact_check"] = reason
            artifacts.append(data)

        pending_items: list[dict[str, Any]] = []
        snapshot = self._scheduler.inspect()
        for item in snapshot["queue"]:
            if int(item["task_id"]) == task_id:
                pending_items.append({
                    "kind": "队列等待",
                    "info": item,
                    "reason": "ticket 尚未被认领",
                })
        for item in snapshot["running"]:
            if int(item["task_id"]) == task_id:
                pending_items.append({
                    "kind": "执行租约",
                    "info": item,
                    "reason": "已认领，正在生成产物",
                })
        for item in snapshot["awaiting"]:
            if int(item["task_id"]) == task_id:
                pending_items.append({
                    "kind": "待签收",
                    "info": item,
                    "reason": "文件已写完，等待签收后才算完成",
                })
        for receipt in receipts:
            # 已写完的凭证已经由调度器的“待签收”档代表；这里只补跑/开立中的，
            # 否则详情页会把同一件事数两遍。
            if receipt.state in (STATE_SIGNED, STATE_WRITTEN):
                continue
            pending_items.append({
                "kind": "未签收产物",
                "receipt_id": receipt.receipt_id,
                "state": receipt.state,
                "reason": receipt.rerun_reason or "产物尚未签收",
            })

        return {
            "ok": True,
            "task": task,
            "report_object": next(
                (r.report_object for r in reversed(receipts)), str(task.get("报表对象") or task.get("任务名称", ""))
            ),
            "artifacts": artifacts,
            "pending_items": pending_items,
            "pending_count": len(pending_items),
            "queue": {
                "queue_size": snapshot["queue_size"],
                "running_count": snapshot["running_count"],
                "awaiting_count": snapshot["awaiting_count"],
                "pending_count": snapshot["pending_count"],
            },
        }

    def queue_status(self) -> dict[str, Any]:
        snapshot = self._scheduler.inspect()
        snapshot.update({
            "ok": True,
            "signed_receipts": sum(1 for r in receipt_book.all() if r.state == STATE_SIGNED),
            "needs_rerun": len(receipt_book.needs_rerun()),
        })
        return snapshot

    def receipts(self, *, task_id: int | None = None, period: str | None = None) -> dict[str, Any]:
        rows = receipt_book.for_task(task_id, period=period) if task_id is not None else receipt_book.all()
        if task_id is None and period is not None:
            rows = [r for r in rows if r.report_period == period]
        return {"ok": True, "items": [r.to_dict() for r in rows], "total": len(rows)}

    # ---- 内部 ----

    def _pick_receipt(self, task_id: int, *, receipt_id: int | None, period: str | None):
        if receipt_id is not None:
            receipt = receipt_book.get(receipt_id)
            return receipt if receipt and receipt.task_id == task_id else None
        candidates = receipt_book.for_task(task_id, period=period)
        if not candidates:
            return None
        return max(candidates, key=lambda r: (r.state == STATE_SIGNED, r.receipt_id))

    def _sign_off_receipt(self, task: dict[str, Any], receipt) -> tuple[dict[str, Any], str]:
        ok, reason = artifact_is_intact(receipt)
        if not ok:
            self._mark_receipt_rerun(receipt, task, f"产物不可用：{reason}")
            return task, f"签收失败：{reason}，已标记补跑"
        try:
            receipt, duplicate = receipt_book.sign_off(receipt)
        except ValueError as exc:
            return task, str(exc)
        self._sync_task_signed(task)
        self._scheduler.complete(str(task["id"]))
        if duplicate:
            return task, f"该产物已签收，签收次数 {receipt.sign_off_count}"
        return task, (
            f"导出完成并已签收（周期 {receipt.report_period}，"
            f"第 {receipt.sign_off_count} 次签收）"
        )

    def _sync_task_signed(self, task: dict[str, Any]) -> None:
        # 已清理过期的任务不因历史签收凭证复活；过期是人工/策略结论，
        # 只说明“这份产物已经归档”，不代表工作又回到了待办队列。
        if task.get("status") == "已过期":
            return
        task["status"] = "已完成"
        task["pending"] = False
        task["abnormal"] = False

    def _mark_task_failed(self, task: dict[str, Any]) -> None:
        task["status"] = "导出失败"
        task["pending"] = True
        task["abnormal"] = True

    def _mark_task_awaiting_sign_off(self, task: dict[str, Any]) -> None:
        """文件写完但还没签收：任务保持执行中，签收后才会转已完成。"""
        task["status"] = "执行中"
        task["pending"] = True
        task["abnormal"] = False

    def _sync_task_after_rerun(self, task: dict[str, Any]) -> None:
        """补跑项要能被再次执行：任务回到执行中并继续进入待处理。"""
        if task.get("status") in ("导出失败", "已完成", "已过期"):
            task["status"] = "执行中"
        task["pending"] = True

    def _apply_expiry(self, task: dict[str, Any]) -> None:
        """清理过期：任务与未签收凭证一起进入过期，队列不再保留。"""
        task["status"] = "已过期"
        task["pending"] = False
        task["abnormal"] = False
        self._scheduler.abandon(str(task.get("id", "")))
        for receipt in receipt_book.for_task(int(task.get("id", 0))):
            if receipt.state != STATE_SIGNED:
                receipt_book.mark_expired(receipt, "任务已清理过期")

    def _mark_receipt_rerun(self, receipt, task: dict[str, Any], reason: str) -> None:
        receipt_book.mark_rerun(receipt, reason)
        self._scheduler.abandon(str(task.get("id", receipt.task_id)))
        self._mark_task_failed(task)

    def _remember_task_report(self, task: dict[str, Any], report: str) -> None:
        # 记在任务上供列表/流水线复用，但不进入 LIST_FIELDS，避免表单多一个必填项。
        task["报表对象"] = report

    # ---- 兼容旧接口 ----

    def submit_export(
        self, task_id: int, query_data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """提交导出任务并执行（看板流水线使用的兼容入口）。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"导出任务 {task_id} 不存在"}
        _, message = self.start_export(task_id, query_data)
        receipts = receipt_book.for_task(task_id)
        receipt = max(receipts, key=lambda r: r.receipt_id) if receipts else None
        if receipt is None or receipt.state != STATE_SIGNED:
            return {"ok": False, "message": message}
        entry = task
        return {
            "ok": True,
            "task_id": task_id,
            "task_name": str(entry.get("任务名称", "")),
            "format": receipt.export_format,
            "row_count": receipt.row_count,
            "compressed": receipt.compressed,
            "file_size_bytes": receipt.file_size_bytes,
            "content_preview": receipt.content_preview,
            "receipt_id": receipt.receipt_id,
            "report_period": receipt.report_period,
            "sign_off_count": receipt.sign_off_count,
        }

    def batch_export(
        self, task_ids: list[int], query_data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """批量提交导出任务。"""
        results: list[dict[str, Any]] = []
        for tid in task_ids:
            result = self.submit_export(tid, query_data)
            results.append(result)
        success = sum(1 for r in results if r.get("ok"))
        return {
            "ok": True,
            "total_tasks": len(task_ids),
            "success_count": success,
            "fail_count": len(task_ids) - success,
            "results": results,
        }

    def get_queue_status(self) -> dict[str, Any]:
        """获取导出队列状态。"""
        return self.queue_status()

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        snapshot = self._scheduler.inspect()
        base["artifact"] = "导出任务数据集"
        base["queue_size"] = snapshot["queue_size"]
        base["running_count"] = snapshot["running_count"]
        base["signed_receipts"] = sum(1 for r in receipt_book.all() if r.state == STATE_SIGNED)
        return base


def _disposition(
    receipt, state: str, action: str, detail: str = "",
) -> dict[str, Any]:
    """恢复对账的一条结论：发现了什么、如何处理、依据是什么。"""
    return {
        "task_id": receipt.task_id,
        "task_name": receipt.task_name,
        "receipt_id": receipt.receipt_id,
        "period": receipt.report_period,
        "state": state,
        "disposition": action,
        "action": action,
        "detail": detail or receipt.rerun_reason,
    }


def _parse_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _sample_rows(task: dict[str, Any]) -> list[dict[str, Any]]:
    """没有传入查询结果时，按任务生成一份可复现的示例数据。"""
    limit = min(_parse_int(task.get("行数限制"), 1000), 100)
    task_id = int(task.get("id", 0))
    return [
        {
            "task_id": task_id,
            "row_no": i + 1,
            "city": ["北京", "上海", "广州", "深圳"][i % 4],
            "dau": 1000 + task_id * 10 + i,
        }
        for i in range(max(limit, 1))
    ]


def _generate_export_file(
    data: list[dict[str, Any]], format: str, compressed: bool,
) -> str | bytes:
    """生成导出文件内容。"""
    if not data:
        return "" if format == "csv" else "[]"

    if format == "csv":
        output = io.StringIO()
        output.write("﻿")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(list(data[0].keys()))
        for row in data:
            writer.writerow(list(row.values()))
        return output.getvalue()

    elif format in ("json", "jsonl"):
        if format == "jsonl":
            return "\n".join(json.dumps(row) for row in data)
        return json.dumps(data, ensure_ascii=False, indent=2)

    elif format == "parquet":
        # 简化parquet: JSON序列化
        return json.dumps(data, ensure_ascii=False).encode()

    return json.dumps(data)
