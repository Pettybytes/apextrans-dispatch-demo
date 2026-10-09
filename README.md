# apex-dispatch

ApexTrans dispatch service — shipment data plus the business rules that decide
whether a load can be moved to a different driver automatically or has to wait
for a human.

It is a small, dependency-free Python service (standard library only). Kong AI
Gateway sits in front of it and turns these REST endpoints into MCP tools, then
controls which identity may call each one. **This service knows nothing about
Kong** — it is a plain HTTP service.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | `{"ok": true}` |
| GET | `/exceptions` | Open exceptions: id, customer, load type, lane, current location, nearest terminal, exception type/note, priority, due vs ETA |
| GET | `/shipments/{shipment_id}` | Full shipment record |
| GET | `/drivers/available?terminal=&equipment=&endorsement=&shipment_id=` | Available drivers; with `shipment_id`, each carries `deadhead_miles` to the shipment's nearest terminal |
| POST | `/reroutes` | Body `{shipment_id, new_driver_id, reason, requested_by}` → runs the rules, returns a proposal |
| POST | `/reroutes/{proposal_id}/approve` | Body `{approver, note}` → applies a held proposal |
| GET | `/reroutes?status=pending` | Held proposals |
| GET | `/updates` | Applied changes and customer-facing ETA messages, newest first |
| GET | `/audit` | Every proposal, decision and approval with UTC timestamps |
| GET | `/openapi.json` | OpenAPI 3.0 spec (import this into the gateway) |

Rules, the cost model and the auto-approval limit are written up in
[`approval_limits.md`](approval_limits.md).

## Run locally

```bash
cd dispatch
python3 app.py            # http://localhost:8090
curl -s localhost:8090/health
```

## Run as a container (Kong network)

```bash
docker build -t apex-dispatch ~/apextrans-demo/dispatch
docker run -d --name apex-dispatch \
  --network kong-ai-quickstart-net --network-alias apex-dispatch \
  -p 8090:8090 apex-dispatch
curl -s localhost:8090/health
```

Kong reaches the service at `http://apex-dispatch:8090`.

## Exception agent

`exception_agent.py` is the demo script that reads exceptions, asks the model
to pick a driver, and calls the reroute/approve tools.

```bash
# direct mode (before the gateway tools exist): tools hit localhost:8090
python3 exception_agent.py --direct

# through the gateway (later): LLM + MCP tools both go via Kong
source ~/.kong/apextrans-squad-keys.env
python3 exception_agent.py
```

Environment (gateway mode): `KONNECT_PROXY_URL`, `KEY_DISPATCH`,
`APEX_MCP_URL`, `KEY_SUPERVISOR`. Secrets are read from the environment and are
never printed.

## State

Shipments, drivers and terminals are loaded from `data/*.csv` at start-up and
held in memory. Every proposal, decision and approval is appended to
`data/audit.jsonl`.
