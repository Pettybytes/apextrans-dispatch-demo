#!/usr/bin/env python3
"""Acceptance check for apex-dispatch.

Runs the demo's acceptance criteria against a *fresh* service (restart the
service first so in-memory state and /audit are clean).

    python3 app.py &            # or the container
    python3 check_acceptance.py

Exits non-zero if any criterion fails.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("DISPATCH_BASE_URL", "http://localhost:8090")
NON_HAZMAT_DRIVER = os.environ.get("NON_HAZMAT_DRIVER", "DRV-1003")  # Flatbed, endorsements=None
AUTO_SHIPMENT = os.environ.get("AUTO_SHIPMENT", "APX-2610161")        # Reefer, nearest terminal PHX
AUTO_DRIVER = os.environ.get("AUTO_DRIVER", "DRV-1009")               # Reefer-certified, based at PHX
# Held fixture deliberately uses a *hard* rule (equipment mismatch: Flatbed load,
# Dry Van driver) so it never depends on the tunable speed/HOS or cost constant.
HELD_SHIPMENT = os.environ.get("HELD_SHIPMENT", "APX-2610145")        # Flatbed, nearest terminal KCK
HELD_DRIVER = os.environ.get("HELD_DRIVER", "DRV-1001")               # Dry Van 53ft -> equipment mismatch

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" - " + detail) if detail else ""))
    return ok


def call(method, path, body=None, expect=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, payload = response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, json.loads(exc.read() or b"{}")
    if expect is not None and status != expect:
        raise AssertionError("%s %s -> %s (expected %s): %s" % (method, path, status, expect, payload))
    return status, payload


def main():
    print("apex-dispatch acceptance check against %s\n" % BASE)

    _, health = call("GET", "/health", expect=200)
    check("health returns ok", health.get("ok") is True, json.dumps(health))

    _, exceptions = call("GET", "/exceptions", expect=200)
    check("8 open exceptions", exceptions.get("count") == 8, "count=%s" % exceptions.get("count"))
    hazmat = [e for e in exceptions["exceptions"] if e["shipment_id"] == "APX-2610141"]
    check(
        "hazmat exception present",
        bool(hazmat) and hazmat[0]["load_type"] == "Hazmat",
        hazmat[0]["exception_note"] if hazmat else "missing",
    )

    # --- Hazmat gate: a non-Hazmat driver must be rejected -----------------
    _, proposal = call(
        "POST",
        "/reroutes",
        {
            "shipment_id": "APX-2610141",
            "new_driver_id": NON_HAZMAT_DRIVER,
            "reason": "acceptance: hazmat gate",
            "requested_by": "check_acceptance",
        },
        expect=201,
    )
    joined = " ".join(proposal.get("conflicts", []))
    check(
        "hazmat load rejects non-Hazmat driver",
        proposal["decision"] == "pending_human" and "Hazmat endorsement" in joined,
        joined or "(no conflicts)",
    )

    # --- auto-apply path ---------------------------------------------------
    _, auto = call(
        "POST",
        "/reroutes",
        {
            "shipment_id": AUTO_SHIPMENT,
            "new_driver_id": AUTO_DRIVER,
            "reason": "acceptance: clean reroute",
            "requested_by": "check_acceptance",
        },
        expect=201,
    )
    check(
        "at least one reroute auto-applies",
        auto["decision"] == "auto_applied" and not auto["conflicts"] and auto["reroute_cost_usd"] <= 500,
        "%s -> %s cost=$%.2f conflicts=%s" % (auto["shipment_id"], auto["new_driver_id"], auto["reroute_cost_usd"], auto["conflicts"]),
    )
    _, shipment = call("GET", "/shipments/%s" % AUTO_SHIPMENT, expect=200)
    check(
        "auto-applied reroute updates the shipment",
        shipment["status"] == "Rerouted" and shipment["driver_id"] == AUTO_DRIVER and shipment["eta_utc"] == auto["new_eta_utc"],
        "status=%s driver=%s eta=%s" % (shipment["status"], shipment["driver_id"], shipment["eta_utc"]),
    )

    # --- held path + approval ---------------------------------------------
    _, held = call(
        "POST",
        "/reroutes",
        {
            "shipment_id": HELD_SHIPMENT,
            "new_driver_id": HELD_DRIVER,
            "reason": "acceptance: expect hold",
            "requested_by": "check_acceptance",
        },
        expect=201,
    )
    check(
        "at least one reroute is held for a human",
        held["decision"] == "pending_human" and bool(held["conflicts"]),
        "; ".join(held["conflicts"]),
    )
    _, before = call("GET", "/shipments/%s" % HELD_SHIPMENT, expect=200)
    check("held reroute did not change the shipment", before["driver_id"] != HELD_DRIVER, "driver still %s" % before["driver_id"])

    _, pending = call("GET", "/reroutes?status=pending", expect=200)
    pending_ids = [p["proposal_id"] for p in pending["proposals"]]
    check("held proposal appears in /reroutes?status=pending", held["proposal_id"] in pending_ids, str(pending_ids))

    _, applied = call(
        "POST",
        "/reroutes/%s/approve" % held["proposal_id"],
        {"approver": "dispatch-supervisor", "note": "acceptance: approved"},
        expect=200,
    )
    check("held proposal applies after approve", applied.get("status") == "applied", "status=%s" % applied.get("status"))
    _, after = call("GET", "/shipments/%s" % HELD_SHIPMENT, expect=200)
    check(
        "approval updates the shipment",
        after["status"] == "Rerouted" and after["driver_id"] == HELD_DRIVER,
        "status=%s driver=%s" % (after["status"], after["driver_id"]),
    )

    _, updates = call("GET", "/updates", expect=200)
    messages = [u["message"] for u in updates["updates"]]
    check(
        "/updates shows both applied changes, newest first",
        len(messages) >= 2
        and AUTO_SHIPMENT in messages[-1]
        and HELD_SHIPMENT in messages[0],
        " | ".join(messages),
    )
    _, audit = call("GET", "/audit", expect=200)
    events = [e["event"] for e in audit["audit"]]
    check(
        "/audit records proposal, approval and applied change",
        {"proposal.created", "proposal.approved", "shipment.rerouted"}.issubset(set(events)),
        " | ".join(events),
    )

    _, approval_reject = call(
        "POST",
        "/reroutes/%s/approve" % held["proposal_id"],
        {"approver": "dispatch-supervisor"},
        expect=409,
    )
    check("re-approving an applied proposal is rejected", True, approval_reject.get("error", ""))

    _, missing = call("GET", "/shipments/APX-DOES-NOT-EXIST", expect=404)
    check("unknown shipment returns 404", "error" in missing, missing.get("error", ""))

    failed = [name for name, ok, _ in results if not ok]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    print("ACCEPTANCE OK")
    return 0


if __name__ == "__main__":
    for attempt in range(20):
        try:
            urllib.request.urlopen(BASE + "/health", timeout=2)
            break
        except Exception:
            time.sleep(0.5)
    sys.exit(main())
