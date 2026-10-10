#!/usr/bin/env python3
"""Capture one dispatch run into data/run.json for the public replay page.

Reads only - no keys, no writes, nothing authenticated. Run it after a full run:

    1. python3 exception_agent.py                      # copilot proposals
    2. approve one proposal as the supervisor
    3. python3 squad_activity.py --support APX-2610149
       python3 squad_activity.py --compliance

    python3 export_run.py                       # http://localhost:8090
    DISPATCH_BASE_URL=... python3 export_run.py -o data/run.json

Everything that is not demo content is stripped: header-name logs, client IPs
and any host name. Only synthetic shipment data reaches the file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("DISPATCH_BASE_URL", "http://localhost:8090").rstrip("/")

# Keys we never let into the published file.
DROP_KEYS = {"client_ip", "headers", "host", "path"}
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def get(path):
    request = urllib.request.Request(BASE + path)
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise SystemExit("GET %s -> HTTP %s" % (path, exc.code))


def scrub(value):
    """Drop non-demo fields and blank out any address that slipped through."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in DROP_KEYS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return IPV4.sub("[address]", value)
    return value


def enrich(exceptions):
    """Add the shipment fields the board shows but /exceptions does not carry."""
    for e in exceptions:
        sid = e.get("shipment_id")
        if not sid:
            continue
        try:
            ship = get("/shipments/" + urllib.parse.quote(sid))
        except SystemExit:
            continue
        for key in ("origin_terminal", "dest_terminal", "est_margin_usd", "miles", "percent_complete"):
            if key in ship:
                e[key] = ship[key]
    return exceptions


def main():
    parser = argparse.ArgumentParser(description="Capture a run for the replay page")
    parser.add_argument("-o", "--out", default=os.path.join("data", "run.json"))
    parser.add_argument("--snapshot", metavar="PATH",
                        help="write the currently open loads and exit - run this BEFORE the run")
    parser.add_argument("--start", metavar="PATH",
                        help="use this snapshot as the starting board, so the replay opens at full count")
    args = parser.parse_args()

    if args.snapshot:
        snapshot = [scrub(e) for e in enrich(get("/exceptions").get("exceptions", []))]
        with open(args.snapshot, "w", encoding="utf-8") as handle:
            json.dump({"exceptions": snapshot}, handle, indent=1)
            handle.write("\n")
        print("snapshot: %d open loads -> %s" % (len(snapshot), args.snapshot))
        return

    if args.start and os.path.exists(args.start):
        with open(args.start, encoding="utf-8") as handle:
            start_exceptions = json.load(handle).get("exceptions", [])
    else:
        start_exceptions = [scrub(e) for e in enrich(get("/exceptions").get("exceptions", []))]
        print("note: no --start snapshot, so the board opens with only the loads still open now")

    proposals = get("/reroutes").get("proposals", [])
    updates = get("/updates").get("updates", [])
    audit = get("/audit").get("audit", [])
    drivers = get("/drivers/available").get("drivers", [])

    # header-name probes are diagnostics, never demo content
    events = [a for a in audit if a.get("event") != "request.headers"]
    events = [scrub(e) for e in events]

    # oldest first, so the page can replay it forwards
    events.sort(key=lambda e: str(e.get("ts_utc", "")))

    driver_map = {
        d.get("driver_id"): {
            "name": d.get("name"),
            "equipment": d.get("equipment"),
            "endorsements": d.get("endorsements"),
            "current_terminal": d.get("current_terminal"),
        }
        for d in drivers if d.get("driver_id")
    }
    # names for drivers who left the available list after being assigned
    for u in updates:
        if u.get("driver_id") and u["driver_id"] not in driver_map:
            driver_map[u["driver_id"]] = {"name": u.get("driver_name")}

    run = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "source": "Recorded lab run - synthetic ApexTrans dispatches",
        "start": {"loads_open": len(start_exceptions)},
        "exceptions": start_exceptions,
        "proposals": [scrub(p) for p in proposals],
        "updates": [scrub(u) for u in updates],
        "events": events,
        "drivers": driver_map,
    }

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(run, handle, indent=1, sort_keys=True)
        handle.write("\n")

    kinds = {}
    for e in events:
        kinds[e.get("event")] = kinds.get(e.get("event"), 0) + 1
    print("wrote %s" % args.out)
    print("  loads open at start : %d" % len(start_exceptions))
    print("  proposals           : %d" % len(proposals))
    print("  replay events       : %d %s" % (len(events), kinds))
    print("  bytes               : %d" % os.path.getsize(args.out))
    leaked = IPV4.findall(open(args.out, encoding="utf-8").read())
    print("  stray addresses     : %d" % len(leaked))


if __name__ == "__main__":
    main()
