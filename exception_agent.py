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
    python3 exception_agent.py --direct          # tools -> localhost:8090
    python3 exception_agent.py                   # everything through Kong
    python3 exception_agent.py --yes             # auto-answer the approval prompt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_DIRECT_BASE = os.environ.get("DISPATCH_BASE_URL", "http://localhost:8090")
LLM_MODEL = os.environ.get("APEX_LLM_MODEL", "my-gpt-4o")
REQUEST_TIMEOUT = int(os.environ.get("APEX_HTTP_TIMEOUT", "30"))

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

    def list_exceptions(self):
        if self.mcp:
            return self.mcp.call(self._resolve("exceptions"), {}).get("exceptions", [])
        return self.direct.list_exceptions()

    def list_available_drivers(self, shipment_id):
        if self.mcp:
            payload = self.mcp.call(self._resolve("driver"), {"shipment_id": shipment_id})
            return payload.get("drivers", [])
        return self.direct.list_available_drivers(shipment_id)

    def create_reroute(self, shipment_id, new_driver_id, reason):
        if self.mcp:
            return self.mcp.call(
                self._resolve("create_reroute", "reroute"),
                {"shipment_id": shipment_id, "new_driver_id": new_driver_id, "reason": reason, "requested_by": "exception-agent"},
            )
        return self.direct.create_reroute(shipment_id, new_driver_id, reason)

    def try_approve_as_agent(self, proposal_id):
        """Step 5: the agent tries to approve. Through Kong this must be 403."""
        if not self.mcp:
            return None
        try:
            self.mcp.call(
                self._resolve("approve"),
                {"proposal_id": proposal_id, "approver": "exception-agent", "note": "agent attempting self-approval"},
            )
            return 200, {"note": "gateway allowed the agent to approve (unexpected)"}
        except HttpError as exc:
            return exc.status, {"error": exc.args[0]}

    def approve(self, proposal_id, approver, note):
        if self.mcp:
            return 200, self.mcp.call(
                self._resolve("approve"), {"proposal_id": proposal_id, "approver": approver, "note": note}
            )
        return self.direct.approve_reroute(proposal_id, approver, note)

    def list_updates(self):
        if self.mcp:
            return self.mcp.call(self._resolve("updates"), {}).get("updates", [])
        return self.direct.list_updates()


# ------------------------------------------------------------------- LLM picker
def build_prompt(exceptions, candidates_by_shipment):
    lines = [
        "You are a dispatcher assistant for ApexTrans.",
        "For each exception choose the single best available driver.",
        "Prefer drivers that match the equipment and endorsement, have enough HOS for",
        "deadhead_miles + remaining_miles at 50 mph, are closest (lowest cost), and can still",
        "meet delivery_due_utc. If no driver is a clean fit, still pick the least-bad option.",
        "Reply with JSON only, shape:",
        '{"picks":[{"shipment_id":"APX-...","new_driver_id":"DRV-....","reason":"one sentence"}]}',
        "",
        json.dumps(
            [
                {
                    "exception": {
                        "shipment_id": exception["shipment_id"],
                        "load_type": exception["load_type"],
                        "priority": exception["priority"],
                        "nearest_terminal": exception["nearest_terminal"],
                        "remaining_miles": exception["remaining_miles"],
                        "delivery_due_utc": exception["delivery_due_utc"],
                        "eta_utc": exception["eta_utc"],
                        "exception_type": exception["exception_type"],
                    },
                    "candidates": candidates_by_shipment[exception["shipment_id"]],
                }
                for exception in exceptions
            ],
            indent=1,
        ),
    ]
    return "\n".join(lines)


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


def call_llm(proxy_url, api_key, prompt):
    """POST the gateway's OpenAI-compatible chat endpoint."""
    url = proxy_url.rstrip("/") + "/v1/chat/completions"
    body = {
        "model": LLM_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "You are a precise freight dispatcher. Reply with JSON only."},
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


def allocate(exceptions, candidates_by_shipment, reserved=None):
    """Global greedy allocation: the best-matched, cheapest driver per exception.

    Candidate pairs are ranked (hard-constraint match, HOS coverage, cost) and
    assigned cheapest-first, so one driver is never promised to two loads.
    """
    used = set(reserved or ())
    ranked = []
    for exception in exceptions:
        shipment_id = exception["shipment_id"]
        for candidate in candidates_by_shipment.get(shipment_id, []):
            if candidate.get("holds_pending_proposal"):
                continue
            matched = bool(candidate.get("equipment_match")) and bool(candidate.get("endorsement_match"))
            hos_ok = candidate["hos_hours_left"] >= candidate.get("transit_hours_needed", 0)
            rank = (0 if matched else 1, 0 if hos_ok else 1, candidate.get("reroute_cost_usd", 1e9))
            ranked.append((rank, shipment_id, candidate))
    ranked.sort(key=lambda item: item[0])

    picks = {}
    for _, shipment_id, candidate in ranked:
        if shipment_id in picks or candidate["driver_id"] in used:
            continue
        matched = bool(candidate.get("equipment_match")) and bool(candidate.get("endorsement_match"))
        picks[shipment_id] = {
            "shipment_id": shipment_id,
            "new_driver_id": candidate["driver_id"],
            "reason": "allocated %s - %s mi deadhead, $%.0f, %s"
            % (candidate["equipment"], candidate.get("deadhead_miles", 0),
               candidate.get("reroute_cost_usd", 0),
               "meets equipment/endorsement" if matched else "least-bad available"),
        }
        used.add(candidate["driver_id"])
    return picks


# -------------------------------------------------------------------- the flow
def main():
    global _USE_COLOR
    parser = argparse.ArgumentParser(description="ApexTrans exception agent")
    parser.add_argument("--direct", action="store_true", help="tool calls go to the dispatch service, not the gateway")
    parser.add_argument("--base", default=DEFAULT_DIRECT_BASE, help="direct-mode base URL")
    parser.add_argument("--yes", action="store_true", help="auto-answer the approval prompt with y")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("--color", action="store_true", help="force ANSI colour")
    parser.add_argument("--limit", type=int, default=0, help="only handle the first N exceptions (0 = all)")
    args = parser.parse_args()

    if args.no_color:
        _USE_COLOR = False
    elif args.color:
        _USE_COLOR = True

    started = time.time()
    proxy_url = os.environ.get("KONNECT_PROXY_URL")
    key_dispatch = os.environ.get("KEY_DISPATCH")
    key_supervisor = os.environ.get("KEY_SUPERVISOR")
    mcp_url = os.environ.get("APEX_MCP_URL")

    if args.direct:
        tools = Tools(direct_base=args.base)
        mode = "DIRECT (pre-gateway): tools -> %s" % args.base
    else:
        if not mcp_url:
            print("%sAPEX_MCP_URL is not set - cannot reach the gateway MCP route.%s" % (RED, RESET))
            return 2
        tools = Tools(mcp=McpTools(mcp_url, key_dispatch))
        mode = "GATEWAY: MCP -> %s" % mcp_url

    print("%sApexTrans exception agent%s  |  %s" % (BOLD, RESET, mode))
    note("KEY_DISPATCH=%s  KEY_SUPERVISOR=%s  KONNECT_PROXY_URL=%s  APEX_MCP_URL=%s"
         % (secret_state("KEY_DISPATCH"), secret_state("KEY_SUPERVISOR"),
            secret_state("KONNECT_PROXY_URL"), secret_state("APEX_MCP_URL")))

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
    step(3, "ask %s to choose a driver per exception" % LLM_MODEL)
    prompt = build_prompt(exceptions, candidates_by_shipment)
    allocated = allocate(exceptions, candidates_by_shipment)
    picks = []
    if proxy_url and key_dispatch:
        try:
            decision, usage = call_llm(proxy_url, key_dispatch, prompt)
            picks = decision.get("picks", []) or []
            note("model replied (tokens: %s)" % usage.get("total_tokens", "?"))
        except HttpError as exc:
            print("%sLLM call failed via gateway (%s) - using the local allocator%s" % (AMBER, exc.status, RESET))
            picks = []
    else:
        print("%sKONNECT_PROXY_URL / KEY_DISPATCH not set - using local allocator (offline rehearsal)%s" % (AMBER, RESET))

    # A driver can only take one load: keep the model's pick while the driver is
    # still free, otherwise fall back to the global allocation for that shipment.
    model_picks = {p.get("shipment_id"): p for p in picks if p.get("shipment_id")}
    by_shipment, used = {}, set()
    for exception in exceptions:
        shipment_id = exception["shipment_id"]
        model_pick = model_picks.get(shipment_id)
        if model_pick and model_pick.get("new_driver_id") and model_pick["new_driver_id"] not in used:
            by_shipment[shipment_id] = model_pick
            used.add(model_pick["new_driver_id"])
            continue
        if model_pick:
            note("%s: model picked %s, already assigned - using allocated driver"
                 % (shipment_id, model_pick.get("new_driver_id")))
        fallback = allocated.get(shipment_id)
        if fallback and fallback["new_driver_id"] not in used:
            by_shipment[shipment_id] = fallback
            used.add(fallback["new_driver_id"])

    # 4. call the reroute tool
    step(4, "call the reroute tool")
    auto, held = [], []
    for exception in exceptions:
        pick = by_shipment.get(exception["shipment_id"])
        if not pick:
            note("%s - no pick returned by the model" % exception["shipment_id"])
            continue
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
            if status == 403:
                print("    %s403 Forbidden - the gateway blocked the agent from approving %s%s" % (RED, target, RESET))
                print("        %s%s%s" % (RED, payload.get("error", ""), RESET))
            else:
                print("    %sunexpected: approve returned %s %s%s" % (RED, status, payload, RESET))
        else:
            note("direct mode: no gateway in front of the service, so there is nothing to return 403")
            note("(once wired, this same call is expected to come back 403 from Kong)")
    else:
        note("no held proposals, nothing to try")

    # 6. supervisor approval
    step(6, "supervisor approval")
    if held:
        target = held[0]["proposal_id"]
        shipment = held[0]["shipment_id"]
        answer = "y" if args.yes else input("    Dispatcher, approve %s? [y/N] " % target).strip().lower()
        if answer in ("y", "yes"):
            status, payload = tools.approve(
                target,
                approver="dispatch-supervisor",
                note="Dispatcher approved %s for %s" % (target, shipment),
            )
            if status < 400:
                print("    %sAPPLIED%s %s  by %s" % (GREEN, RESET, target, "dispatch-supervisor"))
            else:
                print("    %sapprove failed: %s %s%s" % (RED, status, payload, RESET))
            if not tools.mcp:
                note("direct mode: no X-API-Key needed locally; through Kong this uses KEY_SUPERVISOR")
            else:
                note("KEY_SUPERVISOR=%s (used for this call)" % secret_state("KEY_SUPERVISOR"))
        else:
            note("not approved - %s stays held" % target)
    else:
        note("no held proposals to approve")

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
