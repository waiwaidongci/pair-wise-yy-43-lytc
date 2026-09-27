from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, PermissionDenied, ValidationError,
                     ensure_role, normalize_severity, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CONFIRM_ROLES, CREATE_ROLES, ENTITY, PLAN_ROLES,
                    RECORD_ROLES, TANK_BUSY_STATUSES, TANK_ROLES, TITLE,
                    TRANSFER_ROLES, TRANSFER_STATUSES, VIEW_ROLES,
                    completion_blockers, discrepancy_pct, escalation_required,
                    prestart_failures, priority_score, response_deadline_hours,
                    role_for_transition, transfer_needs_review,
                    validate_transition)


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
        if item["status"] == "containing" and target == "recovering":
            gaps = self._difference_gaps(item_id)
            if gaps:
                raise ConflictError("存在未结计量差异，不能从围控转入回收",
                                    details={"gaps": gaps})
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        result = self.enrich(self.repository.get_item(item_id))
        result["lightering"] = self.lightering_progress(item_id)
        return result

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def create_tank(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, TANK_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        capacity = require_number(payload.get("capacity"), "capacity", 0.000001)
        tank = self.repository.create_tank(item_id, name, capacity, actor)
        self.repository.append_audit("tank_create", ENTITY, item_id, actor, {
            "tank_id": tank["id"], "name": name, "capacity": capacity,
        })
        return self._tank_view(tank)

    def list_tanks(self, item_id: int, role: str) -> list:
        self._view(role)
        return [self._tank_view(tank) for tank in self.repository.list_tanks(item_id)]

    def revise_plan(self, item_id: int, tank_id: int, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, PLAN_ROLES)
        actor = require_text(actor, "actor", 100)
        capacity = require_number(payload.get("capacity"), "capacity", 0.000001)
        before = self.repository.get_tank(item_id, tank_id)
        tank, voided = self.repository.revise_tank_plan(item_id, tank_id, capacity, actor)
        self.repository.append_audit("tank_plan_revise", ENTITY, item_id, actor, {
            "tank_id": tank_id, "old_capacity": before["capacity"],
            "new_capacity": capacity, "plan_version": tank["plan_version"],
            "voided_transfer_ids": voided,
            "progress": self._progress_snapshot(item_id),
        })
        return self._tank_view(tank)

    def create_transfer(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_ROLES)
        actor = require_text(actor, "actor", 100)
        tank_id = payload.get("tank_id")
        if isinstance(tank_id, bool) or not isinstance(tank_id, int) or tank_id < 1:
            raise ValidationError("tank_id必须是正整数")
        ship_name = require_text(payload.get("ship_name"), "ship_name", 100)
        quantity = require_number(payload.get("quantity"), "quantity", 0.000001)
        ground = require_number(payload.get("ground_resistance"), "ground_resistance")
        wind = require_number(payload.get("wind_speed"), "wind_speed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        tank = self.repository.get_tank(item_id, tank_id)
        remaining = tank["capacity"] - self.repository.tank_extracted(tank_id)
        failures = prestart_failures(quantity, remaining, ground, wind)
        status = "pending" if failures else "open"
        transfer = self.repository.create_transfer(
            item_id, tank_id, ship_name, quantity, status, failures,
            tank["plan_version"], external_ref, actor)
        self.repository.append_audit("transfer_create", ENTITY, item_id, actor, {
            "transfer_id": transfer["id"], "tank_id": tank_id,
            "ship_name": ship_name, "planned_quantity": quantity,
            "status": status, "failed_checks": failures,
            "progress": self._progress_snapshot(item_id),
        })
        return transfer

    def list_transfers(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.lightering_progress(item_id)["transfers"]

    def recheck_transfer(self, item_id: int, transfer_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_ROLES)
        actor = require_text(actor, "actor", 100)
        transfer = self.repository.get_transfer(item_id, transfer_id)
        if transfer["status"] != "pending":
            raise ConflictError("仅待处理作业可重新核对")
        quantity = require_number(payload.get("quantity", transfer["planned_quantity"]),
                                  "quantity", 0.000001)
        ground = require_number(payload.get("ground_resistance"), "ground_resistance")
        wind = require_number(payload.get("wind_speed"), "wind_speed")
        tank = self.repository.get_tank(item_id, transfer["tank_id"])
        remaining = tank["capacity"] - self.repository.tank_extracted(tank["id"])
        failures = prestart_failures(quantity, remaining, ground, wind)
        status = "pending" if failures else "open"
        self.repository.set_transfer_recheck(transfer_id, quantity, status, failures)
        updated = self.repository.get_transfer(item_id, transfer_id)
        self.repository.append_audit("transfer_recheck", ENTITY, item_id, actor, {
            "transfer_id": transfer_id, "status": status, "failed_checks": failures,
            "progress": self._progress_snapshot(item_id),
        })
        return updated

    def record_quantities(self, item_id: int, transfer_id: int,
                          payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, TRANSFER_ROLES)
        actor = require_text(actor, "actor", 100)
        transfer = self.repository.get_transfer(item_id, transfer_id)
        if transfer["status"] != "open":
            raise ConflictError("仅进行中的作业可录入计量")
        ship_q = require_number(payload.get("ship_quantity"), "ship_quantity")
        shore_q = require_number(payload.get("shore_quantity"), "shore_quantity")
        review = transfer_needs_review(ship_q, shore_q)
        status = "review" if review else "confirmed"
        tank = self.repository.get_tank(item_id, transfer["tank_id"])
        self.repository.set_transfer_quantities(
            transfer_id, ship_q, shore_q, status, actor, tank["plan_version"],
            None if review else actor)
        updated = self.repository.get_transfer(item_id, transfer_id)
        self.repository.append_audit("transfer_quantities", ENTITY, item_id, actor, {
            "transfer_id": transfer_id, "ship_quantity": ship_q,
            "shore_quantity": shore_q,
            "difference_pct": round(discrepancy_pct(ship_q, shore_q), 4),
            "status": status, "progress": self._progress_snapshot(item_id),
        })
        return updated

    def confirm_transfer(self, item_id: int, transfer_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        transfer = self.repository.get_transfer(item_id, transfer_id)
        if transfer["status"] not in ("review", "void"):
            raise ConflictError("仅待核对或已作废的作业可确认")
        ticket = require_text(payload.get("measurement_ticket"), "measurement_ticket", 100)
        if transfer["quantities_by"] == actor:
            raise PermissionDenied("计量确认须由另一人完成")
        tank = self.repository.get_tank(item_id, transfer["tank_id"])
        self.repository.set_transfer_confirmed(transfer_id, ticket,
                                               tank["plan_version"], actor)
        updated = self.repository.get_transfer(item_id, transfer_id)
        self.repository.append_audit("transfer_confirm", ENTITY, item_id, actor, {
            "transfer_id": transfer_id, "measurement_ticket": ticket,
            "plan_version": tank["plan_version"],
            "progress": self._progress_snapshot(item_id),
        })
        return updated

    def lightering_progress(self, item_id: int) -> Dict[str, Any]:
        tanks = [self._tank_view(tank) for tank in self.repository.list_tanks(item_id)]
        tank_names = {tank["id"]: tank["name"] for tank in tanks}
        transfers = self.repository.list_transfers(item_id)
        by_status = {status: 0 for status in TRANSFER_STATUSES}
        by_ship: Dict[str, Dict[str, float]] = {}
        views = []
        ship_total = 0.0
        shore_total = 0.0
        for transfer in transfers:
            by_status[transfer["status"]] += 1
            ship_q = transfer["ship_quantity"] or 0.0
            shore_q = transfer["shore_quantity"] or 0.0
            ship_total += ship_q
            shore_total += shore_q
            ship = by_ship.setdefault(transfer["ship_name"],
                                      {"ship_quantity": 0.0, "shore_quantity": 0.0})
            ship["ship_quantity"] = round(ship["ship_quantity"] + ship_q, 6)
            ship["shore_quantity"] = round(ship["shore_quantity"] + shore_q, 6)
            view = dict(transfer)
            view["tank_name"] = tank_names.get(transfer["tank_id"])
            view["difference_pct"] = (
                round(discrepancy_pct(ship_q, shore_q), 4)
                if transfer["ship_quantity"] is not None else None)
            views.append(view)
        return {
            "tanks": tanks,
            "transfers": views,
            "ship_total": round(ship_total, 6),
            "shore_total": round(shore_total, 6),
            "difference": round(ship_total - shore_total, 6),
            "difference_pct": round(discrepancy_pct(ship_total, shore_total), 4),
            "by_status": by_status,
            "by_ship": by_ship,
            "unsettled_count": sum(by_status[s] for s in TANK_BUSY_STATUSES),
            "unresolved_differences": self._difference_gaps(item_id),
        }

    def _tank_view(self, tank: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(tank)
        extracted = self.repository.tank_extracted(tank["id"])
        result["extracted"] = round(extracted, 6)
        result["remaining"] = round(tank["capacity"] - extracted, 6)
        busy = self.repository.tank_busy_transfer(tank["id"])
        result["unsettled_transfer_id"] = busy["id"] if busy else None
        return result

    def _difference_gaps(self, item_id: int) -> list:
        gaps = []
        for transfer in self.repository.unresolved_differences(item_id):
            gaps.append({
                "transfer_id": transfer["id"],
                "tank_id": transfer["tank_id"],
                "tank_name": transfer.get("tank_name"),
                "ship_name": transfer["ship_name"],
                "status": transfer["status"],
                "ship_quantity": transfer["ship_quantity"],
                "shore_quantity": transfer["shore_quantity"],
                "difference_pct": round(discrepancy_pct(
                    transfer["ship_quantity"] or 0.0,
                    transfer["shore_quantity"] or 0.0), 4),
                "missing": ("补齐计量单并由另一人确认" if transfer["status"] == "review"
                            else "舱容计划已修订，原确认作废，需重新确认"),
            })
        return gaps

    def _progress_snapshot(self, item_id: int) -> Dict[str, Any]:
        progress = self.lightering_progress(item_id)
        return {
            "ship_total": progress["ship_total"],
            "shore_total": progress["shore_total"],
            "difference_pct": progress["difference_pct"],
            "by_status": progress["by_status"],
            "unresolved_count": len(progress["unresolved_differences"]),
        }

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
