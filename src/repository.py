from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tanks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    capacity REAL NOT NULL,
                    plan_version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, name)
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    tank_id INTEGER NOT NULL REFERENCES tanks(id) ON DELETE CASCADE,
                    ship_name TEXT NOT NULL,
                    planned_quantity REAL NOT NULL,
                    ship_quantity REAL,
                    shore_quantity REAL,
                    status TEXT NOT NULL
                        CHECK(status IN ('pending','open','review','confirmed','void')),
                    failed_checks TEXT NOT NULL DEFAULT '[]',
                    measurement_ticket TEXT,
                    plan_version INTEGER NOT NULL DEFAULT 1,
                    quantities_by TEXT,
                    confirmed_by TEXT,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _transfer(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["failed_checks"] = json.loads(item["failed_checks"] or "[]")
        return item

    def create_tank(self, item_id: int, name: str, capacity: float,
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO tanks(item_id, name, capacity, plan_version,
                       created_by, created_at) VALUES(?,?,?,1,?,?)""",
                    (item_id, name, capacity, actor, now),
                )
                tank_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("舱名已存在") from exc
        return self.get_tank(item_id, tank_id)

    def get_tank(self, item_id: int, tank_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tanks WHERE id=? AND item_id=?", (tank_id, item_id)
            ).fetchone()
        if row is None:
            raise NotFoundError("舱不存在")
        return dict(row)

    def list_tanks(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tanks WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def revise_tank_plan(self, item_id: int, tank_id: int, capacity: float,
                         actor: str) -> tuple:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE tanks SET capacity=?, plan_version=plan_version+1
                   WHERE id=? AND item_id=?""",
                (capacity, tank_id, item_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("舱不存在")
            rows = self.conn.execute(
                "SELECT id FROM transfers WHERE tank_id=? AND status='confirmed'",
                (tank_id,),
            ).fetchall()
            voided = [int(row["id"]) for row in rows]
            if voided:
                marks = ",".join("?" * len(voided))
                self.conn.execute(
                    f"UPDATE transfers SET status='void', updated_at=? WHERE id IN ({marks})",
                    [utc_now()] + voided,
                )
        return self.get_tank(item_id, tank_id), voided

    def tank_extracted(self, tank_id: int) -> float:
        with self._lock:
            row = self.conn.execute(
                """SELECT COALESCE(SUM(ship_quantity),0) AS total FROM transfers
                   WHERE tank_id=? AND ship_quantity IS NOT NULL""",
                (tank_id,),
            ).fetchone()
        return float(row["total"])

    def tank_busy_transfer(self, tank_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM transfers WHERE tank_id=?
                   AND status IN ('pending','open','review') ORDER BY id DESC LIMIT 1""",
                (tank_id,),
            ).fetchone()
        return self._transfer(row) if row is not None else None

    def create_transfer(self, item_id: int, tank_id: int, ship_name: str,
                        planned_quantity: float, status: str, failed_checks: list,
                        plan_version: int, external_ref: Optional[str],
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                busy = self.conn.execute(
                    """SELECT * FROM transfers WHERE tank_id=?
                       AND status IN ('pending','open','review') ORDER BY id DESC LIMIT 1""",
                    (tank_id,),
                ).fetchone()
                if busy is not None:
                    gap = {"transfer_id": busy["id"], "tank_id": tank_id,
                           "ship_name": busy["ship_name"], "status": busy["status"],
                           "missing": "沉船舱已有未结作业"}
                    raise ConflictError("沉船舱已有未结作业", details={"gaps": [gap]})
                cur = self.conn.execute(
                    """INSERT INTO transfers(item_id, tank_id, ship_name, planned_quantity,
                       status, failed_checks, plan_version, external_ref, created_by,
                       created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, tank_id, ship_name, planned_quantity, status,
                     json.dumps(failed_checks, ensure_ascii=False), plan_version,
                     external_ref, actor, now, now),
                )
                transfer_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("转驳作业唯一标识已存在") from exc
        return self.get_transfer(item_id, transfer_id)

    def get_transfer(self, item_id: int, transfer_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM transfers WHERE id=? AND item_id=?",
                (transfer_id, item_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("转驳作业不存在")
        return self._transfer(row)

    def list_transfers(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM transfers WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self._transfer(row) for row in rows]

    def set_transfer_recheck(self, transfer_id: int, planned_quantity: float,
                             status: str, failed_checks: list) -> None:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE transfers SET planned_quantity=?, status=?, failed_checks=?,
                   updated_at=? WHERE id=? AND status='pending'""",
                (planned_quantity, status,
                 json.dumps(failed_checks, ensure_ascii=False), utc_now(), transfer_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("仅待处理作业可重新核对")

    def set_transfer_quantities(self, transfer_id: int, ship_quantity: float,
                                shore_quantity: float, status: str, actor: str,
                                plan_version: int,
                                confirmed_by: Optional[str]) -> None:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE transfers SET ship_quantity=?, shore_quantity=?, status=?,
                   quantities_by=?, confirmed_by=?, plan_version=?, updated_at=?
                   WHERE id=? AND status='open'""",
                (ship_quantity, shore_quantity, status, actor, confirmed_by,
                 plan_version, utc_now(), transfer_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("仅进行中的作业可录入计量")

    def set_transfer_confirmed(self, transfer_id: int, measurement_ticket: str,
                               plan_version: int, actor: str) -> None:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE transfers SET status='confirmed', measurement_ticket=?,
                   confirmed_by=?, plan_version=?, updated_at=?
                   WHERE id=? AND status IN ('review','void')""",
                (measurement_ticket, actor, plan_version, utc_now(), transfer_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("仅待核对或已作废的作业可确认")

    def unresolved_differences(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT t.*, k.name AS tank_name FROM transfers t
                   JOIN tanks k ON k.id=t.tank_id
                   WHERE t.item_id=? AND t.status IN ('review','void') ORDER BY t.id""",
                (item_id,),
            ).fetchall()
        return [self._transfer(row) for row in rows]

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
