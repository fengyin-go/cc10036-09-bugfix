"""导出签收与订阅交付的领域测试：直接驱动服务层，不依赖 HTTP 和端口。

覆盖题面场景：
- 文件写完但未签收时，任务、签名数据和队列三者的关系；
- 两个动作交错（导出写完、队列未签收、订阅到点取文件）；
- 中断恢复回到可说明状态、核对原签收、完成项不再排队；
- 订阅工作台按签收数据交付，周期不符不消费，月结重复触发幂等。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 把产物目录指向独立临时目录，避免测试污染真实缓存，也避免测试之间互相看到文件。
_ARTIFACT_DIR = tempfile.mkdtemp(prefix="cc10036-test-artifacts-")
os.environ["EXPORT_ARTIFACT_DIR"] = _ARTIFACT_DIR

from app.services import SERVICES, service_for  # noqa: E402
from app.services.export_receipts import (  # noqa: E402
    STATE_RERUN,
    STATE_SIGNED,
    STATE_WRITTEN,
    artifact_is_intact,
    receipt_book,
)
from app.store import store  # noqa: E402

PERIOD = "2026-11"


class WorkflowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        """每个用例重置账本、队列和种子数据，保证用例互不影响。"""
        store._tables = {
            name: [dict(row) for row in rows]
            for name, rows in __import__("app.seed", fromlist=["SEED_ROWS"]).SEED_ROWS.items()
        }
        receipt_book._receipts.clear()
        receipt_book._next_id = 1
        for service in SERVICES.values():
            if hasattr(service, "_scheduler"):
                scheduler = service._scheduler
                scheduler._queue.clear()
                scheduler._running.clear()
                scheduler._awaiting.clear()

        self.export = service_for("adhoc_export")
        self.subscription = service_for("report_subscription")

    # ---- 场景 1：写完到签收的边界 ----

    def test_write_stops_before_sign_off_and_queue_is_released_from_execution(self):
        task, message = self.export.start_export(
            2, auto_sign_off=False, report_period=PERIOD,
        )
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "执行中")
        self.assertTrue(task["pending"])
        self.assertIn("等待签收", message)

        receipts = receipt_book.for_task(2, period=PERIOD)
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertEqual(receipt.state, STATE_WRITTEN)
        self.assertEqual(receipt.sign_off_count, 0)
        self.assertTrue(receipt.artifact_path and Path(receipt.artifact_path).exists())

        queue = self.export.queue_status()
        self.assertEqual(queue["queue_size"], 0)       # 不再占可执行队列
        self.assertEqual(queue["running_count"], 0)    # 租约已结束
        self.assertEqual(queue["awaiting_count"], 1)   # 但仍在等签收

        detail = self.export.detail(2)
        self.assertEqual(detail["pending_count"], 1)
        self.assertEqual(detail["pending_items"][0]["kind"], "待签收")

    def test_sign_off_is_idempotent_and_releases_everything(self):
        self.export.start_export(2, auto_sign_off=False, report_period=PERIOD)
        task, message = self.export.run_action(2, "完成导出")
        self.assertEqual(task["status"], "已完成")
        self.assertFalse(task["pending"])
        self.assertIn("第 1 次签收", message)

        receipt = receipt_book.for_task(2, period=PERIOD)[0]
        self.assertEqual(receipt.state, STATE_SIGNED)
        self.assertEqual(receipt.sign_off_count, 1)
        signed_at = receipt.signed_off_at

        task, message = self.export.run_action(2, "完成导出")
        receipt = receipt_book.for_task(2, period=PERIOD)[0]
        self.assertEqual(receipt.sign_off_count, 1)      # 重复签收不累加
        self.assertEqual(receipt.signed_off_at, signed_at)
        self.assertIn("未重复签收", message)

        queue = self.export.queue_status()
        self.assertEqual(
            (queue["queue_size"], queue["running_count"], queue["awaiting_count"]),
            (0, 0, 0),
        )

    def test_start_after_signed_off_does_not_requeue_finished_work(self):
        self.export.start_export(1, report_period=PERIOD)
        task, message = self.export.start_export(1, report_period=PERIOD)
        self.assertIn("已签收", message)
        self.assertEqual(task["pending"], False)
        self.assertEqual(self.export.queue_status()["pending_count"], 0)

    # ---- 场景 2：写完、未签收的两个动作交错 ----

    def test_subscription_refuses_to_read_file_that_is_written_but_unsigned(self):
        self.export.start_export(
            2, auto_sign_off=False, report_period=PERIOD,
            report_object="营收分析看板",
        )
        result = self.subscription.deliver(2, period=PERIOD)
        self.assertFalse(result["ok"])
        self.assertFalse(result["consumed"])
        self.assertEqual(result["reason"], "waiting_sign_off")
        self.assertEqual(result["waiting_receipts"], [receipt_book.for_task(2)[0].receipt_id])

        # 到点取文件没取到，不应该留下交付记录，也不应该被算作消费。
        board = self.subscription.delivery_board(2, PERIOD)
        self.assertEqual(board["delivery_count"], 0)
        self.assertEqual(board["consumption_total"], 0)
        self.assertEqual(board["sign_off_total"], 0)
        self.assertEqual(board["receipts"][0]["state"], STATE_WRITTEN)

    def test_after_sign_off_the_same_file_is_delivered_once(self):
        self.export.start_export(
            2, auto_sign_off=False, report_period=PERIOD,
            report_object="营收分析看板",
        )
        self.export.run_action(2, "完成导出")
        first = self.subscription.deliver(2, period=PERIOD)
        self.assertTrue(first["consumed"])
        self.assertEqual(first["sign_off_count"], 1)
        self.assertTrue(Path(first["artifact_path"]).exists())

        second = self.subscription.deliver(2, period=PERIOD)
        self.assertFalse(second["consumed"])
        self.assertEqual(second["reason"], "already_delivered")
        self.assertEqual(second["read_files"], [])
        self.assertEqual(second["previously_delivered"], [first["artifact_path"]])

        board = self.subscription.delivery_board(2, PERIOD)
        self.assertEqual(board["delivery_count"], 1)
        self.assertEqual(board["consumption_total"], 1)
        self.assertEqual(board["sign_off_total"], 1)

    def test_subscription_rejects_artifact_from_other_period(self):
        self.export.start_export(
            2, auto_sign_off=False, report_period="2026-10",
            report_object="营收分析看板",
        )
        self.export.run_action(2, "完成导出")
        result = self.subscription.deliver(2, period=PERIOD)
        self.assertFalse(result["consumed"])
        self.assertEqual(result["reason"], "no_artifact")

    def test_month_close_is_idempotent_and_reports_read_files(self):
        self.export.start_export(1, report_period=PERIOD)          # 核心指标监控大盘
        self.export.start_export(
            2, auto_sign_off=False, report_period=PERIOD,
            report_object="营收分析看板",
        )
        self.export.run_action(2, "完成导出")

        first = self.subscription.month_close(PERIOD)
        self.assertEqual(first["triggered"], 2)
        self.assertEqual(first["delivered"], 2)
        self.assertEqual(first["skipped"], 0)
        delivered_files = {
            item["artifact_path"] for item in first["results"] if item["artifact_path"]
        }
        self.assertEqual(len(delivered_files), 2)

        second = self.subscription.month_close(PERIOD)
        self.assertEqual(second["delivered"], 0)
        self.assertEqual(second["skipped"], 2)
        for item in second["results"]:
            self.assertEqual(item["read_files"], [])   # 第二次一个文件都没读

        for subscription_id in (1, 2):
            board = self.subscription.delivery_board(subscription_id, PERIOD)
            self.assertEqual(board["consumption_total"], 1)   # 每份只消费一次
            self.assertEqual(board["sign_off_total"], 1)

    # ---- 场景 3：交付状态切换时核对 ----

    def test_toggling_subscription_status_reconciles_existing_deliveries(self):
        self.export.start_export(1, report_period=PERIOD)
        self.subscription.deliver(1, period=PERIOD)
        receipt = receipt_book.for_task(1, period=PERIOD)[0]

        # 产物被作废后，订阅状态切换必须发现原交付已失效。
        self.export.invalidate(1, receipt.receipt_id, "产物被替换")
        self.subscription.run_action(1, "暂停订阅")
        board = self.subscription.delivery_board(1, PERIOD)
        self.assertEqual(board["deliveries"][0]["status"], "已失效")
        self.assertIn("需补跑", board["deliveries"][0]["invalid_reason"])
        self.assertEqual(board["consumption_total"], 1)  # 历史读取事实保留，但结论作废

        # 暂停期间不参与交付；重新启用后可以再次交付新签收的产物。
        self.subscription.run_action(1, "启用订阅")
        result = self.subscription.deliver(1, period=PERIOD)
        self.assertFalse(result["consumed"])
        self.assertEqual(result["reason"], "waiting_sign_off")

    # ---- 场景 4：中断恢复 ----

    def test_recover_signs_off_written_artifact_and_leaves_explainable_state(self):
        self.export.start_export(
            3, auto_sign_off=False, report_period=PERIOD,
            report_object="用户留存分析看板",
        )
        receipt = receipt_book.for_task(3, period=PERIOD)[0]
        self.assertEqual(receipt.state, STATE_WRITTEN)

        result = self.export.recover()
        self.assertTrue(result["ok"])
        receipt = receipt_book.for_task(3, period=PERIOD)[0]
        self.assertEqual(receipt.state, STATE_SIGNED)
        self.assertEqual(receipt.sign_off_count, 1)
        task = store.find("adhoc_export", 3)
        self.assertEqual(task["status"], "已完成")
        self.assertFalse(task["pending"])

        kinds = {item["state"] for item in result["dispositions"]}
        self.assertIn("补签收", kinds)
        queue = self.export.queue_status()
        self.assertEqual(queue["pending_count"], 0)

    def test_recover_marks_missing_artifact_for_rerun_instead_of_rerunning_completed(self):
        self.export.start_export(
            2, auto_sign_off=False, report_period=PERIOD,
            report_object="营收分析看板",
        )
        receipt = receipt_book.for_task(2, period=PERIOD)[0]
        self.export.run_action(2, "完成导出")
        Path(receipt.artifact_path).unlink()  # 模拟产物被误删

        result = self.export.recover()
        receipt = receipt_book.for_task(2, period=PERIOD)[0]
        self.assertEqual(receipt.state, STATE_RERUN)
        self.assertTrue(receipt.needs_rerun)
        self.assertEqual(result["needs_rerun"], 1)
        task = store.find("adhoc_export", 2)
        self.assertEqual(task["status"], "导出失败")
        self.assertTrue(task["pending"])  # 需要补跑，进入待处理

        # 已完成且签收有效的任务不会被重新排队。
        self.export.start_export(1, report_period=PERIOD)
        before = self.export.queue_status()["pending_count"]
        self.export.recover()
        self.assertEqual(self.export.queue_status()["pending_count"], before)

    def test_recover_does_not_double_count_rerun_items(self):
        self.export.start_export(2, auto_sign_off=False, report_period=PERIOD,
                                 report_object="营收分析看板")
        receipt = receipt_book.for_task(2, period=PERIOD)[0]
        Path(receipt.artifact_path).unlink()

        first = self.export.recover()
        second = self.export.recover()
        self.assertEqual(first["needs_rerun"], 1)
        self.assertEqual(second["needs_rerun"], 1)
        self.assertEqual(
            second["dispositions"][0]["state"], "等待补跑",
        )

    def test_completed_task_cannot_stay_in_queue(self):
        self.export.start_export(1, report_period=PERIOD)
        # 人为制造残留：已完成任务又被塞进队列，恢复必须把它清掉。
        self.export._scheduler.enqueue("1", priority=5)
        sweep = self.export.recover()
        self.assertEqual(self.export.queue_status()["queue_size"], 0)
        self.assertEqual(self.export.queue_status()["pending_count"], 0)
        self.assertTrue(any(d["action"] == "已清理" for d in sweep["dispositions"]))

    def test_expired_task_is_not_revived_by_recover(self):
        self.export.start_export(1, report_period=PERIOD)
        self.export.run_action(1, "清理过期")
        self.export.recover()
        task = store.find("adhoc_export", 1)
        self.assertEqual(task["status"], "已过期")
        self.assertFalse(task["pending"])
        self.assertEqual(self.export.queue_status()["pending_count"], 0)

    # ---- 场景 5：详情页产物与待处理项 ----

    def test_detail_reports_artifacts_and_pending_items(self):
        self.export.start_export(2, auto_sign_off=False, report_period=PERIOD)
        detail = self.export.detail(2)
        self.assertTrue(detail["ok"])
        self.assertEqual(len(detail["artifacts"]), 1)
        artifact = detail["artifacts"][0]
        self.assertTrue(artifact["artifact_intact"])
        self.assertEqual(artifact["sign_off_count"], 0)
        self.assertEqual(detail["pending_count"], 1)

        self.export.run_action(2, "完成导出")
        detail = self.export.detail(2)
        self.assertEqual(detail["pending_count"], 0)
        self.assertEqual(detail["artifacts"][0]["sign_off_count"], 1)

    def test_receipt_checksum_survives_inspection(self):
        self.export.start_export(2, auto_sign_off=False, report_period=PERIOD)
        receipt = receipt_book.for_task(2, period=PERIOD)[0]
        ok, reason = artifact_is_intact(receipt)
        self.assertTrue(ok, reason)
        Path(receipt.artifact_path).write_text("tampered", encoding="utf-8")
        ok, reason = artifact_is_intact(receipt)
        self.assertFalse(ok)
        self.assertIn("不一致", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
