#!/usr/bin/env python3
"""Regenerate the ApexTrans demo dataset from the frozen seed corpus.

Deterministic: fixed seed, no clock reads, so the same run always writes the
same CSVs. Reads ``data/base/*.csv`` (the original 40 shipments, 44 drivers and
24 terminals) and writes ``data/shipments.csv``, ``data/drivers.csv``,
``data/terminals.csv`` and ``data/trucks.csv``.

Rows that carry a demo story beat are pinned by id (PINNED_*) and are never
regenerated:

  * APX-2610161 and APX-2610177 must auto-apply.
  * APX-2610149 must be held (estimated cost over the $500 limit).
  * APX-2610179 is a Flatbed whose closest candidate is a Dry Van driver, so the
    rules engine catches the equipment mismatch.

    python3 gen.py
"""

from __future__ import annotations

import csv
import math
import os
import random
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
BASE = os.path.join(DATA, "base")

SEED = 20261014
BASE_NOW = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)

TARGET_SHIPMENTS = 120
TARGET_DRIVERS = 120
TARGET_EXCEPTIONS = 12

# ---------------------------------------------------------------- new terminals
NEW_TERMINALS = [
    ("SAT", "San Antonio", "TX", 29.42, -98.49),
    ("MSY", "New Orleans", "LA", 29.95, -90.07),
    ("PDX", "Portland", "OR", 45.52, -122.68),
    ("BUF", "Buffalo", "NY", 42.89, -78.88),
    ("TUL", "Tulsa", "OK", 36.15, -95.99),
    ("ELP", "El Paso", "TX", 31.76, -106.49),
]

# ------------------------------------------------------------------ story pins
# Available drivers whose numbers the story beats depend on.
PINNED_HOS = {
    # driver_id: (drive_used_today, on_duty_window_used, hours_since_break, cycle_used_8d)
    "DRV-1001": (1.5, 4.0, 2.0, 32.0),   # beats: APX-2610177 auto-applies
    "DRV-1002": (6.0, 8.0, 3.0, 41.0),
    "DRV-1003": (1.5, 3.5, 1.0, 22.0),
    "DRV-1004": (9.0, 11.0, 9.0, 58.0),  # tight drive, break due
    "DRV-1005": (3.0, 5.0, 2.5, 26.0),
    "DRV-1006": (8.5, 10.5, 4.0, 63.0),  # drive + cycle nearly gone
    "DRV-1007": (7.5, 9.0, 8.5, 44.0),   # break due
    "DRV-1008": (4.0, 6.0, 3.0, 30.0),
    "DRV-1009": (2.0, 5.0, 2.0, 40.0),   # beats: APX-2610161 auto-applies
    "DRV-1010": (2.0, 4.0, 1.5, 18.0),   # beats: closest to APX-2610179 (Dry Van)
}

PINNED_TRUCKS = {
    # driver_id: (status, open_defect)
    "DRV-1001": ("In service", ""),                 # clean, so APX-2610177 auto-applies
    "DRV-1002": ("Out of service", "reefer unit fault"),
    "DRV-1003": ("In service", "cracked mirror"),   # minor: a note, never a conflict
    "DRV-1004": ("Out of service", "brake air leak"),
    "DRV-1005": ("In service", ""),
    "DRV-1006": ("Out of service", "lights inoperative"),
    "DRV-1007": ("Out of service", "tire tread below limit"),
    "DRV-1008": ("In service", ""),
    "DRV-1009": ("In service", ""),                 # clean, so APX-2610161 auto-applies
    "DRV-1010": ("In service", ""),                 # clean, so APX-2610179 is only a mismatch
}

# Defects that ground a tractor, and the minor ones that only earn a note.
BLOCKING_DEFECTS = ["brake air leak", "tire tread below limit", "lights inoperative",
                    "reefer unit fault", "air bag leak", "faulty abs sensor"]
MINOR_DEFECTS = ["worn wiper blades", "cracked mirror", "low coolant warning"]
BLOCKING_MARKERS = ("brake", "tire", "light", "reefer", "abs", "air bag")


def is_blocking_defect(defect):
    text = (defect or "").lower()
    return any(marker in text for marker in BLOCKING_MARKERS)

# Terminals where a new Available driver would break a story beat, so the
# generated available pool deliberately skips them.
BEAT_SENSITIVE_TERMINALS = {"PHX", "CLT", "HAR"}

CUSTOMERS = [
    "Sunbelt Beverages", "Keystone Chemicals", "Great Lakes Steel", "Summit Electronics",
    "Brightline Pharma", "Cascade Paper", "Harbor Freight Foods", "Ironwood Lumber",
    "Meridian Plastics", "Northgate Produce", "Orion Auto Parts", "Piedmont Textiles",
    "Redstone Building Supply", "Sierra Bottling", "Tidewater Seafood", "Union Cold Storage",
    "Vertex Components", "Waypoint Retail", "Yellowstone Dairy", "Zephyr Aerospace",
    "Anchor Brewing Co", "Bluegrass Mills", "Copperfield Tools", "Delta Nurse Supplies",
]

EXCEPTION_NOTES = {
    "Weather": "Severe storm on planned route; chain law / closure in effect",
    "Equipment breakdown": "Tractor fault code, unit towed to repair shop",
    "HOS limit": "Driver approaching hours limit before final leg",
    "Road closure": "Interstate ramp closed; 40 mile detour required",
    "Reefer temp alarm": "Reefer unit drifting above setpoint; load at risk",
    "Driver illness": "Driver unwell at last stop; cannot complete delivery",
    "Missed appointment": "Delivery window missed; receiver rescheduling",
    "Inspection hold": "Held at weigh station for level 1 inspection",
    "Truck breakdown": "Assigned tractor failed pre-trip; replacement needed",
    "Driver out of hours": "Driver has run out of legal driving hours",
    "Reefer unit fault": "Reefer unit fault code; load cannot move on this trailer",
    "Late pickup": "Shipper not ready at pickup; appointment slipped",
}

LOAD_MIX = [
    ("Dry Van", "Dry Van 53ft", "N", 0.34),
    ("Reefer", "Reefer 53ft", "N", 0.30),
    ("Flatbed", "Flatbed 48ft", "N", 0.28),
    ("Hazmat", "Dry Van 53ft", "Y", 0.08),
]


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 3958.8 * math.asin(min(1.0, math.sqrt(a)))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def read(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def pick_load(rng):
    roll = rng.random()
    acc = 0.0
    for load_type, equipment, hazmat, weight in LOAD_MIX:
        acc += weight
        if roll <= acc:
            return load_type, equipment, hazmat
    return "Dry Van", "Dry Van 53ft", "N"


def main():
    rng = random.Random(SEED)

    # ---------------------------------------------------------- terminals
    base_terminals = read(os.path.join(BASE, "terminals.csv"))
    terminals = list(base_terminals)
    for code, city, state, lat, lon in NEW_TERMINALS:
        terminals.append({"terminal_code": code, "city": city, "state": state,
                          "lat": lat, "lon": lon})
    tcoords = {t["terminal_code"]: (float(t["lat"]), float(t["lon"])) for t in terminals}
    tcity = {t["terminal_code"]: "%s, %s" % (t["city"], t["state"]) for t in terminals}
    codes = [t["terminal_code"] for t in terminals]

    # ------------------------------------------------------------ drivers
    base_drivers = read(os.path.join(BASE, "drivers.csv"))
    drivers = []
    for row in base_drivers:
        rid = row["driver_id"]
        if rid in PINNED_HOS:
            drive, window, brk, cycle = PINNED_HOS[rid]
        else:
            drive = round(rng.uniform(0.0, 10.5), 1)
            window = round(min(13.9, drive + rng.uniform(0.5, 4.0)), 1)
            brk = round(rng.uniform(0.0, 9.5), 1)
            cycle = round(rng.uniform(4.0, 68.0), 1)
        drivers.append({
            "driver_id": rid,
            "name": row["name"],
            "home_terminal": row["home_terminal"],
            "current_terminal": row["current_terminal"],
            "current_city": row["current_city"],
            "status": row["status"],
            "drive_hours_used_today": drive,
            "on_duty_window_used": window,
            "hours_since_last_break": brk,
            "cycle_hours_used_8d": cycle,
            "last_10h_reset_utc": iso(BASE_NOW - timedelta(hours=round(rng.uniform(1.0, 22.0), 1))),
            "endorsements": row["endorsements"],
            "equipment": row["equipment"],
            "truck_id": "",
            "safety_score": row["safety_score"],
            "phone": row["phone"],
        })

    names = ["Marcus Reed", "Elena Vasquez", "Dwayne Carter", "Ingrid Halvorsen", "Tobias Grant",
             "Yusuf Demir", "Camille Rousseau", "Hector Salinas", "Nadia Petrova", "Owen Brady",
             "Fatima Rahman", "Lukas Meyer", "Rene Duplantis", "Sasha Kowalski", "Grace Wambui",
             "Diego Marchetti", "Aisha Bello", "Karl Lindgren", "Priscilla Okafor", "Viktor Novak",
             "Marisol Vega", "Desmond Clarke", "Anika Sharma", "Bruno Almeida", "Tessa Nguyen",
             "Reuben Osei", "Lidia Kowalczyk", "Soren Bjorklund", "Hana Takahashi", "Malcolm Reyes",
             "Petra Silva", "Emeka Adeyemi", "Clara Fontaine", "Rustam Karimov", "Bianca Rossi",
             "Neil Fairbanks", "Zoe Whitfield", "Amir Haddad", "Lucia Herrera", "Curtis Boyd"]
    equipment_pool = ["Dry Van 53ft", "Reefer 53ft", "Flatbed 48ft"]
    endorsement_pool = ["None", "None", "Hazmat", "Reefer-certified", "Tanker", "TWIC", "Hazmat,TWIC",
                        "Reefer-certified,Tanker"]
    # terminals a NEW available driver may sit at
    available_terminals = [c for c in codes if c not in BEAT_SENSITIVE_TERMINALS]

    next_id = len(drivers) + 1001
    new_available = 20
    while len(drivers) < TARGET_DRIVERS:
        index = len(drivers)
        if index < len(base_drivers) + new_available:
            status = "Available"
            terminal = rng.choice(available_terminals)
            drive = round(rng.uniform(0.0, 9.5), 1)
            window = round(min(13.9, drive + rng.uniform(0.5, 3.5)), 1)
            equipment = rng.choice(equipment_pool)
        else:
            status = rng.choice(["On Load", "On Load", "On Load", "Off Duty", "On Break"])
            terminal = rng.choice(codes)
            drive = round(rng.uniform(0.0, 10.5), 1)
            window = round(min(13.9, drive + rng.uniform(0.5, 4.0)), 1)
            equipment = rng.choice(equipment_pool)
        drivers.append({
            "driver_id": "DRV-%d" % next_id,
            "name": names[(index - len(base_drivers)) % len(names)] + (
                "" if index - len(base_drivers) < len(names) else " %d" % (index // len(names))),
            "home_terminal": terminal,
            "current_terminal": terminal,
            "current_city": tcity[terminal],
            "status": status,
            "drive_hours_used_today": drive,
            "on_duty_window_used": window,
            "hours_since_last_break": round(rng.uniform(0.0, 9.5), 1),
            "cycle_hours_used_8d": round(rng.uniform(4.0, 68.0), 1),
            "last_10h_reset_utc": iso(BASE_NOW - timedelta(hours=round(rng.uniform(1.0, 22.0), 1))),
            "endorsements": rng.choice(endorsement_pool),
            "equipment": equipment,
            "truck_id": "",
            "safety_score": rng.randint(76, 99),
            "phone": "(555) %03d-%04d" % (rng.randint(200, 989), rng.randint(1000, 9989)),
        })
        next_id += 1

    # ------------------------------------------------------------- trucks
    trucks = []
    truck_of = {}
    for i, driver in enumerate(drivers, start=1001):
        truck_id = "TRK-%d" % i
        driver["truck_id"] = truck_id
        truck_of[driver["driver_id"]] = truck_id
    for driver in drivers:
        if driver["driver_id"] in PINNED_TRUCKS:
            status, defect = PINNED_TRUCKS[driver["driver_id"]]
        elif rng.random() < 0.06:
            status, defect = "Out of service", rng.choice(BLOCKING_DEFECTS)
        elif rng.random() < 0.18:
            status, defect = "In service", rng.choice(MINOR_DEFECTS)
        else:
            status, defect = "In service", ""
        # the status always matches the defect: a blocking defect grounds the truck
        if defect and is_blocking_defect(defect):
            status = "Out of service"
        trucks.append({
            "truck_id": driver["truck_id"],
            "terminal": driver["home_terminal"],
            "equipment": driver["equipment"],
            "status": status,
            "open_defect": defect,
            "last_inspection_utc": iso(BASE_NOW - timedelta(days=rng.randint(1, 40), hours=rng.randint(0, 23))),
        })

    # ---------------------------------------------------------- shipments
    base_shipments = read(os.path.join(BASE, "shipments.csv"))
    shipments = [dict(row) for row in base_shipments]
    on_load = [d["driver_id"] for d in drivers if d["status"] == "On Load"]

    next_ship = 2610200
    while len(shipments) < TARGET_SHIPMENTS:
        origin, dest = rng.sample(codes, 2)
        o_lat, o_lon = tcoords[origin]
        d_lat, d_lon = tcoords[dest]
        miles = int(round(haversine(o_lat, o_lon, d_lat, d_lon) * 1.2))
        if miles < 120:
            continue
        load_type, equipment, hazmat = pick_load(rng)
        pct = rng.randint(0, 100)
        pickup = BASE_NOW + timedelta(hours=rng.randint(-30, 8))
        transit = miles / 55.0
        due = pickup + timedelta(hours=transit + rng.uniform(2, 20))
        eta = due + timedelta(hours=rng.choice([-4, -2, 0, 1, 3, 6]))
        status = rng.choices(["In Transit", "Scheduled", "Delivered", "At Risk"],
                             weights=[5, 2, 3, 2])[0]
        if status == "Delivered":
            pct = 100
            eta = pickup + timedelta(hours=transit)
        fuel = round(rng.uniform(0.62, 0.78), 2)
        rate = round(rng.uniform(2.10, 3.90), 2)
        linehaul = round(miles * rate, 2)
        est_fuel = round(miles * fuel, 2)
        shipments.append({
            "shipment_id": "APX-%d" % next_ship,
            "bol_number": "BOL%d" % rng.randint(100000000, 999999999),
            "seal_number": "SL-%d" % rng.randint(100000, 999999),
            "customer": rng.choice(CUSTOMERS),
            "load_type": load_type,
            "equipment": equipment,
            "weight_lbs": rng.randint(8000, 44000),
            "pallets": rng.randint(4, 26),
            "hazmat": hazmat,
            "temp_setpoint_f": rng.choice(["", "", "", "34", "36", "-10"]) if load_type == "Reefer" else "",
            "origin_terminal": origin,
            "origin_city": tcity[origin],
            "dest_terminal": dest,
            "dest_city": tcity[dest],
            "miles": miles,
            "pickup_utc": iso(pickup),
            "delivery_due_utc": iso(due),
            "eta_utc": iso(eta),
            "percent_complete": pct,
            "nearest_terminal": dest if pct < 70 else rng.choice(codes),
            "current_location": "En route, ~%d mi from %s" % (round(miles * (100 - pct) / 100), tcity[dest]),
            "current_lat": round(d_lat + rng.uniform(-1.5, 1.5), 3),
            "current_lon": round(d_lon + rng.uniform(-1.5, 1.5), 3),
            "status": status,
            "exception_type": "",
            "exception_note": "",
            "priority": rng.choice(["Standard", "Standard", "Expedited"]),
            "driver_id": rng.choice(on_load),
            "tractor_id": rng.choice(["TRC-%d" % rng.randint(4100, 4999)]),
            "trailer_id": "TRL-%d" % rng.randint(7000, 7999),
            "rate_per_mile_usd": rate,
            "linehaul_usd": linehaul,
            "fuel_cost_per_mile_usd": fuel,
            "est_fuel_usd": est_fuel,
            "est_margin_usd": round(linehaul - est_fuel - miles * 0.95, 2),
        })
        next_ship += 1

    # ------------------------------------------------- the four new exceptions
    # Chosen to exercise the new rules: a failed tractor, a driver with no hours
    # left, a reefer unit fault, and an ordinary slip.
    new_exceptions = [
        ("APX-2610203", "Truck breakdown", "Dry Van", "Dry Van 53ft", "N", "SAT", "TUL", 268, 41, 62,
         "Expedited", "DRV-1090"),
        ("APX-2610207", "Driver out of hours", "Reefer", "Reefer 53ft", "N", "PDX", "MSY", 693, 58, 71,
         "Expedited", "DRV-1091"),
        ("APX-2610211", "Reefer unit fault", "Reefer", "Reefer 53ft", "N", "BUF", "ELP", 812, 33, 55,
         "Standard", "DRV-1092"),
        ("APX-2610215", "Late pickup", "Flatbed", "Flatbed 48ft", "N", "TUL", "SAT", 281, 22, 40,
         "Expedited", "DRV-1093"),
    ]
    by_id = {s["shipment_id"]: s for s in shipments}
    for (sid, etype, load_type, equipment, hazmat, origin, dest, miles, pct,
         late_hours, priority, driver_id) in new_exceptions:
        pickup = BASE_NOW - timedelta(hours=6)
        transit = miles / 55.0
        due = pickup + timedelta(hours=transit + 6)
        row = {
            "shipment_id": sid,
            "bol_number": "BOL%d" % rng.randint(100000000, 999999999),
            "seal_number": "SL-%d" % rng.randint(100000, 999999),
            "customer": rng.choice(CUSTOMERS),
            "load_type": load_type,
            "equipment": equipment,
            "weight_lbs": rng.randint(8000, 44000),
            "pallets": rng.randint(4, 26),
            "hazmat": hazmat,
            "temp_setpoint_f": "34" if load_type == "Reefer" else "",
            "origin_terminal": origin,
            "origin_city": tcity[origin],
            "dest_terminal": dest,
            "dest_city": tcity[dest],
            "miles": miles,
            "pickup_utc": iso(pickup),
            "delivery_due_utc": iso(due),
            "eta_utc": iso(due + timedelta(hours=late_hours)),
            "percent_complete": pct,
            "nearest_terminal": dest,
            "current_location": "En route, ~%d mi from %s" % (round(miles * (100 - pct) / 100), tcity[dest]),
            "current_lat": round(tcoords[dest][0] + 0.4, 3),
            "current_lon": round(tcoords[dest][1] + 0.4, 3),
            "status": "Exception",
            "exception_type": etype,
            "exception_note": EXCEPTION_NOTES[etype],
            "priority": priority,
            "driver_id": driver_id,
            "tractor_id": truck_of.get(driver_id, "TRC-4100"),
            "trailer_id": "TRL-%d" % rng.randint(7000, 7999),
            "rate_per_mile_usd": round(rng.uniform(2.10, 3.90), 2),
            "linehaul_usd": 0.0,
            "fuel_cost_per_mile_usd": round(rng.uniform(0.62, 0.78), 2),
            "est_fuel_usd": 0.0,
            "est_margin_usd": round(rng.uniform(400, 2400), 2),
        }
        row["linehaul_usd"] = round(miles * row["rate_per_mile_usd"], 2)
        row["est_fuel_usd"] = round(miles * row["fuel_cost_per_mile_usd"], 2)
        if sid in by_id:
            by_id[sid].update(row)
        else:
            shipments.append(row)

    # every driver referenced by a shipment must be On Load in the roster
    referenced = {s["driver_id"] for s in shipments}
    for driver in drivers:
        if driver["driver_id"] in referenced and driver["driver_id"] not in PINNED_HOS:
            if driver["status"] == "Available":
                driver["status"] = "On Load"

    # --------------------------------------------------------------- write
    exceptions = [s for s in shipments if s["status"] == "Exception"]
    write(os.path.join(DATA, "terminals.csv"), terminals,
          ["terminal_code", "city", "state", "lat", "lon"])
    write(os.path.join(DATA, "drivers.csv"), drivers, [
        "driver_id", "name", "home_terminal", "current_terminal", "current_city", "status",
        "drive_hours_used_today", "on_duty_window_used", "hours_since_last_break",
        "cycle_hours_used_8d", "last_10h_reset_utc", "endorsements", "equipment",
        "truck_id", "safety_score", "phone"])
    write(os.path.join(DATA, "trucks.csv"), trucks, [
        "truck_id", "terminal", "equipment", "status", "open_defect", "last_inspection_utc"])
    write(os.path.join(DATA, "shipments.csv"), shipments, list(base_shipments[0].keys()))

    print("terminals  %d" % len(terminals))
    print("drivers    %d (%d available)" % (len(drivers), sum(1 for d in drivers if d["status"] == "Available")))
    print("trucks     %d (%d out of service, %d with an open defect)"
          % (len(trucks), sum(1 for t in trucks if t["status"] == "Out of service"),
             sum(1 for t in trucks if t["open_defect"])))
    print("shipments  %d" % len(shipments))
    print("exceptions %d" % len(exceptions))
    for s in sorted(exceptions, key=lambda r: r["shipment_id"]):
        print("   %s  %-10s %-14s near %s" % (s["shipment_id"], s["load_type"], s["exception_type"], s["nearest_terminal"]))


if __name__ == "__main__":
    main()
