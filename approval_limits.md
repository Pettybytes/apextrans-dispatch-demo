# ApexTrans Dispatch — Approval Limits & Reroute Rules

These are the guardrails the `apex-dispatch` service applies on every
`POST /reroutes`. They exist to keep a load moving without letting a bad
reassignment leave the building unchecked.

## The two outcomes

| Outcome | Condition | Effect |
| --- | --- | --- |
| `auto_applied` | **no conflicts** *and* reroute cost **≤ $500** | Shipment is reassigned immediately |
| `pending_human` | any conflict, **or** cost **> $500** | Proposal is held with a `conflicts` list for a supervisor |

A cost above the limit is reported as a conflict
(`cost $746 over $500 limit`) so the hold reason is explicit.

## Hard conflicts — always hold for a human

| # | Rule | Notes |
| --- | --- | --- |
| 1 | **Equipment mismatch** | `Reefer` load needs `Reefer 53ft`; `Flatbed` needs `Flatbed 48ft`; `Dry Van` and `Hazmat` need `Dry Van 53ft` |
| 2 | **Hazmat endorsement** | A `Hazmat` load may only go to a driver holding the `Hazmat` endorsement |
| 3 | **Reefer certification** | A `Reefer` load may only go to a `Reefer-certified` driver |
| 4 | **Drive limit (11-hour)** | `11 − drive_hours_used_today` must cover the job |
| 5 | **On-duty window (14-hour)** | `14 − on_duty_window_used` must cover the job |
| 6 | **Weekly cycle (70-hour)** | `70 − cycle_hours_used_8d` must cover the job |
| 7 | **Truck out of service** | `Truck out of service: <defect>` |
| 8 | **Open tractor defect** | `Open defect on truck <id>: <defect>. Must be repaired before dispatch` |
| 9 | **Reefer unit fault** | A `Reefer` load cannot move on a tractor whose open defect is the reefer unit |
| 10 | **Availability** | Driver must be `Available`, and must not already be holding another pending proposal |
| 11 | **Estimated cost** | Over the auto-approval limit (`cost $746 over $500 limit`) |
| 12 | **Missed delivery window** | The projected ETA is still after `delivery_due_utc` |
| 13 | **Critical priority** | Any `priority=Critical` shipment requires human approval |

## Hours of service

Each driver carries live clocks rather than one number:

- `drive_hours_used_today`, `on_duty_window_used`, `hours_since_last_break`, `cycle_hours_used_8d`, `last_10h_reset_utc`
- `available = min(11 − drive_hours_used_today, 14 − on_duty_window_used, 70 − cycle_hours_used_8d)`

The job needs `(deadhead_miles + remaining_miles) / 55 mph`. Every clock that
cannot cover it reports its own line:

- `Drive limit: needs 9.4 h, driver has 3.1 h left today (11-hour rule)`
- `On-duty window: 14-hour window closes in 2.0 h`
- `Weekly cycle: 66 of 70 hours used`

## Tractor condition

`trucks.csv` holds one tractor per driver (`truck_id, terminal, equipment,
status, open_defect, last_inspection_utc`). `GET /drivers/available` and
`GET /shipments/{id}` carry `truck_id`, `truck_status` and `open_defect`. An out
of service tractor, an open defect, or a reefer unit fault on a `Reefer` load is
a hard conflict, so a load never moves on a tractor that should not be rolling.

## ETA notes — never a hold on their own

These only push the ETA out; they are reported in the proposal's `notes` list,
kept separate from `conflicts` so an auto-applied reroute still has no conflicts:

| Rule | Effect |
| --- | --- |
| **Break due** (`hours_since_last_break >= 8`) | adds 30 minutes |
| **Overnight reset** (job longer than the remaining shift) | adds a 10-hour reset |

## Cost model

```
reroute_cost_usd = deadhead_miles × (fuel_cost_per_mile_usd + 0.95)
```

- `deadhead_miles` — estimated road miles from the driver's current terminal to
  the shipment's **nearest terminal**: `great_circle_miles × 1.2` (circuity factor).
- `+0.95` — flat per-mile adder for driver pay and fixed cost on the empty leg.
- `fuel_cost_per_mile_usd` — taken from the shipment record.

## Derived quantities

- `remaining_miles = miles × (100 − percent_complete) / 100`
- `transit_hours = (deadhead_miles + remaining_miles) / 55 mph`
- `new_eta_utc = max(now_utc, pickup_utc) + transit_hours`

## On apply (`auto_applied` or an approved proposal)

1. Shipment `driver_id` → the new driver.
2. Shipment `status` → `Rerouted`.
3. Shipment `eta_utc` → the new ETA.
4. New driver `status` → `On Load`.
5. A customer-facing line is appended to the `/updates` feed, e.g.
   *"APX-2610141 reassigned to Keisha Okafor (DRV-1004). New ETA 16:10 UTC. Customer notified."*

## Tunables (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISPATCH_AUTO_LIMIT_USD` | `500` | Auto-approval ceiling |
| `DISPATCH_SPEED_MPH` | `55` | Planning speed for HOS and ETA |
| `DISPATCH_CIRCUITY_FACTOR` | `1.2` | Great-circle → road-mile multiplier |
| `DISPATCH_DEADHEAD_ADDON` | `0.95` | Per-mile adder in the cost model |

Every decision — proposal, conflict list, approval and applied change — is
appended to `data/audit.jsonl` and served on `GET /audit`.
