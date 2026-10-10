# apex-dispatch RUNBOOK

Everything runs from `~/apextrans-demo/dispatch` unless noted.

## Container

| Action | Command |
| --- | --- |
| Start | `docker run -d --name apex-dispatch --network kong-ai-quickstart-net --network-alias apex-dispatch -p 8090:8090 apex-dispatch` |
| Stop | `docker stop apex-dispatch` |
| Start again | `docker start apex-dispatch` |
| **Reset (fresh state)** | `docker restart apex-dispatch` |
| Rebuild + recreate | `docker build -t apex-dispatch . && docker rm -f apex-dispatch` then the start row above |
| Health | `curl -s localhost:8090/health` → `{"ok": true}` |

Reset clears every proposal, update and in-memory shipment change, so the demo
starts from the 8 original exceptions again. Never touch
`kong-ai-quickstart-gateway` or `ai-pii-service`.

## Demo, in order

1. **Service is up**
   `docker restart apex-dispatch && curl -s localhost:8090/health`
   → `{"ok": true}`

2. **Acceptance suite** (always reset first, it writes state)
   `python3 check_acceptance.py`
   → 16 lines, ending `15/15 checks passed` and `ACCEPTANCE OK`

3. **The story, offline (no gateway needed)**
   `python3 exception_agent.py --direct --yes --reset`
   → `--reset` restarts the container first so **every run starts from fresh state**
   (identical to `docker restart apex-dispatch`; set `APEX_CONTAINER` to change the
   name). Then: 8 exceptions, `AUTO-APPLIED` / `HELD FOR HUMAN` lines with the
   blocking reasons, any `model pick X rejected (reason) -> using Y` lines, one
   `APPLIED RR-000x by dispatch-supervisor`, and the `/updates` feed. With the
   shipped data at 55 mph expect **2 auto-applied, 6 held**. Reset after with
   `docker restart apex-dispatch`.

4. **Through the gateway** (once the Kong tools are applied)
   `source ~/.kong/apextrans-squad-keys.env` (you run this, never Bentley)
   `python3 exception_agent.py --model my-gpt-4o`
   → step 1 lists the tools the agent can see (**no `approve_reroute`**); step 5
   prints Kong's refusal in red (403, or "tool not found"); step 6 uses
   `KEY_SUPERVISOR` to approve and applies.

5. **Cursor**
   `python3 kong/add_cursor_mcp.py`
   → prints the saved server names only; `firecrawl-mcp`, `kong-konnect` and
   `Origin` must all still be there.

## If this breaks

- **Service down / connection refused on 8090** → `docker ps | grep apex-dispatch`; if it is missing rerun the start row, if it is unhealthy read `docker logs --tail 50 apex-dispatch`.
- **401 from the gateway** → the `X-API-Key` is missing or from the wrong squad; re-source `~/.kong/apextrans-squad-keys.env`, and check you used `KEY_DISPATCH` for the agent and `KEY_SUPERVISOR` for the approval step.
- **403 (or "tool not found") when you did not expect it** → that identity is not allowed that tool: only `dispatch-supervisor` can see or call `approve_reroute`. Expected for the agent in step 5; if a read tool 403s instead, check the consumer ACLs on `apex-dispatch-mcp`.
