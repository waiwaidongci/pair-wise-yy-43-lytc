from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .domain import (ConflictError, NotFoundError, ValidationError,
                     ensure_role, normalize_severity, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, FAIL_ITEM_LABELS,
                    RECORD_ROLES, SHIP_ENTITY, SHIP_ROLES, SHORE_TANK_ENTITY,
                    TANK_ENTITY, TANK_PLAN_ROLES,
                    TRANSFER_CONFIRM_ROLES, TRANSFER_CREATE_ROLES,
                    TRANSFER_ENTITY, TRANSFER_MEASURE_ROLES,
                    TRANSFER_PRECHECK_ROLES, VIEW_ROLES,
                    completion_blockers, escalation_required, measure_diff_ratio,
                    measure_over_limit, precheck_failures, priority_score,
                    recovery_gaps, response_deadline_hours,
                    role_for_transition, transfer_gap, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        gaps = []
        if target == "recovering":
            gaps = recovery_gaps(self.repository.list_transfers(item_id))
            if gaps:
                raise ConflictError(
                    f"尚有{len(gaps)}项舱级转驳差异未结，不能从围控转入回收", gaps)
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        detail = {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        }
        if target == "recovering":
            detail["transfer_progress"] = self.transfer_progress(item_id)
        self.repository.append_audit("transition", ENTITY, item_id, actor, detail)
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result

    # ---------- 舱级转驳 ----------
    def _require_status(self, item_id: int, allowed) -> Dict[str, Any]:
        item = self.repository.get_item(item_id)
        if item["status"] not in allowed:
            raise ConflictError(f"事件当前为{item['status']}，不能执行该操作")
        return item

    def create_ship(self, item_id: int, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SHIP_ROLES)
        actor = require_text(actor, "actor", 100)
        self._require_status(item_id, ("containing", "recovering"))
        name = require_text(payload.get("name"), "name", 100)
        ship = self.repository.create_ship(item_id, name, actor)
        self.repository.append_audit("ship_create", SHIP_ENTITY, item_id, actor, {
            "ship_id": ship["id"], "name": name})
        return ship

    def list_ships(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_ships(item_id)

    def create_tank(self, item_id: int, ship_id: int, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SHIP_ROLES)
        actor = require_text(actor, "actor", 100)
        self._require_status(item_id, ("containing", "recovering"))
        ship = self.repository.get_ship(ship_id)
        if ship["item_id"] != item_id:
            raise NotFoundError("船舱不属于该事件")
        name = require_text(payload.get("name"), "name", 100)
        capacity = require_number(payload.get("capacity"), "capacity", 0.000001)
        tank = self.repository.create_tank(ship_id, item_id, name, capacity, actor)
        self.repository.append_audit("tank_create", TANK_ENTITY, item_id, actor, {
            "tank_id": tank["id"], "ship": ship["name"], "name": name,
            "capacity": capacity, "plan_version": 1})
        return tank

    def revise_tank_plan(self, item_id: int, tank_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TANK_PLAN_ROLES)
        actor = require_text(actor, "actor", 100)
        self._require_status(item_id, ("containing", "recovering"))
        tank = self.repository.get_tank(tank_id)
        if tank["item_id"] != item_id:
            raise NotFoundError("船舱不属于该事件")
        capacity = require_number(payload.get("capacity"), "capacity", 0.000001)
        old_version = tank["plan_version"]
        if capacity == tank["capacity"]:
            raise ValidationError("新舱容与现行计划一致，无需修订")
        # 同舱有在途作业时不能修订：作废确认会与在途作业并存，违反一舱一未结
        inflight = [t for t in self.repository.list_transfers(item_id)
                    if t["tank_id"] == tank_id and t["status"] != "confirmed"]
        if inflight:
            raise ConflictError(
                f"船舱{tank['name']}有{len(inflight)}项在途作业，不能修订舱容计划",
                [{"tank_id": tank_id, "code": "inflight_transfer",
                  "transfer_id": t["id"],
                  "message": f"作业{t['ref']}处于{t['status']}，请先结项"}
                 for t in inflight])
        before = {t["id"]: t for t in self.repository.list_transfers(item_id)
                  if t["tank_id"] == tank_id}
        updated = self.repository.revise_tank_plan(tank_id, capacity, actor)
        # 原确认全部作废（保留confirmed状态，以voided标记），需重新核对确认
        voided = []
        for transfer_id, transfer in before.items():
            if not (transfer["status"] == "confirmed"
                    and not transfer["confirmation_voided"]):
                continue
            mark = self.repository.get_transfer(transfer_id)
            mark = self.enrich_transfer(mark)
            voided.append({"transfer_id": transfer_id, "ref": transfer["ref"],
                           "status": mark["status"]})
            self.repository.append_audit("transfer_voided", TRANSFER_ENTITY, item_id, actor, {
                "transfer_id": transfer_id, "tank": tank["name"],
                "old_plan_version": old_version, "new_plan_version": updated["plan_version"],
                "progress": self._transfer_progress_entry(mark)})
        self.repository.append_audit("tank_plan_revise", TANK_ENTITY, item_id, actor, {
            "tank_id": tank_id, "name": tank["name"],
            "old_capacity": tank["capacity"], "new_capacity": capacity,
            "old_plan_version": old_version,
            "new_plan_version": updated["plan_version"], "voided": voided})
        result = dict(updated)
        result["voided_transfers"] = voided
        return result

    def create_shore_tank(self, item_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SHIP_ROLES)
        actor = require_text(actor, "actor", 100)
        self._require_status(item_id, ("containing", "recovering"))
        name = require_text(payload.get("name"), "name", 100)
        capacity = require_number(payload.get("capacity"), "capacity", 0.000001)
        shore_tank = self.repository.create_shore_tank(item_id, name, capacity)
        self.repository.append_audit("shore_tank_create", SHORE_TANK_ENTITY, item_id, actor, {
            "shore_tank_id": shore_tank["id"], "name": name, "capacity": capacity})
        return shore_tank

    def list_shore_tanks(self, item_id: int, role: str) -> list:
        self._view(role)
        return [self._shore_tank_progress(t)
                for t in self.repository.list_shore_tanks(item_id)]

    def _shore_tank_progress(self, shore_tank: Dict[str, Any]) -> Dict[str, Any]:
        committed = self.repository.shore_tank_committed(shore_tank["id"])
        result = dict(shore_tank)
        result["committed_qty"] = round(committed, 6)
        result["remaining_capacity"] = round(shore_tank["capacity"] - committed, 6)
        return result

    def _next_transfer_ref(self, item_id: int) -> str:
        count = len(self.repository.list_transfers(item_id))
        return f"TR-{count + 1}"

    def create_transfer(self, item_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        self._require_status(item_id, ("containing", "recovering"))
        tank_id = require_number(payload.get("tank_id"), "tank_id", 1)
        shore_tank_id = require_number(payload.get("shore_tank_id"), "shore_tank_id", 1)
        planned_qty = require_number(payload.get("planned_qty"), "planned_qty", 0.000001)
        tank = self.repository.get_tank(int(tank_id))
        if tank["item_id"] != item_id:
            raise NotFoundError("船舱不属于该事件")
        shore_tank = self.repository.get_shore_tank(int(shore_tank_id))
        if shore_tank["item_id"] != item_id:
            raise NotFoundError("岸罐不属于该事件")
        ref = payload.get("ref")
        ref = require_text(ref, "ref", 100) if ref is not None else self._next_transfer_ref(item_id)
        # 沉船舱同时只接一项未结作业：在途作业或作废待重新确认的作业都算未结
        for existing in self.repository.list_transfers(item_id):
            if existing["tank_id"] != int(tank_id):
                continue
            if existing["status"] != "confirmed" or existing["confirmation_voided"]:
                raise ConflictError("该沉船舱已有一项未结作业，不能重复开工")
        transfer = self.repository.create_transfer(
            item_id, int(tank_id), int(shore_tank_id), ref, planned_qty, actor)
        transfer = self.enrich_transfer(transfer)
        self.repository.append_audit("transfer_create", TRANSFER_ENTITY, item_id, actor, {
            "transfer_id": transfer["id"], "ref": ref,
            "ship": transfer["ship_name"], "tank": transfer["tank_name"],
            "shore_tank": transfer["shore_tank_name"], "planned_qty": planned_qty,
            "progress": self._transfer_progress_entry(transfer)})
        return transfer

    def precheck_transfer(self, item_id: int, transfer_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_PRECHECK_ROLES)
        actor = require_text(actor, "actor", 100)
        transfer = self.repository.get_transfer(transfer_id)
        if transfer["item_id"] != item_id:
            raise NotFoundError("转驳作业不属于该事件")
        if transfer["status"] not in ("planned", "held"):
            raise ConflictError(f"作业当前为{transfer['status']}，不能重新开工核对")
        grounding = require_number(payload.get("grounding_resistance"),
                                   "grounding_resistance", 0.0)
        wind = require_number(payload.get("wind_speed"), "wind_speed", 0.0)
        shore_tank = self.repository.get_shore_tank(transfer["shore_tank_id"])
        committed = self.repository.shore_tank_committed(
            transfer["shore_tank_id"], exclude_transfer_id=transfer_id)
        remaining = shore_tank["capacity"] - committed
        from .audit import utc_now
        failures = precheck_failures(remaining, transfer["planned_qty"], grounding, wind)
        checked = {"grounding_resistance": grounding, "wind_speed": wind,
                   "remaining_capacity": round(remaining, 6)}
        if failures:
            saved = self.repository.update_transfer(transfer_id, {
                "status": "held", "failed_items": json.dumps(failures, ensure_ascii=False),
                "remaining_capacity": round(remaining, 6),
                "grounding_resistance": grounding, "wind_speed": wind,
                "checked_at": utc_now()})
            saved = self.enrich_transfer(saved)
            labels = "、".join(FAIL_ITEM_LABELS[f] for f in failures)
            self.repository.append_audit("transfer_precheck", TRANSFER_ENTITY,
                                         item_id, actor, {
                "transfer_id": transfer_id, "result": "held",
                "failed_items": failures, "failed_labels": labels,
                "checks": checked,
                "progress": self._transfer_progress_entry(saved)})
            saved["precheck_message"] = f"{labels}不符，停待处理"
            return saved
        saved = self.repository.update_transfer(transfer_id, {
            "status": "in_progress", "failed_items": "[]",
            "remaining_capacity": round(remaining, 6),
            "grounding_resistance": grounding, "wind_speed": wind,
            "checked_at": utc_now()})
        saved = self.enrich_transfer(saved)
        self.repository.append_audit("transfer_precheck", TRANSFER_ENTITY,
                                     item_id, actor, {
            "transfer_id": transfer_id, "result": "in_progress",
            "checks": checked,
            "progress": self._transfer_progress_entry(saved)})
        return saved

    def measure_transfer(self, item_id: int, transfer_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_MEASURE_ROLES)
        actor = require_text(actor, "actor", 100)
        transfer = self.repository.get_transfer(transfer_id)
        if transfer["item_id"] != item_id:
            raise NotFoundError("转驳作业不属于该事件")
        if transfer["status"] not in ("in_progress", "reconciling", "confirmed") or \
                (transfer["status"] == "confirmed" and not transfer["confirmation_voided"]):
            raise ConflictError(f"作业当前为{transfer['status']}，不能登记计量")
        ship_qty = require_number(payload.get("ship_qty"), "ship_qty", 0.0)
        shore_qty = require_number(payload.get("shore_qty"), "shore_qty", 0.0)
        ship_ticket = require_text(payload.get("ship_ticket"), "ship_ticket", 100)
        shore_ticket = require_text(payload.get("shore_ticket"), "shore_ticket", 100)
        note = payload.get("reconciliation_note")
        if note is not None:
            note = require_text(note, "reconciliation_note")
        ratio = measure_diff_ratio(ship_qty, shore_qty)
        over = measure_over_limit(ship_qty, shore_qty)
        if over or (transfer["status"] == "reconciling" and not note):
            status = "reconciling"
        else:
            status = "in_progress"
        from .audit import utc_now
        fields = {"status": status, "ship_qty": ship_qty, "shore_qty": shore_qty,
                  "diff_ratio": round(ratio, 6), "ship_ticket": ship_ticket,
                  "shore_ticket": shore_ticket, "measured_by": actor,
                  "measured_at": utc_now()}
        if note is not None:
            fields["reconciliation_note"] = note
        saved = self.repository.update_transfer(transfer_id, fields)
        saved = self.enrich_transfer(saved)
        self.repository.append_audit("transfer_measure", TRANSFER_ENTITY,
                                     item_id, actor, {
            "transfer_id": transfer_id, "ship_qty": ship_qty,
            "shore_qty": shore_qty, "diff_ratio": round(ratio, 6),
            "over_limit": over, "status": status,
            "ship_ticket": ship_ticket, "shore_ticket": shore_ticket,
            "reconciliation_note": note,
            "progress": self._transfer_progress_entry(saved)})
        return saved

    def confirm_transfer(self, item_id: int, transfer_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        transfer = self.repository.get_transfer(transfer_id)
        if transfer["item_id"] != item_id:
            raise NotFoundError("转驳作业不属于该事件")
        gaps = []
        if transfer["ship_qty"] is None or transfer["shore_qty"] is None:
            gaps.append("尚未登记船端交油与岸端接收计量")
        if not transfer["ship_ticket"] or not transfer["shore_ticket"]:
            gaps.append("缺少船端或岸端计量单")
        if transfer["measured_by"] and actor == transfer["measured_by"]:
            gaps.append("确认人不能是计量记录人，必须由另一人确认")
        note = payload.get("reconciliation_note")
        if note is not None:
            note = require_text(note, "reconciliation_note")
        if transfer["status"] == "reconciling" and not (
                transfer["reconciliation_note"] or note):
            gaps.append("计量差超过2%，须补齐核对说明/计量单后再确认")
        if transfer["status"] == "confirmed" and not transfer["confirmation_voided"]:
            raise ConflictError("作业已确认且未作废")
        if transfer["status"] == "planned" or transfer["status"] == "held":
            gaps.append("作业尚未完成开工核对与计量")
        if gaps:
            raise ConflictError("；".join(gaps),
                                [{"transfer_id": transfer_id, "code": "confirm_blocked",
                                  "message": g} for g in gaps])
        from .audit import utc_now
        fields = {"status": "confirmed", "confirmation_voided": 0,
                  "confirmed_by": actor, "confirmed_at": utc_now()}
        if note is not None:
            fields["reconciliation_note"] = note
        saved = self.repository.update_transfer(transfer_id, fields)
        saved = self.enrich_transfer(saved)
        self.repository.append_audit("transfer_confirm", TRANSFER_ENTITY,
                                     item_id, actor, {
            "transfer_id": transfer_id, "measured_by": transfer["measured_by"],
            "confirmed_by": actor, "diff_ratio": transfer["diff_ratio"],
            "plan_version_at_check": transfer["plan_version"],
            "progress": self._transfer_progress_entry(saved)})
        return saved

    def get_transfer(self, item_id: int, transfer_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        transfer = self.repository.get_transfer(transfer_id)
        if transfer["item_id"] != item_id:
            raise NotFoundError("转驳作业不属于该事件")
        return self.enrich_transfer(transfer)

    def list_transfers(self, item_id: int, role: str) -> list:
        self._view(role)
        return [self.enrich_transfer(t)
                for t in self.repository.list_transfers(item_id)]

    def enrich_transfer(self, transfer: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(transfer)
        result["failed_labels"] = [FAIL_ITEM_LABELS[f] for f in transfer["failed_items"]]
        gap = transfer_gap(transfer)
        result["open"] = gap is not None
        result["gap"] = gap
        result.pop("shore_side_id", None)
        return result

    def _transfer_progress_entry(self, transfer: Dict[str, Any]) -> Dict[str, Any]:
        entry = {
            "transfer_id": transfer["id"], "ref": transfer["ref"],
            "ship": transfer["ship_name"], "tank": transfer["tank_name"],
            "shore_tank": transfer["shore_tank_name"],
            "status": transfer["status"], "planned_qty": transfer["planned_qty"],
            "ship_qty": transfer["ship_qty"], "shore_qty": transfer["shore_qty"],
            "diff_ratio": transfer["diff_ratio"],
            "confirmation_voided": transfer["confirmation_voided"],
        }
        gap = transfer_gap(transfer)
        entry["open"] = gap is not None
        entry["gap"] = gap["message"] if gap else None
        return entry

    def transfer_progress(self, item_id: int) -> Dict[str, Any]:
        transfers = [self.enrich_transfer(t)
                     for t in self.repository.list_transfers(item_id)]
        confirmed = [t for t in transfers
                     if t["status"] == "confirmed" and not t["confirmation_voided"]]
        ship_pumped: Dict[str, float] = {}
        shore_received: Dict[str, float] = {}
        for t in confirmed:
            ship_pumped[t["ship_name"]] = ship_pumped.get(t["ship_name"], 0.0) + (
                t["ship_qty"] or 0.0)
            shore_received[t["shore_tank_name"]] = shore_received.get(
                t["shore_tank_name"], 0.0) + (t["shore_qty"] or 0.0)
        gaps = recovery_gaps(transfers)
        return {
            "total": len(transfers),
            "confirmed": len(confirmed),
            "open": len(transfers) - len(confirmed),
            "pumped_qty": round(sum(ship_pumped.values()), 6),
            "received_qty": round(sum(shore_received.values()), 6),
            "by_ship": {k: round(v, 6) for k, v in sorted(ship_pumped.items())},
            "by_shore_tank": {k: round(v, 6) for k, v in sorted(shore_received.items())},
            "gaps": gaps,
            "transfers": [self._transfer_progress_entry(t) for t in transfers],
        }

    def fleet(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        ships = self.repository.list_ships(item_id)
        for ship in ships:
            ship["tanks"] = self.repository.list_tanks(ship["id"])
        shore_tanks = [self._shore_tank_progress(t)
                       for t in self.repository.list_shore_tanks(item_id)]
        return {"ships": ships, "shore_tanks": shore_tanks,
                "progress": self.transfer_progress(item_id)}
