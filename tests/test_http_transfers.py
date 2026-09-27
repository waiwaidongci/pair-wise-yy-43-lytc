import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.http_api import make_handler
from src.repository import Repository
from src.service import Service


def _request(url, method="GET", body=None, actor="ops", role="operations"):
    headers = {"X-Actor": actor, "X-Role": role}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class TransferHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Repository(str(Path(cls.tmp.name) / "http.db"))
        cls.service = Service(cls.repo)
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                        make_handler(cls.service, static_dir))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.repo.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.base = f"http://127.0.0.1:{self.port}"
        _, item = _request(f"{self.base}/api/items", "POST", {
            "title": "http转驳", "description": "409缺口", "severity": "major",
            "quantity": 100, "threshold": 10, "external_ref": f"HTTP-{id(self)}"},
            actor="obs", role="observer")
        self.iid = item["id"]
        for target, role in (("assessing", "response_commander"),
                             ("containing", "response_commander")):
            _request(f"{self.base}/api/items/{self.iid}/transition", "POST",
                     {"target": target, "expected_version": self._version()},
                     actor="cmd", role=role)

    def _version(self):
        _, item = _request(f"{self.base}/api/items/{self.iid}")
        return item["version"]

    def _seed(self, ship_qty, shore_qty):
        _, ship = _request(f"{self.base}/api/items/{self.iid}/ships", "POST",
                           {"name": f"沉船-{id(self)}-{ship_qty}"})
        _, tank = _request(
            f"{self.base}/api/items/{self.iid}/ships/{ship['id']}/tanks",
            "POST", {"name": "左舱", "capacity": 500})
        _, shore = _request(f"{self.base}/api/items/{self.iid}/shore-tanks",
                            "POST", {"name": f"岸罐-{id(self)}-{shore_qty}",
                                     "capacity": 1000})
        _, tr = _request(f"{self.base}/api/items/{self.iid}/transfers", "POST",
                         {"tank_id": tank["id"], "shore_tank_id": shore["id"],
                          "planned_qty": 100})
        _request(f"{self.base}/api/items/{self.iid}/transfers/{tr['id']}/precheck",
                 "POST", {"grounding_resistance": 5, "wind_speed": 3})
        _request(f"{self.base}/api/items/{self.iid}/transfers/{tr['id']}/measure",
                 "POST", {"ship_qty": ship_qty, "shore_qty": shore_qty,
                          "ship_ticket": "S-1", "shore_ticket": "R-1"})
        return tr["id"]

    def test_409_lists_gaps_and_progress_carries_detail(self):
        tr_id = self._seed(100, 90)  # 差10%，转待核对
        # 直接转入回收 -> 409 且 gaps 列出 discrepancy
        status, payload = _request(
            f"{self.base}/api/items/{self.iid}/transition", "POST",
            {"target": "recovering", "expected_version": self._version()},
            actor="ops", role="operations")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "ConflictError")
        self.assertEqual(len(payload["gaps"]), 1)
        gap = payload["gaps"][0]
        self.assertEqual(gap["code"], "discrepancy")
        self.assertGreater(gap["diff_ratio"], 0.02)
        self.assertIn("待核对", gap["message"])
        # 详情接口带进度
        _, detail = _request(
            f"{self.base}/api/items/{self.iid}/transfers/{tr_id}")
        self.assertEqual(detail["gap"]["code"], "discrepancy")
        _, progress = _request(f"{self.base}/api/items/{self.iid}/progress")
        self.assertEqual(progress["open"], 1)
        self.assertEqual(len(progress["gaps"]), 1)
        # 审计带进度
        _, audit = _request(f"{self.base}/api/audit",
                            actor="cmd", role="response_commander")
        measured = [e for e in audit["events"]
                    if e.get("action") == "transfer_measure"
                    and e["detail"].get("transfer_id") == tr_id]
        self.assertTrue(measured)
        self.assertEqual(measured[-1]["detail"]["progress"]["status"],
                         "reconciling")
        # 补齐核对说明，另一人确认后解锁
        status, _ = _request(
            f"{self.base}/api/items/{self.iid}/transfers/{tr_id}/confirm",
            "POST", {"reconciliation_note": "复测后差异消除"},
            actor="cmd", role="response_commander")
        self.assertEqual(status, 200)
        status, item = _request(
            f"{self.base}/api/items/{self.iid}/transition", "POST",
            {"target": "recovering", "expected_version": self._version()},
            actor="ops", role="operations")
        self.assertEqual(status, 200)
        self.assertEqual(item["status"], "recovering")

    def test_held_precheck_returns_named_failures(self):
        _, ship = _request(f"{self.base}/api/items/{self.iid}/ships", "POST",
                           {"name": f"held-{id(self)}"})
        _, tank = _request(
            f"{self.base}/api/items/{self.iid}/ships/{ship['id']}/tanks",
            "POST", {"name": "右舱", "capacity": 500})
        _, shore = _request(f"{self.base}/api/items/{self.iid}/shore-tanks",
                            "POST", {"name": f"小罐-{id(self)}", "capacity": 50})
        _, tr = _request(f"{self.base}/api/items/{self.iid}/transfers", "POST",
                         {"tank_id": tank["id"], "shore_tank_id": shore["id"],
                          "planned_qty": 100})
        status, payload = _request(
            f"{self.base}/api/items/{self.iid}/transfers/{tr['id']}/precheck",
            "POST", {"grounding_resistance": 150, "wind_speed": 12})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "held")
        self.assertEqual(set(payload["failed_items"]),
                         {"remaining_capacity", "grounding_resistance",
                          "wind_speed"})
        self.assertIn("停待处理", payload["precheck_message"])


if __name__ == "__main__":
    unittest.main()
