import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import OPEN_STATUSES, DomainError, PrivacyRequestService  # noqa: E402


class EscalationDeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = PrivacyRequestService(Path(self.tmp.name) / "test.db")
        # 响应时限 30 天，升级阈值 5 天（超期 5 天后才进入升级队列）
        self.service.configure_jurisdiction(
            "sup1", "supervisor", "CN", "中国", 30, 30, True, True, escalation_threshold_days=5
        )
        self.service.create_subject("intake1", "intake", "SUB-001", "CN", False, "person@example.test")
        self.req = self.service.create_request(
            "intake1", "intake", "PR-001", 1, "access", "IDEM-001"
        )["request"]

    def tearDown(self):
        self.tmp.cleanup()

    def backdate_due(self, request_id, overdue_days):
        """把到期日改为 overdue_days 天前，模拟超期。"""
        due = (datetime.now(timezone.utc) - timedelta(days=overdue_days)).isoformat(timespec="seconds")
        with self.service.connect() as conn:
            conn.execute("UPDATE requests SET due_date=? WHERE id=?", (due, request_id))

    def process_and_assign(self, request_id=None):
        rid = self.req["id"] if request_id is None else request_id
        r = self.service.verify_identity("officer1", "privacy_officer", rid, 1, "ID-1")
        r = self.service.assign_request("sup1", "supervisor", rid, "officer1", r["version"])
        return r

    # 1. 阈值内不升级，超过阈值自动入队
    def test_threshold_blocks_until_overdue_beyond_grace(self):
        self.backdate_due(self.req["id"], 3)
        self.assertEqual([], self.service.escalation_queue("sup1", "supervisor"))
        self.backdate_due(self.req["id"], 6)
        queue = self.service.escalation_queue("sup1", "supervisor")
        self.assertEqual(1, len(queue))
        self.assertEqual("open", queue[0]["status"])
        self.assertGreaterEqual(queue[0]["current_overdue_days"], 6)

    # 2. 同一请求未关闭前不能重复提交升级单
    def test_duplicate_escalation_blocked_while_open(self):
        self.backdate_due(self.req["id"], 10)
        self.service.escalation_queue("sup1", "supervisor")
        with self.assertRaises(DomainError) as ctx:
            self.service.open_escalation("sup1", "supervisor", self.req["id"])
        self.assertEqual(409, ctx.exception.status)
        # 再次扫描仍然只有一张单
        self.service.escalation_queue("sup1", "supervisor")
        rows = self.service.escalation_queue("sup1", "supervisor", include_closed=True)
        self.assertEqual(1, len(rows))

    # 3. 未超阈值不能手动开单；未超期的已结束请求不开单
    def test_manual_open_requires_overdue_and_open_request(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.open_escalation("sup1", "supervisor", self.req["id"])
        self.assertEqual(409, ctx.exception.status)
        self.backdate_due(self.req["id"], 2)
        with self.assertRaises(DomainError) as ctx2:
            self.service.open_escalation("sup1", "supervisor", self.req["id"])
        self.assertEqual(409, ctx2.exception.status)

    # 4. 处置说明内容不全不能提交，非被指派人无权提交
    def test_disposition_requires_all_fields_and_assignee(self):
        self.process_and_assign()
        self.backdate_due(self.req["id"], 8)
        esc = self.service.escalation_queue("officer1", "privacy_officer")[0]
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_escalation_disposition(
                "officer1", "privacy_officer", esc["id"], "", "DPO", "2026-10-10", esc["version"]
            )
        self.assertEqual(400, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.submit_escalation_disposition(
                "other", "privacy_officer", esc["id"], "已协调系统导出", "DPO", "2026-10-10", esc["version"]
            )
        self.assertEqual(403, ctx2.exception.status)
        with self.assertRaises(DomainError) as ctx3:
            self.service.submit_escalation_disposition(
                "officer1", "privacy_officer", esc["id"], "已协调系统导出", "DPO", "10/10/2026", esc["version"]
            )
        self.assertEqual(400, ctx3.exception.status)

    # 5. 完整生命周期：处置 -> 退回 -> 再处置 -> 关闭；关闭后移出活跃队列
    def test_full_lifecycle_submit_return_resubmit_close(self):
        self.process_and_assign()
        self.backdate_due(self.req["id"], 12)
        esc = self.service.escalation_queue("sup1", "supervisor")[0]

        updated = self.service.submit_escalation_disposition(
            "officer1", "privacy_officer", esc["id"], "跨部门核查中，已安排导出", "数据治理组", "2026-10-20", esc["version"]
        )
        self.assertEqual("awaiting_review", updated["status"])
        self.assertEqual(2, updated["version"])

        # 待复核状态不能再次提交处置
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_escalation_disposition(
                "officer1", "privacy_officer", esc["id"], "补充", "数据治理组", "2026-10-21", updated["version"]
            )
        self.assertEqual(409, ctx.exception.status)

        # 复核意见不能为空
        with self.assertRaises(DomainError) as ctx2:
            self.service.review_escalation("sup1", "supervisor", esc["id"], "close", "  ", updated["version"])
        self.assertEqual(400, ctx2.exception.status)
        # 只有主管能复核
        with self.assertRaises(DomainError) as ctx3:
            self.service.review_escalation("officer1", "privacy_officer", esc["id"], "close", "同意", updated["version"])
        self.assertEqual(403, ctx3.exception.status)

        returned = self.service.review_escalation(
            "sup1", "supervisor", esc["id"], "return", "计划完成日太晚，请给出更早日期与具体步骤", updated["version"]
        )
        self.assertEqual("returned", returned["status"])

        resubmitted = self.service.submit_escalation_disposition(
            "officer1", "privacy_officer", esc["id"], "已完成导出排期并每日跟进", "数据治理组-王", "2026-09-30", returned["version"]
        )
        self.assertEqual("awaiting_review", resubmitted["status"])

        closed = self.service.review_escalation(
            "sup1", "supervisor", esc["id"], "close", "处置计划合理，确认关闭并跟踪完成", resubmitted["version"]
        )
        self.assertEqual("closed", closed["status"])
        self.assertIsNotNone(closed["closed_at"])

        # 活跃队列不再包含，include_closed 可追溯
        self.assertEqual([], self.service.escalation_queue("sup1", "supervisor"))
        history = self.service.escalation_queue("sup1", "supervisor", include_closed=True)
        self.assertEqual("closed", history[0]["status"])

        # 关闭后自动扫描不会重开（避免关闭即被移回）；复发需手动提交新升级单
        self.assertEqual([], self.service.escalation_queue("sup1", "supervisor"))
        new_esc = self.service.open_escalation("sup1", "supervisor", self.req["id"])
        self.assertEqual(2, new_esc["id"])
        self.assertEqual("open", new_esc["status"])
        all_rows = self.service.escalation_queue("sup1", "supervisor", include_closed=True)
        self.assertEqual({"closed", "open"}, {r["status"] for r in all_rows})

    # 6. 未提交处置说明主管不能直接关闭（请求仍开放时）
    def test_close_requires_disposition_while_request_open(self):
        self.process_and_assign()
        self.backdate_due(self.req["id"], 7)
        esc = self.service.escalation_queue("sup1", "supervisor")[0]
        with self.assertRaises(DomainError) as ctx:
            self.service.review_escalation("sup1", "supervisor", esc["id"], "close", "直接关闭", esc["version"])
        self.assertEqual(409, ctx.exception.status)

    # 7. 地区汇总数据
    def test_region_summary_counts_and_overdue_days(self):
        self.process_and_assign()
        self.backdate_due(self.req["id"], 9)
        summary = self.service.escalation_summary("sup1", "supervisor")
        cn = next(r for r in summary["regions"] if r["jurisdiction"] == "CN")
        self.assertEqual(5, cn["escalation_threshold_days"])
        self.assertEqual(1, cn["active_count"])
        self.assertEqual(1, cn["open_count"])
        self.assertEqual(1, cn["overdue_open_request_count"])
        self.assertGreaterEqual(cn["max_overdue_days"], 9)
        self.assertEqual(1, summary["total_active"])

    # 8. 处理人只看到自己的升级单，受理角色看不到
    def test_visibility_by_role(self):
        self.process_and_assign()
        self.backdate_due(self.req["id"], 7)
        self.service.escalation_queue("sup1", "supervisor")
        own = self.service.escalation_queue("officer1", "privacy_officer")
        self.assertEqual(1, len(own))
        other = self.service.escalation_queue("officer2", "privacy_officer")
        self.assertEqual([], other)
        self.assertEqual([], self.service.escalation_queue("intake1", "intake"))

    # 9. 升级记录、复核意见与时间线可追溯
    def test_events_and_timeline_traceable(self):
        self.process_and_assign()
        self.backdate_due(self.req["id"], 11)
        esc = self.service.escalation_queue("sup1", "supervisor")[0]
        updated = self.service.submit_escalation_disposition(
            "officer1", "privacy_officer", esc["id"], "已制定补救计划", "DPO", "2026-10-01", esc["version"]
        )
        self.service.review_escalation("sup1", "supervisor", esc["id"], "close", "同意计划", updated["version"])
        detail = self.service.get_escalation("sup1", "supervisor", esc["id"])
        actions = [e["action"] for e in detail["events"]]
        self.assertEqual(["escalation.opened", "disposition.submitted", "escalation.closed"], actions)
        self.assertEqual("同意计划", detail["review_note"])
        self.assertEqual("已制定补救计划", detail["disposition_note"])
        audit_actions = [t["action"] for t in detail["request_timeline"]]
        self.assertIn("escalation.opened", audit_actions)
        self.assertIn("escalation.disposition_submitted", audit_actions)
        self.assertIn("escalation.closed", audit_actions)

    # 10. 乐观锁：旧版本不能提交/复核
    def test_version_conflict(self):
        self.process_and_assign()
        self.backdate_due(self.req["id"], 7)
        esc = self.service.escalation_queue("sup1", "supervisor")[0]
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_escalation_disposition(
                "officer1", "privacy_officer", esc["id"], "说明", "DPO", "2026-10-01", esc["version"] + 1
            )
        self.assertEqual(409, ctx.exception.status)

    # 11. 阈值配置校验
    def test_threshold_configuration_validation(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.configure_jurisdiction(
                "sup1", "supervisor", "EU", "欧盟", 30, 30, True, True, escalation_threshold_days=181
            )
        self.assertEqual(400, ctx.exception.status)
        updated = self.service.configure_jurisdiction(
            "sup1", "supervisor", "CN", "中国", 30, 30, True, True, escalation_threshold_days=0
        )
        self.assertEqual(0, updated["escalation_threshold_days"])

    # 12. 已结束请求不自动进入升级队列
    def test_finished_requests_never_auto_escalate(self):
        r = self.process_and_assign()
        loc = self.service.add_data_location("officer1", "privacy_officer", r["id"], "CRM", "profile", "customer")
        self.service.classify_location("officer1", "privacy_officer", loc["id"], False, False, False)
        r = self.service.prepare_response("officer1", "privacy_officer", r["id"], r["version"])
        r = self.service.fulfill_request("officer1", "privacy_officer", r["id"], "已提供副本", r["version"])
        self.backdate_due(r["id"], 20)
        self.assertEqual([], self.service.escalation_queue("sup1", "supervisor"))
        self.assertEqual("fulfilled", r["status"])
        self.assertNotIn("fulfilled", OPEN_STATUSES)


if __name__ == "__main__":
    unittest.main()
