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

Reset is `docker restart apex-dispatch`. It **restarts the service only** - it does
not run the demo - and it clears every proposal, update and in-memory shipment
change, so the next run starts from the 8 original exceptions again. Never touch
`kong-ai-quickstart-gateway` or `ai-pii-service`.

### The supervisor key

There is no supervisor key in the environment or in any file in this repo, and
the agent ignores `KEY_SUPERVISOR` even if it is set. The key lives only in
Bryan's locked note and is **pasted at approval time** into the hidden prompt
(`Dispatcher, paste your supervisor key:`). With no human at the keyboard the
approval is skipped.

## Demo, in order

1. **Service is up**
   `docker restart apex-dispatch && curl -s localhost:8090/health`
   → `{"ok": true}`. Fresh state is **12 open exceptions, 0 pending**.

2. **Acceptance suite** (always reset first, it writes state)
   `python3 check_acceptance.py`
   → ending `20/20 checks passed` and `ACCEPTANCE OK`

3. **Writes are gateway-only now** (this is the point, not a bug)
   `python3 exception_agent.py --direct --yes --reset`
   → `--reset` restarts the container first (`docker restart apex-dispatch`).
   Reads work, so the tool list and the twelve exceptions print, but the first
   reroute is refused: `403 writes are only accepted through Kong AI Gateway`.
   `--direct` reaches the service and gets nowhere, so use the gateway path
   (step 4) for the full flow. Every refusal is written to `/audit` as
   "blocked direct write from <ip>".

   A normal run on fresh data gives **3 auto-applied and 9 held**: APX-2610161,
   APX-2610177 and APX-2610140 clear the rules on their own, and the rest wait
   for a dispatcher - mostly on the drive clock, a tractor defect, or the
   estimated cost.

4. **Through the gateway** (once the Kong tools are applied)
   `source ~/.kong/apextrans-squad-keys.env` (you run this, never Bentley)
   `python3 exception_agent.py --model my-gpt-4o`
   → step 1 lists the tools the agent can see (**no `approve_reroute`**); step 5
   prints Kong's refusal in red (403, or "tool not found"); step 6 asks you to
   paste the supervisor key and, only if you do, approves in a separate
   supervisor session.

5. **Cursor**
   `python3 kong/add_cursor_mcp.py`
   → prints the saved server names only; `firecrawl-mcp`, `kong-konnect` and
   `Origin` must all still be there.

## Recording a run for the public replay page

```bash
python3 export_run.py --snapshot /tmp/run-start.json   # BEFORE the run: the full board
#   ... run the agent, approve one proposal, run squad_activity ...
python3 export_run.py --start /tmp/run-start.json -o data/run.json
cp data/run.json ~/apextrans-demo/dispatch-site/data/run.json
cd ~/apextrans-demo/dispatch-site && vercel --prod
```

`export_run.py` reads only, needs no keys, and strips header-name logs and any
addresses before writing. The replay page starts at the full board and applies
each event in order, so it can be shown without a live service.

## Kong wiring (once, before the panel)

```bash
cd ~/apextrans-demo/dispatch
AI_GATEWAY_ID=<your-gateway-id> kongctl apply -f kong/apex-dispatch-mcp.yaml   # no --auto-approve
python3 kong/check_tool_access.py
python3 squad_activity.py --support APX-2610149
python3 squad_activity.py --compliance
```

Expected tool counts, enforced by the explicit `access.acls.allow` lists in the
YAML: **support-bot 2, dispatch-copilot 7, compliance-docs 2,
dispatch-supervisor 8**. `check_tool_access.py` exits non-zero if any is wrong.
Both squad scripts then show up in the board's live feed (read attribution only
works for callers the gateway names - the header is trusted only from the Kong
data plane).

The board stamps a content hash onto `board.css` / `board.js`, so a rebuild is
never served stale out of the browser cache.

## Local reset and the replay page

The board's **Reset demo** button only appears when you open it on `localhost`.
It asks for the local admin token (kept in the tab, never shown) and calls
`POST /admin/reset`, which reloads the CSVs in memory - the same result as a
container restart. The endpoint is refused unless the caller is the docker host
*and* presents the token, so it can never be reached from Kong. The token lives
in `~/.apextrans/admin-token` and is passed in at start via
`--env-file ~/.apextrans/admin.env`; it is deliberately not in the Kong keys file
and is never printed.

## If this breaks

- **Service down / connection refused on 8090** → `docker ps | grep apex-dispatch`; if it is missing rerun the start row, if it is unhealthy read `docker logs --tail 50 apex-dispatch`.
- **401 from the gateway** → the `X-API-Key` is missing or from the wrong squad; re-source `~/.kong/apextrans-squad-keys.env`, and check you used `KEY_DISPATCH` for the agent and `KEY_SUPERVISOR` for the approval step.
- **403 (or "tool not found") when you did not expect it** → that identity is not allowed that tool: only `dispatch-supervisor` can see or call `approve_reroute`. Expected for the agent in step 5; if a read tool 403s instead, check the consumer ACLs on `apex-dispatch-mcp`.
