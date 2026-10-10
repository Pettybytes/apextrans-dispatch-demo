#!/usr/bin/env python3
"""ApexTrans exception agent.

Reads open exceptions, asks the model to pick a driver for each one, and calls
the reroute / approve tools. It is written to talk **only** to Kong:

  * LLM calls  -> $KONNECT_PROXY_URL/v1/chat/completions  (X-API-Key: $KEY_DISPATCH)
  * tool calls -> MCP over HTTP at $APEX_MCP_URL          (X-API-Key: $KEY_DISPATCH)

Until the gateway tools exist, run with ``--direct``: tool calls go straight to
the dispatch service on localhost:8090. The LLM call still goes through the
gateway when ``KONNECT_PROXY_URL`` and ``KEY_DISPATCH`` are present; without
them ``--direct`` falls back to a small deterministic chooser so the flow can
still be rehearsed offline.

Secrets are read from the environment and are never printed.

Usage:
    python3 exception_agent.py --direct                 # tools -> localhost:8090
    python3 exception_agent.py                          # everything through Kong
    python3 exception_agent.py --model apex-claude      # pick the gateway LLM route
    python3 exception_agent.py --yes                    # auto-answer the approval prompt

In gateway mode APEX_MCP_URL defaults to $KONNECT_PROXY_URL/apex-dispatch
(falling back to http://localhost:8000/apex-dispatch).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_DIRECT_BASE = os.environ.get("DISPATCH_BASE_URL", "http://localhost:8090")
LLM_MODEL = os.environ.get("APEX_LLM_MODEL", "my-gpt-4o")
REQUEST_TIMEOUT = int(os.environ.get("APEX_HTTP_TIMEOUT", "30"))
SYSTEM_PROMPT = "You are a precise freight dispatcher. Reply with compact JSON only."
# The gateway rate-limits one request against the squad's tokens/minute budget,
# so the prompt stays small: this many closest candidates, minimal fields.
CANDIDATES_PER_EXCEPTION = int(os.environ.get("APEX_PROMPT_CANDIDATES", "3"))

# ---------------------------------------------------------------- pretty print
_USE_COLOR = sys.stdout.isatty()


def _c(code):
    return code if _USE_COLOR else ""


GREEN, AMBER, RED, DIM, BOLD, RESET = (
    _c("\033[32m"),
    _c("\033[33m"),
    _c("\033[31m"),
    _c("\033[2m"),
    _c("\033[1m"),
    _c("\033[0m"),
)


def step(number, text):
    print("\n%s[%s] %s%s" % (BOLD, number, text, RESET))


def note(text):
    print("    %s%s%s" % (DIM, text, RESET))


def secret_state(name):
    """Report whether an env var is set without ever printing its value."""
    return "set" if os.environ.get(name) else "MISSING"


# ------------------------------------------------------------------ transports
class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__("HTTP %s: %s" % (status, body[:300]))
        self.status = status
        self.body = body


def http_json(method, url, body=None, headers=None, timeout=REQUEST_TIMEOUT):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        if value:
            request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload, dict(exc.headers)


# ------------------------------------------------------- direct tool adapter
class DirectTools:
    """Tool calls straight to the dispatch service (pre-Kong mode)."""

    label = "direct (localhost:8090)"

    def __init__(self, base=DEFAULT_DIRECT_BASE):
        self.base = base.rstrip("/")

    def list_tools(self):
        status, spec, _ = http_json("GET", self.base + "/openapi.json")
        names = []
        for path, operations in (spec.get("paths") or {}).items():
            for operation in operations.values():
                if isinstance(operation, dict) and operation.get("operationId"):
                    names.append(operation["operationId"])
        return names

    def get(self, path):
        status, payload, _ = http_json("GET", self.base + path)
        if status >= 400:
            raise HttpError(status, json.dumps(payload))
        return payload

    def list_exceptions(self):
        return self.get("/exceptions")["exceptions"]

    def list_available_drivers(self, shipment_id):
        return self.get("/drivers/available?shipment_id=" + urllib.parse.quote(shipment_id))["drivers"]

    def create_reroute(self, shipment_id, new_driver_id, reason):
        status, payload, _ = http_json(
            "POST",
            self.base + "/reroutes",
            {"shipment_id": shipment_id, "new_driver_id": new_driver_id, "reason": reason, "requested_by": "exception-agent"},
        )
        if status >= 400:
            raise HttpError(status, json.dumps(payload))
        return payload

    def approve_reroute(self, proposal_id, approver, note, api_key=None):
        status, payload, _ = http_json(
            "POST",
            "%s/reroutes/%s/approve" % (self.base, proposal_id),
            {"approver": approver, "note": note},
        )
        return status, payload

    def list_updates(self):
        return self.get("/updates")["updates"]


# ---------------------------------------------------------- MCP tool adapter
class McpTools:
    """MCP over HTTP through the gateway (post-Kong mode)."""

    label = "MCP via gateway"

    def __init__(self, url, api_key):
        self.url = url
        self.api_key = api_key
        self.session_id = None
        self._id = 0

    def _post(self, payload):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json, text/event-stream")
        if self.api_key:
            request.add_header("X-API-Key", self.api_key)
        if self.session_id:
            request.add_header("Mcp-Session-Id", self.session_id)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                status = response.status
                raw = response.read().decode("utf-8", "replace")
                content_type = response.headers.get("Content-Type", "")
                session = response.headers.get("Mcp-Session-Id")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", "replace")
            content_type = exc.headers.get("Content-Type", "")
            session = exc.headers.get("Mcp-Session-Id")

        if session:
            self.session_id = session
        return status, self._parse(raw, content_type)

    @staticmethod
    def _parse(raw, content_type):
        """Accept either a JSON body or an SSE stream of JSON-RPC messages."""
        if "text/event-stream" in (content_type or ""):
            messages = []
            for line in raw.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            messages.append(json.loads(chunk))
                        except json.JSONDecodeError:
                            pass
            return messages[-1] if messages else {}
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {"raw": raw}

    def rpc(self, method, params=None, notify=False):
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self._id
        status, message = self._post(payload)
        if notify:
            return status, None
        if status >= 400:
            raise HttpError(status, json.dumps(message))
        if isinstance(message, dict) and message.get("error"):
            raise HttpError(status, json.dumps(message["error"]))
        return status, (message or {}).get("result")

    def handshake(self):
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "apex-exception-agent", "version": "1.0"},
            },
        )
        self.rpc("notifications/initialized", notify=True)
        return self.session_id

    def list_tools(self):
        result = self.rpc("tools/list", {})[1] or {}
        return [tool.get("name") for tool in result.get("tools", []) if tool.get("name")]

    @staticmethod
    def _pick(names, *keywords):
        for keyword in keywords:
            for name in names:
                if keyword in name.lower():
                    return name
        return None

    def call(self, tool_name, arguments):
        result = self.rpc("tools/call", {"name": tool_name, "arguments": arguments})[1] or {}
        if result.get("isError"):
            raise HttpError(500, json.dumps(result))
        for item in result.get("content", []):
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    return {"text": item["text"]}
        return result

    def call_raw(self, tool_name, arguments):
        """Like call(), but returns (status, payload) instead of raising."""
        try:
            return 200, self.call(tool_name, arguments)
        except HttpError as exc:
            return exc.status, {"error": exc.args[0]}


# --------------------------------------------------------------------- toolset
class Tools:
    """Uniform facade: direct HTTP or MCP, exposing the five operations."""

    def __init__(self, direct_base=None, mcp=None):
        self.direct = DirectTools(direct_base) if direct_base else None
        self.mcp = mcp
        self.names = {}

    def describe(self):
        if self.mcp:
            names = self.mcp.list_tools()
        else:
            names = self.direct.list_tools()
        self.names = {name: name for name in names}
        return names

    def _resolve(self, *keywords):
        if self.mcp:
            name = McpTools._pick(list(self.names), *keywords)
            if not name:
                raise RuntimeError("no gateway tool matched %s (saw %s)" % (keywords, sorted(self.names)))
            return name
        return None

    # Argument names match Kong's MCP renaming: {in}_{name} for parameters, and a
    # single collapsed `body` object for request bodies.
    def list_exceptions(self):
        if self.mcp:
            return self.mcp.call(self._resolve("exceptions"), {}).get("exceptions", [])
        return self.direct.list_exceptions()

    def list_available_drivers(self, shipment_id):
        if self.mcp:
            payload = self.mcp.call(self._resolve("driver"), {"query_shipment_id": shipment_id})
            return payload.get("drivers", [])
        return self.direct.list_available_drivers(shipment_id)

    def create_reroute(self, shipment_id, new_driver_id, reason):
        if self.mcp:
            return self.mcp.call(
                self._resolve("create"),
                {"body": {
                    "shipment_id": shipment_id,
                    "new_driver_id": new_driver_id,
                    "reason": reason,
                    "requested_by": "exception-agent",
                }},
            )
        return self.direct.create_reroute(shipment_id, new_driver_id, reason)

    def try_approve_as_agent(self, proposal_id):
        """Step 5: the agent tries to approve. Through Kong this must be refused."""
        if not self.mcp:
            return None
        tool = McpTools._pick(list(self.names), "approve")
        if not tool:
            return "not_found", {"error": "approve_reroute is not exposed to this identity"}
        try:
            self.mcp.call(tool, {"path_proposal_id": proposal_id,
                                 "body": {"approver": "exception-agent", "note": "agent attempting self-approval"}})
            return 200, {"note": "gateway allowed the agent to approve (unexpected)"}
        except HttpError as exc:
            return exc.status, {"error": exc.args[0]}

    def approve_as_supervisor(self, proposal_id, approver, note, mcp_url=None, supervisor_key=None):
        """Approve using a SEPARATE MCP session owned by the supervisor key.

        The agent's own session is never used for approval. Returns
        (status, payload, supervisor_tool_count).
        """
        if not self.mcp:
            status, payload = self.direct.approve_reroute(proposal_id, approver, note)
            return status, payload, None

        session = McpTools(mcp_url, supervisor_key)
        session.handshake()
        names = session.list_tools()
        tool = McpTools._pick(names, "approve")
        if not tool:
            return "not_found", {"error": "approve_reroute is not visible to the supervisor"}, len(names)
        status, payload = session.call_raw(
            tool,
            {"path_proposal_id": proposal_id, "body": {"approver": approver, "note": note}},
        )
        return status, payload, len(names)

    def list_updates(self):
        if self.mcp:
            return self.mcp.call(self._resolve("updates"), {}).get("updates", [])
        return self.direct.list_updates()


# ------------------------------------------------------------------- LLM picker
def build_prompt(exceptions, candidates_by_shipment):
    """Compact prompt: the closest few candidates per exception, minimal fields.

    Deliberately small so one request fits inside the squad's tokens/minute
    budget - only the top candidates by deadhead miles and the fields the model
    actually needs to choose.
    """
    payload = []
    for exception in exceptions:
        candidates = sorted(
            candidates_by_shipment.get(exception["shipment_id"], []),
            key=lambda candidate: candidate.get("deadhead_miles", 1e9),
        )[:CANDIDATES_PER_EXCEPTION]
        payload.append({
            "shipment_id": exception["shipment_id"],
            "load_type": exception["load_type"],
            "priority": exception["priority"],
            "exception_type": exception["exception_type"],
            "late_hours": exception.get("late_vs_due_hours"),
            "candidates": [
                {
                    "driver_id": candidate["driver_id"],
                    "equipment": candidate["equipment"],
                    "endorsements": candidate["endorsements"],
                    "hours_left": candidate["hours_left"],
                    "deadhead_miles": candidate.get("deadhead_miles"),
                }
                for candidate in candidates
            ],
        })
    return "\n".join([
        "Dispatcher assistant. For each exception pick the single best driver from its candidates.",
        "Match equipment and endorsement, prefer enough HOS and the lowest deadhead miles.",
        "late_hours = ETA minus delivery due in hours (positive = late).",
        "Reply with compact JSON only:",
        '{"picks":[{"shipment_id":"APX-..","new_driver_id":"DRV-..","reason":"under 10 words"}]}',
        json.dumps(payload, separators=(",", ":")),
    ])


def count_prompt_tokens(prompt):
    """Local token count for the request so a rehearsal can show its size.

    Uses tiktoken (o200k_base, what gpt-4o bills against) when available.
    """
    blob = SYSTEM_PROMPT + "\n" + prompt
    try:
        import tiktoken
        return len(tiktoken.get_encoding("o200k_base").encode(blob)) + 8, "tiktoken o200k_base +8 framing"
    except Exception:
        return round(len(blob) / 4) + 8, "chars/4 estimate +8 framing"


def extract_json(text):
    """Pull the first JSON object out of a model reply (tolerating fences)."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def call_llm(proxy_url, api_key, prompt, model=LLM_MODEL):
    """POST the gateway's OpenAI-compatible chat endpoint."""
    url = proxy_url.rstrip("/") + "/v1/chat/completions"
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {"X-API-Key": api_key}
    status, payload, _ = http_json("POST", url, dict(body, response_format={"type": "json_object"}), headers)
    if status == 400:
        # Some gateway routes reject response_format; retry without it.
        status, payload, _ = http_json("POST", url, body, headers)
    if status >= 400:
        raise HttpError(status, json.dumps(payload))
    content = payload["choices"][0]["message"]["content"]
    return extract_json(content), payload.get("usage", {})


def resolve_picks(exceptions, candidates_by_shipment, model_picks):
    """Choose one driver per exception, cheapest valid match first.

    A driver may be used at most once. A model pick is rejected only when it is
    missing, already used, or not one of that exception's candidates; a pick that
    breaks equipment/endorsement rules is still sent to the service, so the
    server's own guardrail holds it (that is intended, and visible in the output).

    Returns a list of (exception, pick, rejected_detail), never skipping one.
    """
    def best_candidate(candidates, used, ignore_used=False):
        pool = [c for c in candidates if ignore_used or c["driver_id"] not in used]
        if not pool:
            return None
        matched = [c for c in pool if c.get("equipment_match") and c.get("endorsement_match")]
        pool = matched or pool
        return min(pool, key=lambda c: (c.get("reroute_cost_usd", 1e9), c.get("deadhead_miles", 1e9)))

    def cheapest_match_cost(exception):
        best = best_candidate(candidates_by_shipment.get(exception["shipment_id"], []), set())
        return best.get("reroute_cost_usd", 1e9) if best else 1e9

    resolved = []
    used = set()
    for exception in sorted(exceptions, key=cheapest_match_cost):
        shipment_id = exception["shipment_id"]
        candidates = candidates_by_shipment.get(shipment_id, [])
        allowed = {candidate["driver_id"] for candidate in candidates}
        model_pick = model_picks.get(shipment_id) or {}
        driver = model_pick.get("new_driver_id")

        reason = None
        if not driver:
            reason = "model returned no pick"
        elif driver in used:
            reason = "already used"
        elif driver not in allowed:
            reason = "not in this exception's candidates"

        if reason is None:
            resolved.append((exception, model_pick, None))
            used.add(driver)
            continue

        fallback = best_candidate(candidates, used) or best_candidate(candidates, used, ignore_used=True)
        if fallback is None:
            print("    %s: model pick %s rejected (%s) and nothing to fall back on"
                  % (shipment_id, driver or "-", reason))
            continue
        print("    %s: model pick %s rejected (%s) -> using %s"
              % (shipment_id, driver or "-", reason, fallback["driver_id"]))
        resolved.append((
            exception,
            {"shipment_id": shipment_id,
             "new_driver_id": fallback["driver_id"],
             "reason": "fallback: %s mi deadhead" % fallback.get("deadhead_miles", 0)},
            "%s -> %s" % (driver or "-", fallback["driver_id"]),
        ))
        used.add(fallback["driver_id"])
    return resolved


def reset_container(base):
    """Restart the dispatch container so a run starts from fresh state."""
    name = os.environ.get("APEX_CONTAINER", "apex-dispatch")
    print("resetting %s so this run starts from fresh state ..." % name)
    if subprocess.call(["docker", "restart", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        print("%scould not restart %s - is Docker running?%s" % (RED, name, RESET))
        return False
    for _ in range(30):
        try:
            urllib.request.urlopen(base.rstrip("/") + "/health", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    print("%s%s did not come back healthy%s" % (RED, name, RESET))
    return False


def ask_supervisor_key():
    """Ask the human operator for the supervisor key, hidden.

    The key is never read from an environment variable or from a file, and is
    never stored - it exists only for the duration of this call. Returns None
    when stdin is not an interactive terminal or nothing was entered, which is
    what makes the approval step skip.
    """
    if not sys.stdin.isatty():
        return None
    try:
        return getpass.getpass("    Dispatcher, paste your supervisor key: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None


# -------------------------------------------------------------------- the flow
def main():
    global _USE_COLOR
    parser = argparse.ArgumentParser(description="ApexTrans exception agent")
    parser.add_argument("--direct", action="store_true", help="tool calls go to the dispatch service, not the gateway")
    parser.add_argument("--base", default=DEFAULT_DIRECT_BASE, help="direct-mode base URL")
    parser.add_argument("--yes", action="store_true",
                        help="non-interactive convenience flag; it never approves, which needs a human key")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("--color", action="store_true", help="force ANSI colour")
    parser.add_argument("--limit", type=int, default=0, help="only handle the first N exceptions (0 = all)")
    parser.add_argument("--model", default=os.environ.get("APEX_LLM_MODEL", "my-gpt-4o"),
                        help="gateway LLM route: my-gpt-4o (default), apex-claude or apex-open")
    parser.add_argument("--reset", action="store_true",
                        help="restart the dispatch container first so the run starts from fresh state")
    args = parser.parse_args()

    if args.no_color:
        _USE_COLOR = False
    elif args.color:
        _USE_COLOR = True

    started = time.time()
    if args.reset:
        reset_container(args.base)
    proxy_url = os.environ.get("KONNECT_PROXY_URL")
    key_dispatch = os.environ.get("KEY_DISPATCH")
    # The supervisor key is never taken from the environment: approvals are human-only.
    supervisor_env_was_set = os.environ.pop("KEY_SUPERVISOR", None) is not None
    gateway = (proxy_url or "http://localhost:8000").rstrip("/")
    mcp_url = os.environ.get("APEX_MCP_URL") or (gateway + "/apex-dispatch")

    if args.direct:
        tools = Tools(direct_base=args.base)
        mode = "DIRECT (pre-gateway): tools -> %s" % args.base
    else:
        tools = Tools(mcp=McpTools(mcp_url, key_dispatch))
        mode = "GATEWAY: MCP -> %s" % mcp_url

    print("%sApexTrans exception agent%s  |  %s" % (BOLD, RESET, mode))
    note("KEY_DISPATCH=%s  KONNECT_PROXY_URL=%s  APEX_MCP_URL=%s"
         % (secret_state("KEY_DISPATCH"), secret_state("KONNECT_PROXY_URL"),
            secret_state("APEX_MCP_URL")))
    if supervisor_env_was_set:
        note("KEY_SUPERVISOR was set in the environment - ignored (approvals are human-only)")

    # 1. which tools can this identity see?
    step(1, "tools/list - what this identity can see")
    if tools.mcp:
        tools.mcp.handshake()
        names = tools.describe()
    else:
        names = tools.describe()
        note("direct mode: no gateway in front, showing the service's own operations")
    for name in names:
        print("    - %s" % name)
    if tools.mcp:
        approves_visible = any("approve" in name.lower() for name in names)
        note("%d tools visible; approve_reroute %s"
             % (len(names), "VISIBLE" if approves_visible else "hidden (expected for the agent)"))
    if not names:
        print("%sNo tools visible - stopping.%s" % (RED, RESET))
        return 2

    # 2. exceptions + candidate drivers
    step(2, "open exceptions and candidate drivers")
    exceptions = tools.list_exceptions()
    if args.limit:
        exceptions = exceptions[: args.limit]
    candidates_by_shipment = {}
    for exception in exceptions:
        drivers = tools.list_available_drivers(exception["shipment_id"])
        # a driver already holding a pending proposal is not really available
        drivers = [d for d in drivers if not d.get("holds_pending_proposal")]
        drivers = sorted(
            drivers,
            key=lambda d: (not d.get("equipment_match", False), not d.get("endorsement_match", False), d.get("reroute_cost_usd", 1e9)),
        )[:5]
        candidates_by_shipment[exception["shipment_id"]] = drivers
        print("    %s  %-7s near %-3s  %2d candidates  %s"
              % (exception["shipment_id"], exception["load_type"], exception["nearest_terminal"], len(drivers), exception["exception_type"]))

    # 3. ask the model to choose
    step(3, "ask %s to choose a driver per exception" % args.model)
    prompt = build_prompt(exceptions, candidates_by_shipment)
    picks = []
    if proxy_url and key_dispatch:
        try:
            decision, usage = call_llm(proxy_url, key_dispatch, prompt, args.model)
            picks = decision.get("picks", []) or []
            note("prompt tokens: %s of the 5000/min budget (total: %s)"
                 % (usage.get("prompt_tokens", "?"), usage.get("total_tokens", "?")))
        except HttpError as exc:
            print("%sLLM call failed via gateway (%s) - using the local allocator%s" % (AMBER, exc.status, RESET))
            tokens, method = count_prompt_tokens(prompt)
            note("the request we were sending is %d tokens (%s); budget 5000/min" % (tokens, method))
            picks = []
    else:
        print("%sKONNECT_PROXY_URL / KEY_DISPATCH not set - using local allocator (offline rehearsal)%s" % (AMBER, RESET))
        tokens, method = count_prompt_tokens(prompt)
        note("prompt is %d tokens (%s); budget 5000/min" % (tokens, method))

    # validate the model's picks: one driver per load, cheapest valid match first
    model_picks = {p.get("shipment_id"): p for p in picks if p.get("shipment_id")}
    if not model_picks:
        note("no model picks to validate - using the best valid candidate per exception")
    resolved = resolve_picks(exceptions, candidates_by_shipment, model_picks)

    # 4. call the reroute tool
    step(4, "call the reroute tool")
    auto, held = [], []
    for exception, pick, _rejection in resolved:
        proposal = tools.create_reroute(
            exception["shipment_id"], pick["new_driver_id"], pick.get("reason") or "exception recovery"
        )
        verdict = proposal["decision"]
        if verdict == "auto_applied":
            auto.append(proposal)
            print("    %sAUTO-APPLIED%s %s -> %s  $%.2f  ETA %s"
                  % (GREEN, RESET, proposal["shipment_id"], proposal["new_driver_id"],
                     proposal["reroute_cost_usd"], proposal["new_eta_utc"]))
            note(pick.get("reason", ""))
        else:
            held.append(proposal)
            print("    %sHELD FOR HUMAN%s %s -> %s  $%.2f"
                  % (AMBER, RESET, proposal["shipment_id"], proposal["new_driver_id"], proposal["reroute_cost_usd"]))
            note(pick.get("reason", ""))
            for conflict in proposal["conflicts"]:
                print("        %s! %s%s" % (AMBER, conflict, RESET))

    # 5. the agent tries to approve by itself
    step(5, "agent attempts to approve a held proposal (must be blocked by the gateway)")
    if held:
        target = held[0]["proposal_id"]
        if tools.mcp:
            status, payload = tools.try_approve_as_agent(target)
            if status == "not_found":
                print("    %stool not found - approve_reroute is not exposed to this identity%s" % (RED, RESET))
                print("        %s%s%s" % (RED, payload.get("error", ""), RESET))
            elif status == 403:
                print("    %s403 Forbidden - the gateway blocked the agent from approving %s%s" % (RED, target, RESET))
                print("        %s%s%s" % (RED, payload.get("error", ""), RESET))
            else:
                print("    %sunexpected: approve returned %s %s%s" % (RED, status, payload, RESET))
        else:
            note("direct mode: no gateway in front of the service, so there is nothing to return 403")
            note("(once wired, this same call is expected to come back 403 from Kong)")
    else:
        note("no held proposals, nothing to try")

    # 6. supervisor approval - human only; the key is pasted at the prompt
    step(6, "supervisor approval (human only)")
    if not held:
        note("no held proposals to approve")
    else:
        target = held[0]["proposal_id"]
        shipment = held[0]["shipment_id"]
        if args.yes:
            note("--yes cannot approve: the approval still needs the human key")
        supervisor_key = ask_supervisor_key()
        if not supervisor_key:
            print("    %sapproval requires a human - skipped%s" % (AMBER, RESET))
        else:
            status, payload, tool_count = tools.approve_as_supervisor(
                target,
                approver="dispatch-supervisor",
                note="Dispatcher approved %s for %s" % (target, shipment),
                mcp_url=mcp_url,
                supervisor_key=supervisor_key,
            )
            if not tools.mcp:
                note("direct mode: one local service, so no separate supervisor session here")
            else:
                note("separate supervisor MCP session; supervisor tool count: %s" % tool_count)
            if status == "not_found":
                print("    %sapprove_reroute is not in the supervisor's tool list%s" % (RED, RESET))
            elif isinstance(status, int) and status < 400:
                print("    %sAPPLIED%s %s by dispatch-supervisor" % (GREEN, RESET, target))
            else:
                print("    %sKong refused the approval: %s%s" % (RED, status, RESET))
            print("    Kong result: %s" % json.dumps(payload, separators=(",", ":"))[:400])

    # 7. updates feed
    step(7, "applied changes (/updates, newest first)")
    for entry in tools.list_updates():
        print("    %s%s%s" % (DIM, entry["ts_utc"], RESET))
        print("      %s" % entry["message"])

    elapsed = time.time() - started
    print("\n%s%2d auto-applied, %d held for human; finished in %.1fs%s"
          % (BOLD, len(auto), len(held), elapsed, RESET))
    return 0


if __name__ == "__main__":
    sys.exit(main())
