#!/usr/bin/env python3
"""Check each squad sees the right number of dispatch tools.

Bryan runs this after applying kong/apex-dispatch-mcp.yaml. It opens one MCP
session per squad, lists that squad's tools, and compares the count with what
the ACLs in the YAML promise.

    python3 kong/check_tool_access.py

Keys:
  * support-bot, dispatch-copilot and compliance-docs keys come from the
    environment: KEY_SUPPORT, KEY_DISPATCH, KEY_COMPLIANCE.
  * There is NO supervisor key in the environment. The dispatch-supervisor key
    is human-only, so it is asked for at a hidden prompt and never read from a
    file or an env var. Leave it blank to skip that squad.

Uses KONNECT_PROXY_URL (default http://localhost:8000) and APEX_MCP_URL
(default $KONNECT_PROXY_URL/apex-dispatch).

A skipped squad is not a failure. Only a wrong tool count, a tool the identity
should not see, or an ACL that lets a squad through exits non-zero.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.error
import urllib.request

PROXY = (os.environ.get("KONNECT_PROXY_URL") or "http://localhost:8000").rstrip("/")
MCP_URL = os.environ.get("APEX_MCP_URL") or (PROXY + "/apex-dispatch")
TIMEOUT = int(os.environ.get("APEX_HTTP_TIMEOUT", "30"))

# (identity, key env var or None, expected tool count, where the key comes from)
SQUADS = [
    ("support-bot", "KEY_SUPPORT", 2, "env"),
    ("dispatch-copilot", "KEY_DISPATCH", 7, "env"),
    ("compliance-docs", "KEY_COMPLIANCE", 2, "env"),
    ("dispatch-supervisor", None, 8, "prompt"),
]

# Tools each identity must NOT be able to see, as a second pair of eyes on the ACLs.
MUST_NOT_SEE = {
    "support-bot": ["create_reroute", "approve_reroute", "list_audit"],
    "dispatch-copilot": ["approve_reroute"],
    "compliance-docs": ["create_reroute", "approve_reroute", "list_updates"],
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


def open_session(key):
    """Handshake once and return (session_id, tool names)."""
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
    return session, [t.get("name") for t in result.get("tools", []) if t.get("name")]


def print_refusal(identity, key, session, tool, arguments):
    """Ask for a tool this identity must not have, and print Kong's answer.

    Returns True when Kong refused, which is what we want to see.
    """
    status, body, _ = rpc("tools/call", {"name": tool, "arguments": arguments}, key, session, rid=4)
    detail = json.dumps(body)[:180] if isinstance(body, (dict, list)) else str(body)[:180]
    if status >= 400:
        print("        Kong refused %s for %s: HTTP %s %s" % (tool, identity, status, detail))
        return True
    print("        *** ACL HOLE: %s was allowed to call %s - %s ***" % (identity, tool, detail))
    return False


def resolve_key(identity, key_name, source):
    """Return (key, skip_reason). A blank reason means we have a key."""
    if source == "prompt":
        # The supervisor key is human-only: never read from the environment.
        if not sys.stdin.isatty():
            return None, "human key not entered"
        try:
            entered = getpass.getpass("    Paste the %s key (blank to skip): " % identity).strip()
        except (EOFError, KeyboardInterrupt):
            return None, "human key not entered"
        if not entered:
            return None, "human key not entered"
        return entered, ""
    key = os.environ.get(key_name or "")
    if not key:
        return None, "%s is not set" % key_name
    return key, ""


def main():
    print("MCP endpoint: %s" % MCP_URL)
    print("expected: support-bot 2, dispatch-copilot 7, compliance-docs 2, dispatch-supervisor 8\n")

    checked = passed = skipped = failed = 0
    skip_reasons = []

    for identity, key_name, expected, source in SQUADS:
        key, reason = resolve_key(identity, key_name, source)
        if not key:
            print("SKIP  %-20s %s" % (identity, reason))
            skipped += 1
            skip_reasons.append(reason)
            continue

        checked += 1
        try:
            session, names = open_session(key)
        except Exception as exc:
            print("FAIL  %-20s %s" % (identity, exc))
            failed += 1
            continue

        forbidden = [t for t in MUST_NOT_SEE.get(identity, []) if t in names]
        squad_ok = len(names) == expected and not forbidden
        print("%s  %-20s %d of %d tools" % ("OK  " if squad_ok else "FAIL", identity, len(names), expected))
        if forbidden:
            print("        must not see: %s" % ", ".join(forbidden))
        print("        %s" % (", ".join(sorted(names)) if names else "(none)"))

        if identity == "support-bot":
            refused = print_refusal(identity, key, session, "create_reroute", {
                "body": {"shipment_id": "APX-2610149", "new_driver_id": "DRV-1001",
                         "reason": "acl check", "requested_by": "check_tool_access"},
            })
            squad_ok = squad_ok and refused

        if squad_ok:
            passed += 1
        else:
            failed += 1

    tail = ""
    if skipped:
        tail = " (%s)" % "; ".join(dict.fromkeys(skip_reasons))
    print("\n%d checked, %d OK, %d skipped%s" % (checked, passed, skipped, tail))

    if failed:
        print("%d squad(s) need attention." % failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
