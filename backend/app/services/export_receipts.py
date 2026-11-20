"""导出签收账本：把“文件写完”和“队列签收”之间的边界落成可核对的数据。

即席导出的执行分三步：认领（建立租约）→ 生成产物（文件落盘）→ 签收（写回任务状态
并释放队列）。三步之间任何一步中断，账本都要能回答三个问题：

1. 这笔工作到底有没有产出文件（产物在哪、多大、校验值是多少）；
2. 签收过没有、签收在哪个周期、签收了几次；
3. 哪些工作真正需要补跑（产物缺失/不完整），哪些只是状态没写回（可原地补签收）。

账本是进程内的，但产物文件和状态更新是分开写入的，因此可以在任意一步中断后
用 ``recover`` 对账，不依赖调用方记住进度。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 签收状态：描述产物与队列之间的关系，而不是简单的任务状态。
STATE_OPEN = "待执行"          # 已认领，还没有产物
STATE_WRITTEN = "文件已写完"    # 产物落盘，尚未签收（中断高发点）
STATE_SIGNED = "已签收"        # 状态与队列都已确认，工作完成
STATE_RERUN = "需补跑"         # 产物不可用，需要重新执行
STATE_EXPIRED = "已过期"       # 超过保留期限，不再补跑

# 交付状态：订阅工作台对签收产物的消费标记。
DELIVERY_PENDING = "待交付"
DELIVERY_DONE = "已交付"
DELIVERY_SKIPPED = "无需交付"


def artifact_root() -> Path:
    """产物目录：默认放临时目录，部署时可用 EXPORT_ARTIFACT_DIR 指定持久盘。"""
    root = Path(os.environ.get("EXPORT_ARTIFACT_DIR") or (Path.home() / ".cache" / "cc10036" / "exports"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def current_period(now: datetime | None = None) -> str:
    """本期周期标签，月结按自然月：2026-11。

    调用方可以显式传周期（回补历史月结），缺省就是当月。
    """
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ExportReceipt:
    """一次导出产出的签收凭证。"""

    receipt_id: int
    task_id: int
    task_name: str
    report_object: str
    report_period: str
    export_format: str
    row_limit: int
    state: str = STATE_OPEN
    row_count: int = 0
    compressed: bool = False
    file_size_bytes: int = 0
    checksum: str = ""
    artifact_path: str = ""
    content_preview: str = ""
    written_at: str | None = None
    signed_off_at: str | None = None
    sign_off_count: int = 0
    delivery_status: str = DELIVERY_PENDING
    delivered_at: str | None = None
    delivery_cycle: str = ""
    consumption_count: int = 0
    needs_rerun: bool = False
    rerun_reason: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "report_object": self.report_object,
            "report_period": self.report_period,
            "export_format": self.export_format,
            "row_limit": self.row_limit,
            "row_count": self.row_count,
            "compressed": self.compressed,
            "file_size_bytes": self.file_size_bytes,
            "checksum": self.checksum,
            "artifact_path": self.artifact_path,
            "content_preview": self.content_preview,
            "written_at": self.written_at,
            "signed_off_at": self.signed_off_at,
            "sign_off_count": self.sign_off_count,
            "state": self.state,
            "delivery_status": self.delivery_status,
            "delivered_at": self.delivered_at,
            "delivery_cycle": self.delivery_cycle,
            "consumption_count": self.consumption_count,
            "needs_rerun": self.needs_rerun,
            "rerun_reason": self.rerun_reason,
            "history": list(self.history),
        }


class ExportReceiptBook:
    """导出签收账本：线程安全，按 (任务, 周期) 唯一。"""

    def __init__(self) -> None:
        self._receipts: dict[int, ExportReceipt] = {}
        self._next_id = 1
        self._lock = threading.RLock()

    # ---- 生命周期 ----

    def open(
        self,
        *, task_id: int, task_name: str, report_object: str = "",
        report_period: str | None = None, export_format: str = "csv",
        row_limit: int = 1000,
    ) -> ExportReceipt:
        """开出一张新凭证；同一任务同一周期的未终结凭证直接复用。"""
        period = report_period or current_period()
        with self._lock:
            for receipt in self._receipts.values():
                if (
                    receipt.task_id == task_id
                    and receipt.report_period == period
                    and receipt.state in (STATE_OPEN, STATE_WRITTEN, STATE_RERUN)
                ):
                    return receipt
            receipt = ExportReceipt(
                receipt_id=self._next_id,
                task_id=task_id,
                task_name=task_name,
                report_object=report_object or task_name,
                report_period=period,
                export_format=export_format,
                row_limit=row_limit,
            )
            self._next_id += 1
            receipt.history.append({"at": _now_iso(), "event": "开立凭证", "period": period})
            self._receipts[receipt.receipt_id] = receipt
            return receipt

    def mark_written(
        self, receipt: ExportReceipt, content: str | bytes, *, compressed: bool,
        row_count: int,
    ) -> ExportReceipt:
        """把产物写进独立文件；写完才允许签收。

        文件写入本身是幂等的：同名覆盖并重新计算校验值，重跑不会留下半截文件。
        """
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        suffix = ".parquet" if receipt.export_format == "parquet" else f".{receipt.export_format}"
        target = artifact_root() / f"task-{receipt.task_id}-{receipt.report_period}-r{receipt.receipt_id}{suffix}"
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)  # 原子替换：读方永远看不到写了一半的文件

        with self._lock:
            receipt.artifact_path = str(target)
            receipt.file_size_bytes = len(data)
            receipt.checksum = hashlib.sha256(data).hexdigest()
            receipt.row_count = row_count
            receipt.compressed = compressed
            preview = content if isinstance(content, str) else content.decode("utf-8", errors="replace")
            receipt.content_preview = preview[:500]
            receipt.written_at = _now_iso()
            receipt.state = STATE_WRITTEN
            receipt.needs_rerun = False
            receipt.rerun_reason = ""
            receipt.history.append({
                "at": receipt.written_at,
                "event": "产物写完",
                "bytes": receipt.file_size_bytes,
                "checksum": receipt.checksum[:12],
            })
            return receipt

    def sign_off(self, receipt: ExportReceipt, *, cycle: str | None = None) -> tuple[ExportReceipt, bool]:
        """签收产物。重复签收不改变签收时间，也不增加次数。返回 (凭证, 是否重复)。"""
        with self._lock:
            if receipt.state == STATE_SIGNED and receipt.sign_off_count > 0:
                return receipt, True
            if receipt.state != STATE_WRITTEN or not receipt.artifact_path:
                raise ValueError(f"凭证 {receipt.receipt_id} 尚未产出文件，不能签收")
            receipt.state = STATE_SIGNED
            receipt.signed_off_at = _now_iso()
            receipt.sign_off_count += 1
            receipt.report_period = cycle or receipt.report_period
            receipt.needs_rerun = False
            receipt.rerun_reason = ""
            receipt.history.append({
                "at": receipt.signed_off_at,
                "event": "签收",
                "period": receipt.report_period,
            })
            return receipt, False

    def mark_rerun(self, receipt: ExportReceipt, reason: str) -> ExportReceipt:
        """产物不可用：标记为需要补跑，并作废原有交付结论。"""
        with self._lock:
            if receipt.state == STATE_EXPIRED:
                return receipt
            receipt.state = STATE_RERUN
            receipt.needs_rerun = True
            receipt.rerun_reason = reason
            receipt.delivery_status = DELIVERY_PENDING
            receipt.history.append({"at": _now_iso(), "event": "标记补跑", "reason": reason})
            return receipt

    def mark_expired(self, receipt: ExportReceipt, reason: str = "") -> ExportReceipt:
        """任务已过期：凭证不再参与补跑，也不应继续排队。"""
        with self._lock:
            receipt.state = STATE_EXPIRED
            receipt.needs_rerun = False
            receipt.rerun_reason = reason or "任务已过期"
            receipt.delivery_status = DELIVERY_SKIPPED
            receipt.history.append({"at": _now_iso(), "event": "过期", "reason": receipt.rerun_reason})
            return receipt

    def mark_delivered(
        self, receipt: ExportReceipt, *, cycle: str, subscription_id: int,
        read_bytes: int, checksum_ok: bool,
    ) -> ExportReceipt:
        with self._lock:
            receipt.delivery_status = DELIVERY_DONE
            receipt.delivery_cycle = cycle
            receipt.delivered_at = _now_iso()
            receipt.consumption_count += 1
            receipt.history.append({
                "at": receipt.delivered_at,
                "event": "订阅交付",
                "cycle": cycle,
                "subscription_id": subscription_id,
                "read_bytes": read_bytes,
                "checksum_ok": checksum_ok,
            })
            return receipt

    # ---- 查询 ----

    def get(self, receipt_id: int) -> ExportReceipt | None:
        with self._lock:
            return self._receipts.get(receipt_id)

    def for_task(self, task_id: int, *, period: str | None = None) -> list[ExportReceipt]:
        with self._lock:
            return [
                receipt for receipt in self._receipts.values()
                if receipt.task_id == task_id and (period is None or receipt.report_period == period)
            ]

    def for_report(self, report_object: str, *, period: str | None = None) -> list[ExportReceipt]:
        with self._lock:
            return [
                receipt for receipt in self._receipts.values()
                if receipt.report_object == report_object
                and (period is None or receipt.report_period == period)
            ]

    def all(self) -> list[ExportReceipt]:
        with self._lock:
            return list(self._receipts.values())

    def pending(self) -> list[ExportReceipt]:
        """尚未完成签收的凭证：详情页的“待处理项”。"""
        return [r for r in self.all() if r.state not in (STATE_SIGNED, STATE_EXPIRED)]

    def needs_rerun(self) -> list[ExportReceipt]:
        """真正需要补跑的凭证：产物缺失或校验不通过。"""
        return [r for r in self.all() if r.needs_rerun and r.state != STATE_EXPIRED]

    def unclaimed_work(self, task_ids: list[int]) -> list[dict[str, Any]]:
        """对照任务表和账本，找出“状态未终结、但账本已签收”的残留工作标识。"""
        stale: list[dict[str, Any]] = []
        by_task: dict[int, ExportReceipt] = {}
        for receipt in self.all():
            if receipt.state == STATE_SIGNED and receipt.sign_off_count > 0:
                by_task[receipt.task_id] = receipt
        for task_id in task_ids:
            receipt = by_task.get(task_id)
            if receipt is not None:
                stale.append({
                    "task_id": task_id,
                    "receipt_id": receipt.receipt_id,
                    "signed_off_at": receipt.signed_off_at,
                })
        return stale


def artifact_is_intact(receipt: ExportReceipt) -> tuple[bool, str]:
    """核对凭证对应的文件是否仍然完整。"""
    if not receipt.artifact_path:
        return False, "没有产物文件"
    path = Path(receipt.artifact_path)
    if not path.exists():
        return False, f"产物文件不存在：{receipt.artifact_path}"
    data = path.read_bytes()
    if len(data) != receipt.file_size_bytes:
        return False, f"产物大小不一致：{len(data)} != {receipt.file_size_bytes}"
    digest = hashlib.sha256(data).hexdigest()
    if digest != receipt.checksum:
        return False, "产物校验值不一致"
    return True, "ok"


receipt_book = ExportReceiptBook()
