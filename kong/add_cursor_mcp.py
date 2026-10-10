#!/usr/bin/env python3
"""Register the apex-dispatch MCP server with Cursor   (Bryan runs this, not Bentley).

Reads KEY_DISPATCH out of ~/.kong/apextrans-squad-keys.env and adds an
`apex-dispatch` entry to ~/.cursor/mcp.json pointing at the gateway:

    <KONNECT_PROXY_URL or http://localhost:8000>/apex-dispatch

The key value is never printed - only the names of the servers that end up in
the file. Existing servers (firecrawl-mcp, kong-konnect, Origin, ...) are kept.

    python3 kong/add_cursor_mcp.py             # back up, then write
    python3 kong/add_cursor_mcp.py --dry-run   # show what would change

Safety:
  * backs ~/.cursor/mcp.json up to ~/.cursor/mcp.json.bak before writing
  * writes the result with mode 600
  * only ever prints server names
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

KEYS_FILE = Path.home() / ".kong" / "apextrans-squad-keys.env"
CURSOR_DIR = Path.home() / ".cursor"
MCP_FILE = CURSOR_DIR / "mcp.json"
BACKUP_FILE = CURSOR_DIR / "mcp.json.bak"
SERVER_NAME = "apex-dispatch"
KEY_NAME = "KEY_DISPATCH"
DEFAULT_PROXY = "http://localhost:8000"


def read_env_file(path):
    """Parse `export NAME=value` lines. Values are handled but never printed."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def gateway_url():
    base = (os.environ.get("KONNECT_PROXY_URL") or DEFAULT_PROXY).rstrip("/")
    return base + "/apex-dispatch"


def load_config():
    if not MCP_FILE.exists():
        return {}
    raw = MCP_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        print("ERROR: %s is not valid JSON (%s) - leaving it alone" % (MCP_FILE, exc))
        raise SystemExit(1)
    if not isinstance(config, dict):
        print("ERROR: %s is not a JSON object - leaving it alone" % MCP_FILE)
        raise SystemExit(1)
    return config


def main():
    parser = argparse.ArgumentParser(description="Add the apex-dispatch MCP server to Cursor")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    args = parser.parse_args()

    if not KEYS_FILE.exists():
        print("ERROR: keys file not found at %s" % KEYS_FILE)
        return 1

    key = read_env_file(KEYS_FILE).get(KEY_NAME)
    if not key:
        print("ERROR: %s not found in %s" % (KEY_NAME, KEYS_FILE))
        return 1

    config = load_config()
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        config["mcpServers"] = servers

    existing = sorted(servers)
    servers[SERVER_NAME] = {
        "url": gateway_url(),
        "headers": {"X-API-Key": key},
    }
    saved = sorted(servers)

    if args.dry_run:
        print("dry run - would save servers: %s" % ", ".join(saved))
        return 0

    CURSOR_DIR.mkdir(parents=True, exist_ok=True)
    if MCP_FILE.exists():
        shutil.copy2(MCP_FILE, BACKUP_FILE)

    MCP_FILE.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.chmod(MCP_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 600

    print("saved servers: %s" % ", ".join(saved))
    if existing:
        print("kept: %s" % ", ".join(existing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
