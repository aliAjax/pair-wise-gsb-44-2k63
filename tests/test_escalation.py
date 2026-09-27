import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, PrivacyRequestService  # noqa: E402


class EscalationBoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = PrivacyRequestService(Path(self.tmp.name) / "test.db")
        self.service.configure_jurisdiction(
            "sup1", "supervisor", "CN", "中国", 30, 30, True, True, escalation_threshold_days=2)
        self.service.configure_jurisdiction("sup1", "supervisor", "EU", "欧盟", 30, 15)
        self.subject = self.service.create_subject(
            "intake1", "intake", "SUB-001", "CN", False, "person@example.test")

    def tearDown(self):
        self.tmp.cleanup()

    def make_request(self, number="PR-ESC", region_subject=None):
        subject = region_subject or self.subject
        return self.service.create_request(
            "intake1", "intake", number, subject["id"], "access", "IDEM-" + number)["request"]

    def make_overdue(self, request_id, days):
        due = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.service.connect() as conn:
            conn.execute("UPDATE requests SET due_date=? WHERE id=?", (due, request_id))

    def assigned_request(self, number="PR-ESC", overdue_days=5):
        request = self.make_request(number)
        request = self.service.verify_identity(
            "officer1", "privacy_officer", request["id"], request["version"], "ID-001")
        request = self.service.assign_request(
            "sup1", "supervisor", request["id"], "officer1", request["version"])
        self.make_overdue(request["id"], overdue_days)
        return request

    def board(self, actor="sup1", role="supervisor"):
        return self.service.escalation_board(actor, role)

    def test_threshold_gates_entry_to_board(self):
        request = self.make_request()
        self.make_overdue(request["id"], 1)  # 逾期1天，未达到阈值2天
        self.assertEqual([], self.board()["escalations"])
        self.make_overdue(request["id"], 3)  # 达到阈值
        escalations = self.board()["escalations"]
        self.assertEqual(1, len(escalations))
        self.assertEqual("open", escalations[0]["status"])
        self.assertEqual(request["id"], escalations[0]["request_id"])
        self.assertGreaterEqual(escalations[0]["overdue_days"], 3)

    def test_finished_request_not_escalated_and_no_duplicate_open(self):
        request = self.assigned_request()
        first = self.board()["escalations"]
        second = self.board()["escalations"]
        self.assertEqual(1, len(first))
        self.assertEqual(first[0]["id"], second[0]["id"])  # 未关闭前不重复开单
        with self.service.connect() as conn:
            conn.execute("UPDATE requests SET status='fulfilled' WHERE id=?", (request["id"],))
        escalations = self.board()["escalations"]
        self.assertEqual(1, len(escalations))  # 已开立的升级单仍需复核关闭
        closed = self.service.submit_escalation(
            "officer1", "privacy_officer", escalations[0]["id"], "已加急完成回复", "officer1", "2030-01-01")
        self.service.close_escalation("sup1", "supervisor", closed["id"], "确认已办结")
        self.assertEqual([], self.board()["escalations"])  # 请求已办结，不再开新单

    def test_submit_requires_complete_fields_and_assignee(self):
        request = self.assigned_request()
        escalation = self.board()["escalations"][0]
        for note, person, plan in [("", "officer1", "2030-01-01"),
                                   ("说明", "", "2030-01-01"),
                                   ("说明", "officer1", "")]:
            with self.assertRaises(DomainError) as ctx:
                self.service.submit_escalation(
                    "officer1", "privacy_officer", escalation["id"], note, person, plan)
            self.assertEqual(400, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_escalation(
                "officer1", "privacy_officer", escalation["id"], "说明", "officer1", "不是日期")
        self.assertEqual(400, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_escalation(
                "officer2", "privacy_officer", escalation["id"], "说明", "officer2", "2030-01-01")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_escalation(
                "intake1", "intake", escalation["id"], "说明", "officer1", "2030-01-01")
        self.assertEqual(403, ctx.exception.status)
        submitted = self.service.submit_escalation(
            "officer1", "privacy_officer", escalation["id"],
            "已联系CRM团队加急导出", "officer1", "2030-01-10")
        self.assertEqual("submitted", submitted["status"])
        self.assertEqual("officer1", submitted["submitted_by"])
        self.assertEqual("2030-01-10T00:00:00+00:00", submitted["planned_completion_date"])

    def test_no_duplicate_submit_and_review_close_flow(self):
        request = self.assigned_request()
        escalation = self.board()["escalations"][0]
        with self.assertRaises(DomainError) as ctx:  # 未提交处置不能复核
            self.service.close_escalation("sup1", "supervisor", escalation["id"], "提前关闭")
        self.assertEqual(409, ctx.exception.status)
        self.service.submit_escalation(
            "officer1", "privacy_officer", escalation["id"], "加急处理中", "officer1", "2030-01-10")
        with self.assertRaises(DomainError) as ctx:  # 不能重复提交
            self.service.submit_escalation(
                "officer1", "privacy_officer", escalation["id"], "再次提交", "officer1", "2030-01-11")
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:  # 非主管不能复核
            self.service.close_escalation("officer1", "privacy_officer", escalation["id"], "自查")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx:  # 复核意见必填
            self.service.close_escalation("sup1", "supervisor", escalation["id"], "  ")
        self.assertEqual(400, ctx.exception.status)
        closed = self.service.close_escalation("sup1", "supervisor", escalation["id"], "处置方案可行，按计划跟进")
        self.assertEqual("closed", closed["status"])
        self.assertEqual("sup1", closed["reviewed_by"])
        self.assertEqual([], self.board()["escalations"])  # 关闭后移出队列
        with self.assertRaises(DomainError) as ctx:  # 已关闭不能重复复核
            self.service.close_escalation("sup1", "supervisor", escalation["id"], "再次关闭")
        self.assertEqual(409, ctx.exception.status)
        detail = self.service.get_request("sup1", "supervisor", request["id"])
        self.assertEqual(1, len(detail["escalations"]))
        actions = [entry["action"] for entry in detail["timeline"]]
        self.assertEqual(["escalation.opened", "escalation.submitted", "escalation.closed"],
                         [a for a in actions if a.startswith("escalation.")])

    def test_reescalation_after_planned_date_passes(self):
        request = self.assigned_request()
        escalation = self.board()["escalations"][0]
        self.service.submit_escalation(
            "officer1", "privacy_officer", escalation["id"], "加急处理中", "officer1", "2030-01-10")
        self.service.close_escalation("sup1", "supervisor", escalation["id"], "同意计划")
        self.assertEqual([], self.board()["escalations"])  # 计划完成日未到，不重复升级
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        with self.service.connect() as conn:
            conn.execute("UPDATE escalations SET planned_completion_date=? WHERE id=?",
                         (past, escalation["id"]))
        escalations = self.board()["escalations"]  # 超过计划完成日仍未办结，重新升级
        self.assertEqual(1, len(escalations))
        self.assertNotEqual(escalation["id"], escalations[0]["id"])
        self.assertEqual("open", escalations[0]["status"])

    def test_regional_summary_and_visibility(self):
        cn_request = self.assigned_request("PR-CN", overdue_days=6)
        eu_subject = self.service.create_subject(
            "intake1", "intake", "SUB-EU", "EU", False, "eu@example.test")
        eu_request = self.make_request("PR-EU", region_subject=eu_subject)
        self.make_overdue(eu_request["id"], 4)  # EU 阈值0天，逾期即升级
        board = self.board()
        self.assertEqual(2, len(board["escalations"]))
        summary = {row["jurisdiction"]: row for row in board["summary"]}
        self.assertEqual(1, summary["CN"]["open"])
        self.assertGreaterEqual(summary["CN"]["max_overdue_days"], 6)
        self.assertEqual(1, summary["EU"]["open"])
        self.assertEqual(0, summary["EU"]["closed"])
        officer_board = self.board("officer1", "privacy_officer")
        self.assertEqual([cn_request["id"]], [e["request_id"] for e in officer_board["escalations"]])
        self.assertEqual(["CN"], [s["jurisdiction"] for s in officer_board["summary"]])
        auditor_board = self.board("aud1", "auditor")
        self.assertEqual(2, len(auditor_board["escalations"]))
        self.assertEqual([], self.board("intake1", "intake")["escalations"])
        cn_esc = [e for e in board["escalations"] if e["jurisdiction"] == "CN"][0]
        self.service.submit_escalation(
            "officer1", "privacy_officer", cn_esc["id"], "加急处理", "officer1", "2030-02-01")
        summary = {row["jurisdiction"]: row for row in self.board()["summary"]}
        self.assertEqual(0, summary["CN"]["open"])
        self.assertEqual(1, summary["CN"]["submitted"])


if __name__ == "__main__":
    unittest.main()
