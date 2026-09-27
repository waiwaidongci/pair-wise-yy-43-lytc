import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "t.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "沉船抽油", "description": "舱级转驳", "severity": "major",
             "quantity": 50, "threshold": 10, "external_ref": "WR-1"},
            "creator", "observer")
        self.tank = self.service.create_tank(
            self.item["id"], {"name": "No.3舱", "capacity": 100},
            "planner", "response_commander")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _to_containing(self):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in ("assessing", "containing"):
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def _open_transfer(self, quantity=60, ground=5.0, wind=3.0, ship="救助船1号"):
        transfer = self.service.create_transfer(
            self.item["id"],
            {"tank_id": self.tank["id"], "ship_name": ship, "quantity": quantity,
             "ground_resistance": ground, "wind_speed": wind},
            "operator", "operations")
        assert transfer["status"] == "open", transfer
        return transfer

    def test_prestart_failures_pending_and_busy_tank(self):
        transfer = self.service.create_transfer(
            self.item["id"],
            {"tank_id": self.tank["id"], "ship_name": "救助船1号", "quantity": 150,
             "ground_resistance": 20, "wind_speed": 25},
            "operator", "operations")
        self.assertEqual(transfer["status"], "pending")
        self.assertEqual(transfer["failed_checks"], ["剩余舱容", "接地电阻", "风速"])
        with self.assertRaises(ConflictError) as ctx:
            self._open_transfer(quantity=10)
        self.assertEqual(len(ctx.exception.details["gaps"]), 1)
        still = self.service.recheck_transfer(
            self.item["id"], transfer["id"],
            {"quantity": 80, "ground_resistance": 20, "wind_speed": 3},
            "operator", "operations")
        self.assertEqual(still["status"], "pending")
        self.assertEqual(still["failed_checks"], ["接地电阻"])
        opened = self.service.recheck_transfer(
            self.item["id"], transfer["id"],
            {"quantity": 80, "ground_resistance": 5, "wind_speed": 3},
            "operator", "operations")
        self.assertEqual(opened["status"], "open")
        self.assertEqual(opened["failed_checks"], [])

    def test_discrepancy_review_and_four_eyes_confirm(self):
        transfer = self._open_transfer(quantity=80)
        reviewed = self.service.record_quantities(
            self.item["id"], transfer["id"],
            {"ship_quantity": 80, "shore_quantity": 77}, "operator", "operations")
        self.assertEqual(reviewed["status"], "review")
        view = self.service.list_transfers(self.item["id"], "viewer")[0]
        self.assertAlmostEqual(view["difference_pct"], 3.75)
        with self.assertRaises(ConflictError):
            self.service.record_quantities(
                self.item["id"], transfer["id"],
                {"ship_quantity": 80, "shore_quantity": 77}, "operator", "operations")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(
                self.item["id"], transfer["id"],
                {"measurement_ticket": "M-1"}, "operator", "operations")
        with self.assertRaises(ValidationError):
            self.service.confirm_transfer(
                self.item["id"], transfer["id"], {}, "verifier", "response_commander")
        confirmed = self.service.confirm_transfer(
            self.item["id"], transfer["id"],
            {"measurement_ticket": "M-1"}, "verifier", "response_commander")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["measurement_ticket"], "M-1")
        self.assertEqual(confirmed["confirmed_by"], "verifier")

    def test_within_tolerance_auto_confirmed(self):
        transfer = self._open_transfer(quantity=80)
        done = self.service.record_quantities(
            self.item["id"], transfer["id"],
            {"ship_quantity": 80, "shore_quantity": 79}, "operator", "operations")
        self.assertEqual(done["status"], "confirmed")
        tank = self.service.list_tanks(self.item["id"], "viewer")[0]
        self.assertAlmostEqual(tank["extracted"], 80.0)
        self.assertAlmostEqual(tank["remaining"], 20.0)

    def test_recovery_gate_and_plan_revision_voids_confirmation(self):
        transfer = self._open_transfer(quantity=60)
        self.service.record_quantities(
            self.item["id"], transfer["id"],
            {"ship_quantity": 60, "shore_quantity": 58}, "operator", "operations")
        current = self._to_containing()
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], "recovering", current["version"],
                                    "reviewer", TRANSITION_ROLES["recovering"][0])
        gaps = ctx.exception.details["gaps"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["transfer_id"], transfer["id"])
        self.assertEqual(gaps[0]["status"], "review")
        self.service.confirm_transfer(
            self.item["id"], transfer["id"],
            {"measurement_ticket": "M-9"}, "verifier", "response_commander")
        current = self.service.get_item(self.item["id"], "viewer")
        current = self.service.transition(current["id"], "recovering",
                                          current["version"], "reviewer",
                                          TRANSITION_ROLES["recovering"][0])
        self.assertEqual(current["status"], "recovering")

    def test_plan_revision_voids_and_blocks_again(self):
        transfer = self._open_transfer(quantity=50)
        self.service.record_quantities(
            self.item["id"], transfer["id"],
            {"ship_quantity": 50, "shore_quantity": 50}, "operator", "operations")
        current = self._to_containing()
        tank = self.service.revise_plan(
            self.item["id"], self.tank["id"], {"capacity": 120},
            "planner", "response_commander")
        self.assertEqual(tank["plan_version"], 2)
        voided = self.service.list_transfers(self.item["id"], "viewer")[0]
        self.assertEqual(voided["status"], "void")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], "recovering", current["version"],
                                    "reviewer", TRANSITION_ROLES["recovering"][0])
        self.assertEqual(ctx.exception.details["gaps"][0]["status"], "void")
        reconfirmed = self.service.confirm_transfer(
            self.item["id"], transfer["id"],
            {"measurement_ticket": "M-10"}, "verifier", "response_commander")
        self.assertEqual(reconfirmed["status"], "confirmed")
        self.assertEqual(reconfirmed["plan_version"], 2)
        current = self.service.get_item(self.item["id"], "viewer")
        current = self.service.transition(current["id"], "recovering",
                                          current["version"], "reviewer",
                                          TRANSITION_ROLES["recovering"][0])
        self.assertEqual(current["status"], "recovering")

    def test_detail_and_audit_carry_progress(self):
        transfer = self._open_transfer(quantity=40)
        self.service.record_quantities(
            self.item["id"], transfer["id"],
            {"ship_quantity": 40, "shore_quantity": 39.5}, "operator", "operations")
        detail = self.service.get_item(self.item["id"], "viewer")
        progress = detail["lightering"]
        self.assertEqual(progress["ship_total"], 40.0)
        self.assertEqual(progress["shore_total"], 39.5)
        self.assertEqual(progress["by_ship"]["救助船1号"]["ship_quantity"], 40.0)
        self.assertEqual(progress["by_status"]["confirmed"], 1)
        self.assertEqual(progress["tanks"][0]["name"], "No.3舱")
        events = self.service.audit("viewer", self.item["id"])
        actions = [event["action"] for event in events]
        self.assertIn("tank_create", actions)
        self.assertIn("transfer_create", actions)
        self.assertIn("transfer_quantities", actions)
        quant = next(e for e in events if e["action"] == "transfer_quantities")
        self.assertEqual(quant["detail"]["progress"]["ship_total"], 40.0)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_role_guards(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_transfer(
                self.item["id"],
                {"tank_id": self.tank["id"], "ship_name": "救助船1号", "quantity": 10,
                 "ground_resistance": 5, "wind_speed": 3}, "guest", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.revise_plan(self.item["id"], self.tank["id"],
                                     {"capacity": 200}, "operator", "operations")


if __name__ == "__main__":
    unittest.main()
