#!/usr/bin/env python3
"""Check each squad sees the right number of dispatch tools.

Bryan runs this after applying kong/apex-dispatch-mcp.yaml. It opens one MCP
session per squad, lists that squad's tools, and compares the count with what
the ACLs in the YAML promise.

    python3 kong/check_tool_access.py

Keys come from the environment only - nothing is read from a file:

    KONNECT_PROXY_URL   gateway base URL (default http://localhost:8000)
    APEX_MCP_URL        MCP endpoint (default $KONNECT_PROXY_URL/apex-dispatch)
    KEY_SUPPORT         support-bot
    KEY_DISPATCH        dispatch-copilot
    KEY_COMPLIANCE      compliance-docs
    KEY_SUPERVISOR      dispatch-supervisor

Exits non-zero if any squad's count is wrong, so it can gate a demo.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

PROXY = (os.environ.get("KONNECT_PROXY_URL") or "http://localhost:8000").rstrip("/")
MCP_URL = os.environ.get("APEX_MCP_URL") or (PROXY + "/apex-dispatch")
TIMEOUT = int(os.environ.get("APEX_HTTP_TIMEOUT", "30"))

# Identity -> (key env var, expected tool count). The counts match the explicit
# access.acls.allow lists in kong/apex-dispatch-mcp.yaml.
SQUADS = [
    ("support-bot", "KEY_SUPPORT", 2),
    ("dispatch-copilot", "KEY_DISPATCH", 7),
    ("compliance-docs", "KEY_COMPLIANCE", 2),
    ("dispatch-supervisor", "KEY_SUPERVISOR", 8),
]

# Tools each identity must NOT be able to see, as a second pair of eyes on the ACLs.
MUST_NOT_SEE = {
    "support-bot": ["create_reroute", "approve_reroute"],
    "dispatch-copilot": ["approve_reroute"],
    "compliance-docs": ["create_reroute", "approve_reroute"],
    "dispatch-supervisor": [],
}


def post(payload, key, session_id):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(MCP_URL, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    if key:
        request.add_header("X-API-Key", key)
    if session_id:
        request.add_header("Mcp-Session-Id", session_id)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", "replace"), \
                response.headers.get("Content-Type", ""), response.headers.get("Mcp-Session-Id")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), \
            exc.headers.get("Content-Type", ""), exc.headers.get("Mcp-Session-Id")


def parse(raw, ctype):
    if "text/event-stream" in (ctype or ""):
        for line in reversed(raw.splitlines()):
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk and chunk != "[DONE]":
                    try:
                        return json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
        return {}
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"raw": raw}


def rpc(method, params, key, session_id, notify=False, rid=1):
    payload = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if not notify:
        payload["id"] = rid
    status, raw, ctype, new_session = post(payload, key, session_id)
    return status, parse(raw, ctype), (new_session or session_id)


def tools_for(identity, key):
    """Handshake, then return this identity's visible tool names."""
    status, body, session = rpc("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "check-tool-access", "version": "1.0"},
    }, key, None)
    if status >= 400:
        raise RuntimeError("initialize -> HTTP %s: %s" % (status, json.dumps(body)[:200]))
    rpc("notifications/initialized", None, key, session, notify=True, rid=2)
    status, body, session = rpc("tools/list", {}, key, session, rid=3)
    if status >= 400:
        raise RuntimeError("tools/list -> HTTP %s: %s" % (status, json.dumps(body)[:200]))
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError("tools/list -> %s" % body["error"])
    result = (body or {}).get("result") or {}
    return [t.get("name") for t in result.get("tools", []) if t.get("name")]


def main():
    print("MCP endpoint: %s" % MCP_URL)
    print("expected: support-bot 2, dispatch-copilot 7, compliance-docs 2, dispatch-supervisor 8\n")
    failures = 0
    for identity, key_name, expected in SQUADS:
        key = os.environ.get(key_name)
        if not key:
            print("SKIP  %-20s %s is not set" % (identity, key_name))
            failures += 1
            continue
        try:
            names = tools_for(identity, key)
        except Exception as exc:
            print("FAIL  %-20s %s" % (identity, exc))
            failures += 1
            continue

        ok = len(names) == expected
        forbidden = [t for t in MUST_NOT_SEE.get(identity, []) if t in names]
        verdict = "OK  " if (ok and not forbidden) else "FAIL"
        if not ok or forbidden:
            failures += 1
        print("%s  %-20s %d of %d tools" % (verdict, identity, len(names), expected))
        if forbidden:
            print("        must not see: %s" % ", ".join(forbidden))
        print("        %s" % ", ".join(sorted(names)) if names else "        (none)")

    print()
    if failures:
        print("%d squad(s) need attention." % failures)
        return 1
    print("All four squads see exactly what the ACLs promise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
