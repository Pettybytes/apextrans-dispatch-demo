"""ApexTrans dispatch: shipment data + dispatch business rules.

Standard library only. State lives in memory, loaded from the CSVs in ``data/``
at start-up. Every proposal, decision and approval is appended to
``data/audit.jsonl``.

The rules in this module are the business guardrails the dispatch service
exposes over REST (Kong AI Gateway turns those endpoints into MCP tools later).
"""

from __future__ import annotations

import csv
import json
import math
import os
import threading
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DISPATCH_DATA_DIR") or os.path.join(HERE, "data")
AUDIT_PATH = os.path.join(DATA_DIR, "audit.jsonl")

# ---- Tunables (documented in approval_limits.md) --------------------------
SPEED_MPH = float(os.environ.get("DISPATCH_SPEED_MPH", "55"))
CIRCUITY_FACTOR = float(os.environ.get("DISPATCH_CIRCUITY_FACTOR", "1.2"))
DEADHEAD_ADDON_PER_MILE = float(os.environ.get("DISPATCH_DEADHEAD_ADDON", "0.95"))
AUTO_APPROVE_LIMIT_USD = float(os.environ.get("DISPATCH_AUTO_LIMIT_USD", "500"))
EARTH_RADIUS_MILES = 3958.8

# Load type -> required trailer/equipment.
EQUIPMENT_FOR_LOAD = {
    "Reefer": "Reefer 53ft",
    "Flatbed": "Flatbed 48ft",
    "Dry Van": "Dry Van 53ft",
    "Hazmat": "Dry Van 53ft",
}

# Load type -> required endorsement.
ENDORSEMENT_FOR_LOAD = {
    "Reefer": "Reefer-certified",
    "Hazmat": "Hazmat",
}

# Only out-of-service-grade defects block a dispatch. Anything else is a note.
BLOCKING_DEFECT_MARKERS = ("brake", "tire", "light", "reefer", "abs", "air bag")


def is_blocking_defect(defect):
    text = (defect or "").lower()
    return any(marker in text for marker in BLOCKING_DEFECT_MARKERS)

TERMINAL_FIELDS = ("terminal_code", "city", "state", "lat", "lon")
SHIPMENT_KEY = "shipment_id"
DRIVER_KEY = "driver_id"


def _f(value, default=0.0):
    """Parse a CSV numeric field, tolerating blanks."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_utc(value):
    """Parse the CSV UTC timestamp format (2026-10-15T09:05Z)."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(dt):
    """Render a UTC datetime the way the dispatch data uses it."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def now_utc():
    return datetime.now(timezone.utc)


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


class DispatchError(Exception):
    """Raised for a client-visible 4xx condition."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


class Dispatch:
    """In-memory dispatch state plus the reroute rule engine."""

    def __init__(self, data_dir=DATA_DIR):
        self.lock = threading.RLock()
        self.data_dir = data_dir
        self.shipments = {}
        self.drivers = {}
        self.terminals = {}
        self.trucks = {}
        self.proposals = {}
        self.proposal_order = []
        self.proposal_seq = 0
        self.updates = []          # newest last internally, served newest first
        self.audit = []            # newest last internally, served newest first
        self.held_drivers = set()  # drivers holding a pending proposal
        self._last_header_names = None
        self.load()

    # ---- loading ----------------------------------------------------------
    def load(self):
        self.terminals = {
            row[TERMINAL_FIELDS[0]]: row
            for row in self._read_csv("terminals.csv")
            if row.get("terminal_code")
        }
        self.shipments = {
            row[SHIPMENT_KEY]: row
            for row in self._read_csv("shipments.csv")
            if row.get(SHIPMENT_KEY)
        }
        self.drivers = {
            row[DRIVER_KEY]: row
            for row in self._read_csv("drivers.csv")
            if row.get(DRIVER_KEY)
        }
        try:
            self.trucks = {
                row["truck_id"]: row
                for row in self._read_csv("trucks.csv")
                if row.get("truck_id")
            }
        except OSError:
            self.trucks = {}

    def _read_csv(self, name):
        path = os.path.join(self.data_dir, name)
        with open(path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    # ---- state reset ------------------------------------------------------
    def reset_state(self):
        """Reload the CSVs into memory - the same result as a container restart.

        Everything in flight is dropped, then the reset itself is recorded so
        the board can show who did it.
        """
        with self.lock:
            self.proposals = {}
            self.proposal_order = []
            self.proposal_seq = 0
            self.updates = []
            self.audit = []
            self.held_drivers = set()
            self._last_header_names = None
            self.shipments = {}
            self.drivers = {}
            self.terminals = {}
            self.trucks = {}
            self.load()
            self._audit("admin.reset", message="demo reset by dispatcher (local)")
        return {"ok": True, "loads_open": len(self.list_exceptions())}

    # ---- lookups ----------------------------------------------------------
    def shipment(self, shipment_id):
        row = self.shipments.get(shipment_id)
        if row is None:
            raise DispatchError("unknown shipment_id %r" % shipment_id, 404)
        return row

    def driver(self, driver_id):
        row = self.drivers.get(driver_id)
        if row is None:
            raise DispatchError("unknown driver_id %r" % driver_id, 404)
        return row

    def _terminal_coords(self, code):
        row = self.terminals.get(code)
        if row is None:
            raise DispatchError("unknown terminal %r" % code, 404)
        return _f(row["lat"]), _f(row["lon"])

    def nearest_terminal_code(self, shipment):
        """Prefer the curated column; fall back to a great-circle search."""
        code = (shipment.get("nearest_terminal") or "").strip()
        if code in self.terminals:
            return code
        lat, lon = _f(shipment.get("current_lat")), _f(shipment.get("current_lon"))
        best, best_km = None, None
        for terminal_code, row in self.terminals.items():
            dist = haversine_miles(lat, lon, _f(row["lat"]), _f(row["lon"]))
            if best_km is None or dist < best_km:
                best, best_km = terminal_code, dist
        return best

    # ---- derived quantities ----------------------------------------------
    def remaining_miles(self, shipment):
        """Miles still to run on the load, from percentage complete."""
        total = _f(shipment.get("miles"))
        pct = _f(shipment.get("percent_complete"))
        return round(total * max(0.0, 100.0 - pct) / 100.0, 1)

    def deadhead_miles(self, driver, shipment):
        """Estimated road miles from the driver's current terminal to the
        shipment's nearest terminal (great-circle x circuity factor)."""
        lat1, lon1 = self._terminal_coords(driver["current_terminal"])
        lat2, lon2 = self._terminal_coords(self.nearest_terminal_code(shipment))
        straight = haversine_miles(lat1, lon1, lat2, lon2)
        return int(round(straight * CIRCUITY_FACTOR))

    def reroute_cost_usd(self, shipment, deadhead_miles):
        fuel = _f(shipment.get("fuel_cost_per_mile_usd"))
        return round(deadhead_miles * (fuel + DEADHEAD_ADDON_PER_MILE), 2)

    def transit_hours(self, shipment, deadhead_miles):
        return round((deadhead_miles + self.remaining_miles(shipment)) / SPEED_MPH, 2)

    # ---- hours of service ------------------------------------------------
    def driver_hours(self, driver):
        """(drive_left, window_left, cycle_left) from the driver's HOS record."""
        return (
            max(0.0, 11.0 - _f(driver.get("drive_hours_used_today"))),
            max(0.0, 14.0 - _f(driver.get("on_duty_window_used"))),
            max(0.0, 70.0 - _f(driver.get("cycle_hours_used_8d"))),
        )

    def available_hours(self, driver):
        """Legal driving hours available right now: the binding limit of the three."""
        return min(self.driver_hours(driver))

    # ---- equipment -------------------------------------------------------
    def truck_for(self, driver):
        """The tractor assigned to a driver (empty record when unknown)."""
        return self.trucks.get((driver.get("truck_id") or "").strip()) or {}

    def truck_fields(self, driver):
        truck = self.truck_for(driver)
        return {
            "truck_id": truck.get("truck_id", ""),
            "truck_status": truck.get("status", ""),
            "open_defect": truck.get("open_defect", ""),
        }

    def new_eta(self, shipment, deadhead_miles, extra_hours=0.0):
        """Projected arrival, plus any break or overnight-reset time.

        Anchored at max(now, pickup) so a run before the pickup window still
        produces a sensible in-window estimate.
        """
        reference = now_utc()
        pickup = parse_utc(shipment.get("pickup_utc"))
        if pickup and pickup > reference:
            reference = pickup
        hours = (deadhead_miles + self.remaining_miles(shipment)) / SPEED_MPH + extra_hours
        return reference + timedelta(hours=hours)

    # ---- rule engine ------------------------------------------------------
    def evaluate_reroute(self, shipment, driver):
        """Return (conflicts, notes, deadhead_miles, cost_usd, new_eta_datetime).

        ``conflicts`` are hard: any one of them holds the reroute for a human.
        ``notes`` only move the ETA (a break, or an overnight reset), so they do
        not hold a reroute on their own - they just make a late arrival likelier.
        """
        conflicts = []
        notes = []
        deadhead = self.deadhead_miles(driver, shipment)
        cost = self.reroute_cost_usd(shipment, deadhead)
        needed = (deadhead + self.remaining_miles(shipment)) / SPEED_MPH

        load_type = (shipment.get("load_type") or "").strip()
        required_equipment = EQUIPMENT_FOR_LOAD.get(load_type)
        driver_equipment = (driver.get("equipment") or "").strip()
        if required_equipment and driver_equipment != required_equipment:
            conflicts.append(
                "equipment mismatch: %s load needs %s, driver has %s"
                % (load_type, required_equipment, driver_equipment or "none")
            )

        endorsements = (driver.get("endorsements") or "").strip()
        held = [e.strip() for e in endorsements.split(",") if e.strip() and e.strip() != "None"]
        required_endorsement = ENDORSEMENT_FOR_LOAD.get(load_type)
        if required_endorsement and required_endorsement not in held:
            conflicts.append(
                "%s load requires %s endorsement (driver endorsements: %s)"
                % (load_type, required_endorsement, endorsements or "none")
            )

        status = (driver.get("status") or "").strip()
        if status != "Available":
            conflicts.append("%s is not Available (status=%s)" % (driver[DRIVER_KEY], status))
        elif driver[DRIVER_KEY] in self.held_drivers:
            conflicts.append(
                "%s already holds a pending proposal (%s)"
                % (driver[DRIVER_KEY], self._pending_proposal_for(driver[DRIVER_KEY]))
            )

        # --- hours of service: three independent limits -----------------------
        drive_left, window_left, cycle_left = self.driver_hours(driver)
        if drive_left < needed:
            conflicts.append(
                "Drive limit: needs %.1f h, driver has %.1f h left today (11-hour rule)"
                % (needed, drive_left)
            )
        if window_left < needed:
            conflicts.append(
                "On-duty window: 14-hour window closes in %.1f h" % window_left
            )
        if cycle_left < needed:
            conflicts.append(
                "Weekly cycle: %.0f of 70 hours used" % _f(driver.get("cycle_hours_used_8d"))
            )

        # soft: these only push the ETA out
        extra_hours = 0.0
        since_break = _f(driver.get("hours_since_last_break"))
        if since_break >= 8.0:
            notes.append(
                "Break due: 30-minute break needed before driving (%.0f h since last break)"
                % since_break
            )
            extra_hours += 0.5
        if needed > self.available_hours(driver):
            notes.append("needs an overnight reset")
            extra_hours += 10.0

        # --- tractor condition -------------------------------------------------
        truck = self.truck_for(driver)
        if truck:
            defect = (truck.get("open_defect") or "").strip()
            if (truck.get("status") or "").strip() == "Out of service":
                conflicts.append("Truck out of service: %s" % (defect or "no defect recorded"))
            if defect and is_blocking_defect(defect):
                if load_type == "Reefer" and "reefer" in defect.lower():
                    conflicts.append(
                        "Reefer unit fault on truck %s: %s. Trailer cannot hold temperature for this load"
                        % (truck.get("truck_id"), defect)
                    )
                else:
                    conflicts.append(
                        "Open defect on truck %s: %s. Must be repaired before dispatch"
                        % (truck.get("truck_id"), defect)
                    )
            elif defect:
                notes.append(
                    "Minor defect on truck %s: %s (does not block dispatch)"
                    % (truck.get("truck_id"), defect)
                )

        eta = self.new_eta(shipment, deadhead, extra_hours)

        due = parse_utc(shipment.get("delivery_due_utc"))
        if due and eta > due:
            conflicts.append(
                "new ETA %s still misses delivery due %s"
                % (iso_z(eta), shipment.get("delivery_due_utc"))
            )

        if (shipment.get("priority") or "").strip().lower() == "critical":
            conflicts.append("Critical priority shipment requires human approval")

        if cost > AUTO_APPROVE_LIMIT_USD:
            conflicts.append(
                "cost $%.0f over $%.0f limit" % (cost, AUTO_APPROVE_LIMIT_USD)
            )

        return conflicts, notes, deadhead, cost, eta

    def _pending_proposal_for(self, driver_id):
        for proposal_id in reversed(self.proposal_order):
            proposal = self.proposals[proposal_id]
            if proposal["new_driver_id"] == driver_id and proposal["decision"] == "pending_human":
                return proposal_id
        return "unknown"

    # ---- proposals --------------------------------------------------------
    def create_reroute(self, shipment_id, new_driver_id, reason, requested_by):
        with self.lock:
            shipment = self.shipment(shipment_id)
            driver = self.driver(new_driver_id)
            conflicts, notes, deadhead, cost, eta = self.evaluate_reroute(shipment, driver)

            self.proposal_seq += 1
            proposal_id = "RR-%04d" % self.proposal_seq
            decision = "auto_applied" if not conflicts else "pending_human"
            proposal = {
                "proposal_id": proposal_id,
                "shipment_id": shipment_id,
                "new_driver_id": new_driver_id,
                "deadhead_miles": deadhead,
                "reroute_cost_usd": cost,
                "new_eta_utc": iso_z(eta),
                "conflicts": conflicts,
                "notes": notes,
                "decision": decision,
                "status": "pending" if decision == "pending_human" else "applied",
                "reason": reason,
                "requested_by": requested_by,
                "created_utc": iso_z(now_utc()),
            }
            self.proposals[proposal_id] = proposal
            self.proposal_order.append(proposal_id)

            self._audit(
                "proposal.created",
                proposal_id=proposal_id,
                shipment_id=shipment_id,
                new_driver_id=new_driver_id,
                decision=decision,
                conflicts=conflicts,
                reason=reason,
                actor=requested_by or "dispatcher",
            )

            if decision == "pending_human":
                self.held_drivers.add(new_driver_id)
            else:
                self._apply(proposal, actor=requested_by or "dispatcher", note="auto-applied (no conflicts, within limit)")
            return dict(proposal)

    def approve(self, proposal_id, approver, note):
        with self.lock:
            proposal = self.proposals.get(proposal_id)
            if proposal is None:
                raise DispatchError("unknown proposal_id %r" % proposal_id, 404)
            if proposal["decision"] == "auto_applied":
                raise DispatchError("%s was already auto-applied" % proposal_id, 409)
            if proposal.get("status") == "applied":
                raise DispatchError("%s was already approved" % proposal_id, 409)

            self._audit(
                "proposal.approved",
                proposal_id=proposal_id,
                shipment_id=proposal["shipment_id"],
                approver=approver,
                note=note,
            )
            self._apply(proposal, actor=approver, note=note)
            return dict(proposal)

    def _apply(self, proposal, actor, note):
        shipment = self.shipments[proposal["shipment_id"]]
        driver = self.drivers[proposal["new_driver_id"]]

        shipment["driver_id"] = driver[DRIVER_KEY]
        shipment["status"] = "Rerouted"
        shipment["eta_utc"] = proposal["new_eta_utc"]

        driver["status"] = "On Load"
        self.held_drivers.discard(driver[DRIVER_KEY])

        proposal["status"] = "applied"
        proposal["applied_utc"] = iso_z(now_utc())
        proposal["applied_by"] = actor

        eta_hhmm = proposal["new_eta_utc"][11:16]
        message = "%s reassigned to %s (%s). New ETA %s UTC. Customer notified." % (
            proposal["shipment_id"],
            driver["name"],
            driver[DRIVER_KEY],
            eta_hhmm,
        )
        entry = {
            "ts_utc": iso_z(now_utc()),
            "shipment_id": proposal["shipment_id"],
            "proposal_id": proposal["proposal_id"],
            "driver_id": driver[DRIVER_KEY],
            "driver_name": driver["name"],
            "actor": actor or "dispatcher",
            "new_eta_utc": proposal["new_eta_utc"],
            "message": message,
        }
        self.updates.append(entry)
        self._audit(
            "shipment.rerouted",
            proposal_id=proposal["proposal_id"],
            shipment_id=proposal["shipment_id"],
            new_driver_id=driver[DRIVER_KEY],
            new_eta_utc=proposal["new_eta_utc"],
            actor=actor,
            note=note,
        )

    # ---- reads ------------------------------------------------------------
    def list_exceptions(self):
        rows = []
        for shipment in self.shipments.values():
            if (shipment.get("status") or "").strip() != "Exception":
                continue
            due = parse_utc(shipment.get("delivery_due_utc"))
            eta = parse_utc(shipment.get("eta_utc"))
            late_hours = round((eta - due).total_seconds() / 3600.0, 2) if (due and eta) else None
            rows.append(
                {
                    "shipment_id": shipment["shipment_id"],
                    "customer": shipment.get("customer"),
                    "load_type": shipment.get("load_type"),
                    "equipment": shipment.get("equipment"),
                    "lane": "%s -> %s" % (shipment.get("origin_city"), shipment.get("dest_city")),
                    "current_location": shipment.get("current_location"),
                    "nearest_terminal": self.nearest_terminal_code(shipment),
                    "exception_type": shipment.get("exception_type"),
                    "exception_note": shipment.get("exception_note"),
                    "priority": shipment.get("priority"),
                    "delivery_due_utc": shipment.get("delivery_due_utc"),
                    "eta_utc": shipment.get("eta_utc"),
                    "late_vs_due_hours": late_hours,
                    "on_time": (late_hours is not None and late_hours <= 0),
                    "miles": _f(shipment.get("miles")),
                    "remaining_miles": self.remaining_miles(shipment),
                    "percent_complete": _f(shipment.get("percent_complete")),
                    "hazmat": shipment.get("hazmat"),
                    "driver_id": shipment.get("driver_id"),
                }
            )
        rows.sort(key=lambda row: row["shipment_id"])
        return rows

    def available_drivers(self, terminal=None, equipment=None, endorsement=None, shipment_id=None):
        shipment = self.shipment(shipment_id) if shipment_id else None
        out = []
        for driver in self.drivers.values():
            if (driver.get("status") or "").strip() != "Available":
                continue
            if terminal and driver.get("current_terminal") != terminal:
                continue
            if equipment and driver.get("equipment") != equipment:
                continue
            if endorsement and endorsement not in [
                e.strip() for e in (driver.get("endorsements") or "").split(",")
            ]:
                continue
            record = {
                "driver_id": driver[DRIVER_KEY],
                "name": driver.get("name"),
                "status": driver.get("status"),
                "home_terminal": driver.get("home_terminal"),
                "current_terminal": driver.get("current_terminal"),
                "current_city": driver.get("current_city"),
                "hours_left": round(self.available_hours(driver), 1),
                "drive_hours_left": round(self.driver_hours(driver)[0], 1),
                "window_hours_left": round(self.driver_hours(driver)[1], 1),
                "cycle_hours_left": round(self.driver_hours(driver)[2], 1),
                "drive_hours_used_today": _f(driver.get("drive_hours_used_today")),
                "on_duty_window_used": _f(driver.get("on_duty_window_used")),
                "hours_since_last_break": _f(driver.get("hours_since_last_break")),
                "cycle_hours_used_8d": _f(driver.get("cycle_hours_used_8d")),
                "endorsements": driver.get("endorsements"),
                "equipment": driver.get("equipment"),
                "safety_score": driver.get("safety_score"),
                "holds_pending_proposal": driver[DRIVER_KEY] in self.held_drivers,
            }
            record.update(self.truck_fields(driver))
            if shipment is not None:
                deadhead = self.deadhead_miles(driver, shipment)
                record["deadhead_miles"] = deadhead
                record["reroute_cost_usd"] = self.reroute_cost_usd(shipment, deadhead)
                record["transit_hours_needed"] = self.transit_hours(shipment, deadhead)
                required_equipment = EQUIPMENT_FOR_LOAD.get((shipment.get("load_type") or "").strip())
                record["equipment_match"] = (
                    required_equipment is not None and driver.get("equipment") == required_equipment
                )
                required_endorsement = ENDORSEMENT_FOR_LOAD.get((shipment.get("load_type") or "").strip())
                record["endorsement_match"] = (
                    required_endorsement is None
                    or required_endorsement
                    in [e.strip() for e in (driver.get("endorsements") or "").split(",")]
                )
            out.append(record)
        out.sort(key=lambda row: (row.get("deadhead_miles", 0), row["driver_id"]))
        return out

    def shipment_view(self, shipment_id):
        shipment = self.shipment(shipment_id)
        record = dict(shipment)
        record["remaining_miles"] = self.remaining_miles(shipment)
        record["nearest_terminal"] = self.nearest_terminal_code(shipment)
        record.update(self.truck_fields(self.drivers.get(shipment.get("driver_id"), {})))
        return record

    def pending_proposals(self, status=None):
        rows = []
        for proposal_id in self.proposal_order:
            proposal = self.proposals[proposal_id]
            if status == "pending" and proposal["decision"] != "pending_human":
                continue
            if status == "pending" and proposal.get("status") == "applied":
                continue
            rows.append(dict(proposal))
        return list(reversed(rows))

    def updates_feed(self):
        return list(reversed(self.updates))

    def audit_log(self):
        return list(reversed(self.audit))

    def blocked_write(self, client_ip, method, path):
        """Record a write that arrived from outside the gateway."""
        return self._audit(
            "write.blocked",
            message="blocked direct write from %s" % (client_ip or "unknown"),
            client_ip=client_ip or "unknown",
            method=method,
            path=path,
        )

    def record_lookup(self, identity, what):
        """Attribute a read to the caller the gateway named. No request bodies."""
        return self._audit(
            "read.lookup",
            message="%s looked up %s" % (identity, what),
            identity=identity,
            subject=what,
        )

    def record_header_names(self, names):
        """Note the header NAMES a caller sent, once per new set.

        Names only - never values - so we can find out what Kong forwards
        without recording anything sensitive.
        """
        names = sorted(names or [])
        if names == self._last_header_names:
            return None
        self._last_header_names = names
        return self._audit("request.headers", message="header names: %s" % ", ".join(names), headers=names)

    # ---- audit ------------------------------------------------------------
    def _audit(self, event, **fields):
        record = {"ts_utc": iso_z(now_utc()), "event": event}
        record.update(fields)
        with self.lock:
            self.audit.append(record)
        try:
            os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
            with open(AUDIT_PATH, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            pass
        return record
