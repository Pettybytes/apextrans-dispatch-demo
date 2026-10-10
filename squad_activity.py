#!/usr/bin/env python3
"""Squad activity checks for the ApexTrans demo.

Bryan runs this, not the assistant. It acts as a *different* Kong squad in each
mode so you can watch the gateway apply that squad's token limit and tool
visibility, and so the board's live feed picks up the activity.

    python3 squad_activity.py --support APX-2610149
        Acts as support-bot. MCP get_shipment + list_updates, then prints the
        customer-facing status message. No LLM call - support-bot only has 100
        tokens/min.

    python3 squad_activity.py --compliance
        Acts as compliance-docs. MCP list_audit, then one LLM call through Kong
        (my-gpt-4o, max_tokens 250) for a five-line compliance summary.

Keys come from the environment only - nothing is read from a file:

    KONNECT_PROXY_URL   gateway base URL (default http://localhost:8000)
    APEX_MCP_URL        MCP endpoint (default $KONNECT_PROXY_URL/apex-dispatch)
    KEY_SUPPORT         the support-bot squad key
    KEY_COMPLIANCE      the compliance-docs squad key

Both modes print the identity they ran as and the tool count Kong showed them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PROXY = (os.environ.get("KONNECT_PROXY_URL") or "http://localhost:8000").rstrip("/")
MCP_URL = os.environ.get("APEX_MCP_URL") or (PROXY + "/apex-dispatch")
MODEL = os.environ.get("APEX_LLM_MODEL", "my-gpt-4o")
TIMEOUT = int(os.environ.get("APEX_HTTP_TIMEOUT", "30"))
COMPLIANCE_MAX_TOKENS = 250
COMPLIANCE_PROMPT_TOKEN_BUDGET = 1200

SQUADS = {
    "support": ("support-bot", "KEY_SUPPORT"),
    "compliance": ("compliance-docs", "KEY_COMPLIANCE"),
}


class McpError(Exception):
    pass


def http_json(method, url, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    for key, value in (headers or {}).items():
        if value:
            request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


class Mcp:
    """Minimal MCP-over-HTTP client, one session per squad."""

    def __init__(self, identity, key):
        self.identity = identity
        self.key = key
        self.session_id = None
        self._id = 0

    def _post(self, payload):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(MCP_URL, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json, text/event-stream")
        if self.key:
            request.add_header("X-API-Key", self.key)
        if self.session_id:
            request.add_header("Mcp-Session-Id", self.session_id)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                status = response.status
                raw = response.read().decode("utf-8", "replace")
                ctype = response.headers.get("Content-Type", "")
                session = response.headers.get("Mcp-Session-Id")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", "replace")
            ctype = exc.headers.get("Content-Type", "")
            session = exc.headers.get("Mcp-Session-Id")
        if session:
            self.session_id = session
        if "text/event-stream" in (ctype or ""):
            for line in reversed(raw.splitlines()):
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            return status, json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
            return status, {}
        try:
            return status, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return status, {"raw": raw}

    def rpc(self, method, params=None, notify=False):
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self._id
        status, message = self._post(payload)
        if notify:
            return None
        if status >= 400:
            raise McpError("%s -> HTTP %s: %s" % (method, status, json.dumps(message)[:200]))
        if isinstance(message, dict) and message.get("error"):
            raise McpError("%s -> %s" % (method, message["error"]))
        return (message or {}).get("result")

    def handshake(self):
        self.rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "squad-activity", "version": "1.0"},
        })
        self.rpc("notifications/initialized", notify=True)

    def tools(self):
        result = self.rpc("tools/list", {}) or {}
        return [t.get("name") for t in result.get("tools", []) if t.get("name")]

    def call(self, name, arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments}) or {}
        for item in result.get("content", []):
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    return {"text": item["text"]}
        return result


def pick(names, *keywords):
    for keyword in keywords:
        for name in names:
            if keyword in name.lower():
                return name
    return None


def count_tokens(text):
    try:
        import tiktoken
        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        return round(len(text) / 4)


def banner(identity, names, expect=2):
    print("identity: %s" % identity)
    print("tools Kong showed this identity: %d (expected %d)" % (len(names), expect))
    for name in names:
        print("   - %s" % name)


def run_support(shipment_id):
    identity, key_name = SQUADS["support"]
    key = os.environ.get(key_name)
    if not key:
        print("%s is not set - export it first (nothing is read from a file)." % key_name)
        return 2

    mcp = Mcp(identity, key)
    mcp.handshake()
    names = mcp.tools()
    banner(identity, names)

    shipment_tool = pick(names, "shipment")
    updates_tool = pick(names, "updates")
    if not shipment_tool or not updates_tool:
        print("This identity cannot see both get_shipment and list_updates.")
        return 1

    shipment = mcp.call(shipment_tool, {"path_shipment_id": shipment_id})
    updates = mcp.call(updates_tool, {}) or {}
    print("\n%s" % shipment_id)
    print("   customer: %s" % shipment.get("customer", "unknown"))
    print("   status:   %s" % shipment.get("status", "unknown"))

    messages = [u for u in updates.get("updates", []) if u.get("shipment_id") == shipment_id]
    if messages:
        print("\ncustomer-facing status message:")
        print("   %s" % messages[0].get("message", ""))
    else:
        print("\nNo applied change for this shipment yet, so there is no customer message.")
    return 0


def build_compliance_prompt(audit):
    """Compact digest of the audit log, kept well inside the token budget."""
    created = [a for a in audit if a.get("event") == "proposal.created"]
    approved = [a for a in audit if a.get("event") == "proposal.approved"]
    applied = [a for a in audit if a.get("event") == "shipment.rerouted"]
    blocked = [a for a in audit if a.get("event") == "write.blocked"]
    reads = [a for a in audit if a.get("event") == "read.lookup"]

    auto = [a for a in created if a.get("decision") == "auto_applied"]
    waiting = [a for a in created if a.get("decision") == "pending_human"]

    lines = [
        "decisions logged: %d" % len(created),
        "auto-applied by the rules engine: %d" % len(auto),
        "waiting for a dispatcher: %d" % len(waiting),
        "approvals by a human: %d" % len(approved),
        "applied changes: %d" % len(applied),
        "blocked writes: %d" % len(blocked),
        "attributed reads: %d" % len(reads),
    ]
    for a in approved[:10]:
        lines.append("approved %s by %s" % (a.get("proposal_id"), a.get("approver")))
    for a in blocked[:10]:
        lines.append("blocked write from %s to %s" % (a.get("client_ip"), a.get("path")))

    prompt = "\n".join([
        "You are the compliance reviewer for ApexTrans Logistics.",
        "Write exactly five short lines summarising the dispatch audit below.",
        "Cover: decisions today; auto-applied vs waiting for a dispatcher; who approved;",
        "any blocked writes. Plain sentences, no headings, no bullet characters.",
        "",
        "\n".join(lines),
    ])
    return prompt


def run_compliance():
    identity, key_name = SQUADS["compliance"]
    key = os.environ.get(key_name)
    if not key:
        print("%s is not set - export it first (nothing is read from a file)." % key_name)
        return 2

    mcp = Mcp(identity, key)
    mcp.handshake()
    names = mcp.tools()
    banner(identity, names)

    audit_tool = pick(names, "audit")
    if not audit_tool:
        print("This identity cannot see list_audit.")
        return 1
    audit = (mcp.call(audit_tool, {}) or {}).get("audit", [])

    prompt = build_compliance_prompt(audit)
    tokens = count_tokens(prompt)
    print("\naudit entries read: %d" % len(audit))
    print("summary prompt: %d tokens (budget %d)" % (tokens, COMPLIANCE_PROMPT_TOKEN_BUDGET))
    if tokens > COMPLIANCE_PROMPT_TOKEN_BUDGET:
        print("prompt is over budget - stopping rather than burning the squad's limit.")
        return 1

    status, payload = http_json(
        "POST",
        PROXY + "/v1/chat/completions",
        {"model": MODEL, "temperature": 0, "max_tokens": COMPLIANCE_MAX_TOKENS,
         "messages": [{"role": "user", "content": prompt}]},
        {"X-API-Key": key},
    )
    if status >= 400:
        print("LLM call failed: HTTP %s %s" % (status, json.dumps(payload)[:200]))
        return 1
    print("\ncompliance summary:\n")
    print(payload["choices"][0]["message"]["content"].strip())
    return 0


def main():
    parser = argparse.ArgumentParser(description="ApexTrans squad activity checks")
    parser.add_argument("--support", metavar="SHIPMENT_ID", help="act as support-bot and look up one shipment")
    parser.add_argument("--compliance", action="store_true", help="act as compliance-docs and summarise the audit")
    args = parser.parse_args()

    if args.support and args.compliance:
        parser.error("pick one mode")
    if args.support:
        return run_support(args.support)
    if args.compliance:
        return run_compliance()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
