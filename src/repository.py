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
                CREATE TABLE IF NOT EXISTS ships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, name)
                );
                CREATE TABLE IF NOT EXISTS wreck_tanks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ship_id INTEGER NOT NULL REFERENCES ships(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    capacity REAL NOT NULL,
                    plan_version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(ship_id, name)
                );
                CREATE TABLE IF NOT EXISTS shore_tanks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    capacity REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, name)
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    tank_id INTEGER NOT NULL REFERENCES wreck_tanks(id),
                    shore_tank_id INTEGER NOT NULL REFERENCES shore_tanks(id),
                    ref TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planned'
                        CHECK(status IN ('planned','held','in_progress','reconciling','confirmed')),
                    planned_qty REAL NOT NULL,
                    grounding_resistance REAL,
                    wind_speed REAL,
                    remaining_capacity REAL,
                    failed_items TEXT NOT NULL DEFAULT '[]',
                    checked_at TEXT,
                    ship_qty REAL,
                    shore_qty REAL,
                    diff_ratio REAL,
                    ship_ticket TEXT,
                    shore_ticket TEXT,
                    reconciliation_note TEXT,
                    measured_by TEXT,
                    measured_at TEXT,
                    confirmed_by TEXT,
                    confirmed_at TEXT,
                    confirmation_voided INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, ref)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_transfers_open_tank
                    ON transfers(tank_id)
                    WHERE status IN ('planned','held','in_progress','reconciling');
                CREATE INDEX IF NOT EXISTS ix_transfers_item ON transfers(item_id);
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

    # ---------- 舱级转驳实体 ----------
    def create_ship(self, item_id: int, name: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO ships(item_id,name,created_by,created_at) VALUES(?,?,?,?)",
                    (item_id, name, actor, now))
                ship_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("沉船名称已存在") from exc
        return self.get_ship(ship_id)

    def get_ship(self, ship_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM ships WHERE id=?", (ship_id,)).fetchone()
        if row is None:
            raise NotFoundError("沉船不存在")
        return dict(row)

    def list_ships(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM ships WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def create_tank(self, ship_id: int, item_id: int, name: str,
                    capacity: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO wreck_tanks(ship_id,item_id,name,capacity,plan_version,
                       updated_at,created_at) VALUES(?,?,?,?,'1',?,?)""",
                    (ship_id, item_id, name, capacity, now, now))
                tank_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("船舱名称在该沉船已存在") from exc
        return self.get_tank(tank_id)

    def get_tank(self, tank_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM wreck_tanks WHERE id=?", (tank_id,)).fetchone()
        if row is None:
            raise NotFoundError("船舱不存在")
        return dict(row)

    def list_tanks(self, ship_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM wreck_tanks WHERE ship_id=? ORDER BY id",
                (ship_id,)).fetchall()
        return [dict(row) for row in rows]

    def revise_tank_plan(self, tank_id: int, capacity: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """UPDATE wreck_tanks SET capacity=?, plan_version=plan_version+1, updated_at=?
                       WHERE id=?""", (capacity, now, tank_id))
                if cur.rowcount == 0:
                    raise NotFoundError("船舱不存在")
                # 同舱已确认作业原确认作废：部分唯一索引会在存在其他未结作业时冲突，整体回滚
                self.conn.execute(
                    """UPDATE transfers SET confirmation_voided=1, updated_at=?
                       WHERE tank_id=? AND status='confirmed' AND confirmation_voided=0""",
                    (now, tank_id))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该舱存在未结转驳作业，舱容计划修订已回滚") from exc
        return self.get_tank(tank_id)

    def create_shore_tank(self, item_id: int, name: str, capacity: float) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO shore_tanks(item_id,name,capacity,created_at) VALUES(?,?,?,?)",
                    (item_id, name, capacity, now))
                tank_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("岸罐名称已存在") from exc
        return self.get_shore_tank(tank_id)

    def get_shore_tank(self, shore_tank_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM shore_tanks WHERE id=?", (shore_tank_id,)).fetchone()
        if row is None:
            raise NotFoundError("岸罐不存在")
        return dict(row)

    def list_shore_tanks(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM shore_tanks WHERE item_id=? ORDER BY id",
                (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def shore_tank_committed(self, shore_tank_id: int,
                             exclude_transfer_id: Optional[int] = None) -> float:
        """岸罐已承诺占用容量：未结作业按计划量，已确认作业按岸端实收量"""
        sql = " WHERE shore_tank_id=?"
        params: tuple = (shore_tank_id,)
        if exclude_transfer_id is not None:
            sql += " AND id!=?"
            params = (shore_tank_id, exclude_transfer_id)
        with self._lock:
            row = self.conn.execute(
                f"""SELECT COALESCE(SUM(CASE WHEN status='confirmed' AND confirmation_voided=0
                        THEN COALESCE(shore_qty, planned_qty) ELSE planned_qty END),0) AS n
                   FROM transfers{sql}
                   AND (status!='confirmed' OR confirmation_voided=1)""",
                params).fetchone()
            confirmed = self.conn.execute(
                f"""SELECT COALESCE(SUM(COALESCE(shore_qty, planned_qty)),0) AS n
                   FROM transfers{sql}
                   AND status='confirmed' AND confirmation_voided=0""",
                params).fetchone()
        return float(row["n"]) + float(confirmed["n"])

    _TRANSFER_FIELDS = (
        "status", "planned_qty", "grounding_resistance", "wind_speed", "remaining_capacity",
        "failed_items", "checked_at", "ship_qty", "shore_qty", "diff_ratio", "ship_ticket",
        "shore_ticket", "reconciliation_note", "measured_by", "measured_at",
        "confirmed_by", "confirmed_at", "confirmation_voided")

    def create_transfer(self, item_id: int, tank_id: int, shore_tank_id: int,
                        ref: str, planned_qty: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO transfers(item_id,tank_id,shore_tank_id,ref,status,
                       planned_qty,failed_items,confirmation_voided,created_by,
                       created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, tank_id, shore_tank_id, ref, "planned",
                     planned_qty, "[]", 0, actor, now, now))
                transfer_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            msg = str(exc)
            if "ux_transfers_open_tank" in msg:
                raise ConflictError("该沉船舱已有一项未结作业，不能重复开工") from exc
            raise ConflictError("转驳作业编号已存在") from exc
        return self.get_transfer(transfer_id)

    def get_transfer(self, transfer_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """SELECT t.*, s.name AS ship_name, tk.name AS tank_name,
                          tk.plan_version AS plan_version, tk.capacity AS tank_capacity,
                          st.name AS shore_tank_name, st.capacity AS shore_tank_capacity,
                          st.id AS shore_side_id
                   FROM transfers t
                   JOIN wreck_tanks tk ON tk.id=t.tank_id
                   JOIN ships s ON s.id=tk.ship_id
                   JOIN shore_tanks st ON st.id=t.shore_tank_id
                   WHERE t.id=?""", (transfer_id,)).fetchone()
        if row is None:
            raise NotFoundError("转驳作业不存在")
        return self._transfer(row)

    def list_transfers(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT t.*, s.name AS ship_name, tk.name AS tank_name,
                          tk.plan_version AS plan_version, tk.capacity AS tank_capacity,
                          st.name AS shore_tank_name, st.capacity AS shore_tank_capacity,
                          st.id AS shore_side_id
                   FROM transfers t
                   JOIN wreck_tanks tk ON tk.id=t.tank_id
                   JOIN ships s ON s.id=tk.ship_id
                   JOIN shore_tanks st ON st.id=t.shore_tank_id
                   WHERE t.item_id=? ORDER BY t.id""", (item_id,)).fetchall()
        return [self._transfer(row) for row in rows]

    def update_transfer(self, transfer_id: int, fields: Dict[str, Any],
                        void_tank_id: Optional[int] = None) -> Dict[str, Any]:
        now = utc_now()
        updates = {key: value for key, value in fields.items()
                   if key in self._TRANSFER_FIELDS}
        updates["updated_at"] = now
        columns = ",".join(f"{key}=?" for key in updates)
        params = tuple(updates.values()) + (transfer_id,)
        with self._lock, self.conn:
            try:
                cur = self.conn.execute(
                    f"UPDATE transfers SET {columns} WHERE id=?", params)
            except sqlite3.IntegrityError as exc:
                if "ux_transfers_open_tank" in str(exc):
                    raise ConflictError("该沉船舱已有一项未结作业，不能重复开工") from exc
                raise
            if cur.rowcount == 0:
                raise NotFoundError("转驳作业不存在")
        return self.get_transfer(transfer_id)

    @staticmethod
    def _transfer(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["failed_items"] = json.loads(result["failed_items"] or "[]")
        result["confirmation_voided"] = bool(result["confirmation_voided"])
        return result

    def close(self) -> None:
        with self._lock:
            self.conn.close()
