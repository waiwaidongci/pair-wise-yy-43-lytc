import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import (MAX_GROUNDING_RESISTANCE, MAX_WIND_SPEED, MEASURE_DIFF_LIMIT,
                       STATES, TRANSITION_ROLES, precheck_failures,
                       measure_diff_ratio, measure_over_limit)


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self._contained_item()
        self.iid = self.item["id"]
        self.ship = self.service.create_ship(
            self.iid, {"name": "沉船A"}, "ops", "operations")
        self.tank = self.service.create_tank(
            self.iid, self.ship["id"],
            {"name": "3号货油舱", "capacity": 500}, "ops", "operations")
        self.shore = self.service.create_shore_tank(
            self.iid, {"name": "岸罐1", "capacity": 1000}, "ops", "operations")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _contained_item(self):
        item = self.service.create_item(
            {"title": "沉船抽油", "description": "舱级转驳", "severity": "major",
             "quantity": 200, "threshold": 50, "external_ref": "TRF-1"},
            "creator", "observer")
        for target in ("assessing", "containing"):
            item = self.service.transition(
                item["id"], target, item["version"], "cmd",
                TRANSITION_ROLES[target][0])
        return item

    def _create_transfer(self, qty=100, ref=None):
        payload = {"tank_id": self.tank["id"], "shore_tank_id": self.shore["id"],
                   "planned_qty": qty}
        if ref:
            payload["ref"] = ref
        return self.service.create_transfer(self.iid, payload, "ops", "operations")

    def _open_transfer(self, tr, grounding=10.0, wind=5.0):
        return self.service.precheck_transfer(
            self.iid, tr["id"],
            {"grounding_resistance": grounding, "wind_speed": wind},
            "ops", "operations")

    def _measure(self, tr, ship_qty, shore_qty, actor="ops",
                 ship_ticket="S-1", shore_ticket="R-1", note=None):
        payload = {"ship_qty": ship_qty, "shore_qty": shore_qty,
                   "ship_ticket": ship_ticket, "shore_ticket": shore_ticket}
        if note is not None:
            payload["reconciliation_note"] = note
        return self.service.measure_transfer(
            self.iid, tr["id"], payload, actor, "operations")

    def test_rule_helpers(self):
        self.assertEqual(
            precheck_failures(90, 100, MAX_GROUNDING_RESISTANCE, MAX_WIND_SPEED),
            ["remaining_capacity"])
        self.assertIn("grounding_resistance",
                      precheck_failures(100, 100, MAX_GROUNDING_RESISTANCE + 1, 0))
        self.assertIn("wind_speed",
                      precheck_failures(100, 100, 0, MAX_WIND_SPEED + 0.1))
        self.assertEqual(precheck_failures(100, 100, 100, 10.8), [])
        self.assertEqual(measure_diff_ratio(100, 102), 2 / 102)
        self.assertFalse(measure_over_limit(100, 102))
        self.assertTrue(measure_over_limit(100, 103))
        self.assertEqual(MEASURE_DIFF_LIMIT, 0.02)

    def test_full_transfer_flow_unlocks_recovery(self):
        tr = self._create_transfer(100, "TR-1")
        self.assertEqual(tr["status"], "planned")
        self.assertTrue(tr["open"])
        # 开工前核对通过
        tr = self._open_transfer(tr)
        self.assertEqual(tr["status"], "in_progress")
        self.assertEqual(tr["failed_items"], [])
        # 计量差1%，在2%内
        tr = self._measure(tr, 100, 99)
        self.assertEqual(tr["status"], "in_progress")
        # 另一人确认
        tr = self.service.confirm_transfer(
            self.iid, tr["id"], {}, "cmd", "response_commander")
        self.assertEqual(tr["status"], "confirmed")
        self.assertFalse(tr["open"])
        self.assertIsNone(tr["gap"])
        # 进度带出船/舱/岸罐数量
        progress = self.service.transfer_progress(self.iid)
        self.assertEqual(progress["total"], 1)
        self.assertEqual(progress["confirmed"], 1)
        self.assertEqual(progress["open"], 0)
        self.assertEqual(progress["pumped_qty"], 100)
        self.assertEqual(progress["received_qty"], 99)
        self.assertEqual(progress["by_ship"], {"沉船A": 100})
        self.assertEqual(progress["by_shore_tank"], {"岸罐1": 99})
        self.assertEqual(progress["gaps"], [])
        # 可以转入回收
        current = self.service.get_item(self.iid, "viewer")
        current = self.service.transition(
            self.iid, "recovering", current["version"], "ops",
            TRANSITION_ROLES["recovering"][0])
        self.assertEqual(current["status"], "recovering")

    def test_precheck_failures_hold_and_name_item(self):
        tr = self._create_transfer(100)
        # 岸罐容量不足（先占950）
        other_ship = self.service.create_ship(
            self.iid, {"name": "沉船B"}, "ops", "operations")
        other_tank = self.service.create_tank(
            self.iid, other_ship["id"], {"name": "舱B", "capacity": 500},
            "ops", "operations")
        self.service.create_transfer(
            self.iid, {"tank_id": other_tank["id"],
                       "shore_tank_id": self.shore["id"], "planned_qty": 950},
            "ops", "operations")
        held = self._open_transfer(tr)
        self.assertEqual(held["status"], "held")
        self.assertIn("remaining_capacity", held["failed_items"])
        self.assertIn("剩余舱容", held["precheck_message"])
        # 接地电阻、风速不符（使用独立岸罐避免剩余舱容干扰）
        shore2 = self.service.create_shore_tank(
            self.iid, {"name": "岸罐2", "capacity": 1000}, "ops", "operations")
        tr2_tank = self.service.create_tank(
            self.iid, self.ship["id"], {"name": "舱C", "capacity": 500},
            "ops", "operations")
        tr2 = self.service.create_transfer(
            self.iid, {"tank_id": tr2_tank["id"],
                       "shore_tank_id": shore2["id"], "planned_qty": 10},
            "ops", "operations")
        held2 = self._open_transfer(tr2, grounding=120, wind=12)
        self.assertEqual(set(held2["failed_items"]),
                         {"grounding_resistance", "wind_speed"})
        self.assertIn("接地电阻", held2["precheck_message"])
        self.assertIn("风速", held2["precheck_message"])
        # 停待作业是回收缺口
        gaps = self.service.transfer_progress(self.iid)["gaps"]
        codes = {g["code"] for g in gaps}
        self.assertIn("held", codes)

    def test_one_open_transfer_per_tank(self):
        self._create_transfer(10)
        with self.assertRaises(ConflictError):
            self._create_transfer(20)
        # 确认后作业已结，同舱可再开新作业
        tr = self.service.list_transfers(self.iid, "viewer")[0]
        self._open_transfer(tr)
        self._measure(tr, 10, 10)
        self.service.confirm_transfer(
            self.iid, tr["id"], {}, "cmd", "response_commander")
        second = self._create_transfer(5)
        self.assertEqual(second["status"], "planned")
        # 第二作业未结（planned在途）时修订舱容：会与在途作业冲突，整体回滚
        with self.assertRaises(ConflictError):
            self.service.revise_tank_plan(
                self.iid, self.tank["id"], {"capacity": 480}, "cmd",
                "response_commander")
        # 舱容版本未变
        self.assertEqual(self.repo.get_tank(self.tank["id"])["plan_version"], 1)

    def test_discrepancy_over_2_percent_pending_and_confirm_rules(self):
        tr = self._create_transfer(100)
        self._open_transfer(tr)
        measured = self._measure(tr, 100, 95)  # 差5%
        self.assertEqual(measured["status"], "reconciling")
        self.assertGreater(measured["diff_ratio"], 0.02)
        # 未补齐核对说明不能确认
        with self.assertRaises(ConflictError) as ctx:
            self.service.confirm_transfer(
                self.iid, tr["id"], {}, "cmd", "response_commander")
        self.assertTrue(any("2%" in g["message"] for g in ctx.exception.gaps))
        # 同一人不能确认
        with self.assertRaises(ConflictError):
            self.service.confirm_transfer(
                self.iid, tr["id"],
                {"reconciliation_note": "已复测，修正岸罐计量"},
                "ops", "response_commander")
        # 补齐计量单并由另一人确认
        done = self.service.confirm_transfer(
            self.iid, tr["id"],
            {"reconciliation_note": "已复测，修正岸罐计量"},
            "cmd", "response_commander")
        self.assertEqual(done["status"], "confirmed")

    def test_missing_tickets_block_confirm(self):
        tr = self._create_transfer(50)
        self._open_transfer(tr)
        measured = self._measure(tr, 50, 50)
        # 缺计量单场景：手工清空票据后确认被拒
        self.repo.update_transfer(
            tr["id"], {"ship_ticket": None, "shore_ticket": None})
        with self.assertRaises(ConflictError) as ctx:
            self.service.confirm_transfer(
                self.iid, tr["id"], {}, "cmd", "response_commander")
        messages = [g["message"] for g in ctx.exception.gaps]
        self.assertTrue(any("计量单" in m for m in messages))
        del measured

    def test_plan_revision_voids_confirmation_and_blocks_recovery(self):
        tr = self._create_transfer(100)
        self._open_transfer(tr)
        self._measure(tr, 100, 100)
        self.service.confirm_transfer(
            self.iid, tr["id"], {}, "cmd", "response_commander")
        # 修订舱容计划，原确认作废
        revised = self.service.revise_tank_plan(
            self.iid, self.tank["id"], {"capacity": 480}, "cmd",
            "response_commander")
        self.assertEqual(revised["plan_version"], 2)
        self.assertEqual(len(revised["voided_transfers"]), 1)
        detail = self.service.get_transfer(self.iid, tr["id"], "viewer")
        self.assertTrue(detail["confirmation_voided"])
        self.assertTrue(detail["open"])
        self.assertEqual(detail["gap"]["code"], "confirmation_voided")
        # 不能转入回收：409缺口列出作废确认
        current = self.service.get_item(self.iid, "viewer")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.iid, "recovering", current["version"], "ops",
                TRANSITION_ROLES["recovering"][0])
        self.assertEqual(len(ctx.exception.gaps), 1)
        self.assertEqual(ctx.exception.gaps[0]["code"], "confirmation_voided")
        # 重新计量+另一人确认后解锁
        detail = self.service.measure_transfer(
            self.iid, tr["id"],
            {"ship_qty": 98, "shore_qty": 98, "ship_ticket": "S-2",
             "shore_ticket": "R-2", "reconciliation_note": "按新舱容复测"},
            "ops", "operations")
        self.assertEqual(detail["status"], "in_progress")
        # 作废未重新确认期间，同舱不能开新作业
        with self.assertRaises(ConflictError):
            self._create_transfer(5)
        self.service.confirm_transfer(
            self.iid, tr["id"], {}, "cmd", "response_commander")
        current = self.service.get_item(self.iid, "viewer")
        current = self.service.transition(
            self.iid, "recovering", current["version"], "ops",
            TRANSITION_ROLES["recovering"][0])
        self.assertEqual(current["status"], "recovering")

    def test_roles_and_status_gate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_transfer(
                self.iid, {"tank_id": self.tank["id"],
                           "shore_tank_id": self.shore["id"], "planned_qty": 1},
                "viewer", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.precheck_transfer(
                self.iid, 1, {"grounding_resistance": 1, "wind_speed": 1},
                "cmd", "response_commander")
        with self.assertRaises(ValidationError):
            self.service.create_transfer(
                self.iid, {"tank_id": self.tank["id"],
                           "shore_tank_id": self.shore["id"], "planned_qty": -1},
                "ops", "operations")

    def test_audit_carries_progress_and_chain_intact(self):
        tr = self._create_transfer(100)
        self._open_transfer(tr)
        self._measure(tr, 100, 97)  # 超差，审计带状态
        events = self.service.audit("viewer", self.iid)
        measure_events = [e for e in events if e["action"] == "transfer_measure"]
        self.assertTrue(measure_events)
        progress = measure_events[-1]["detail"]["progress"]
        self.assertEqual(progress["status"], "reconciling")
        self.assertIn("待核对", progress["gap"])
        # 尝试转入回收的失败不产生transition事件
        current = self.service.get_item(self.iid, "viewer")
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.iid, "recovering", current["version"], "ops",
                TRANSITION_ROLES["recovering"][0])
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
