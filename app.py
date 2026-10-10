"""apex-dispatch REST service.

Standard-library HTTP server (no third-party dependencies) exposing the
ApexTrans dispatch data and reroute rules as JSON endpoints. Kong AI Gateway
turns these endpoints into MCP tools; this service knows nothing about Kong.

Run:  python3 app.py            (listens on 0.0.0.0:8090)
"""

from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from dispatch_core import Dispatch, DispatchError, HERE

PORT = int(os.environ.get("PORT", "8090"))
HOST = os.environ.get("HOST", "0.0.0.0")
OPENAPI_PATH = os.path.join(HERE, "openapi.json")
STATIC_DIR = os.path.join(HERE, "static")
CONFIG_DIR = os.path.join(HERE, "config")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ico": "image/x-icon",
}

DISPATCH = Dispatch()

SHIPMENT_RE = re.compile(r"^/shipments/(?P<shipment_id>[^/]+)$")
APPROVE_RE = re.compile(r"^/reroutes/(?P<proposal_id>[^/]+)/approve$")


class Handler(BaseHTTPRequestHandler):
    server_version = "apex-dispatch/1.0"
    protocol_version = "HTTP/1.1"

    # ---- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the demo output tidy
        sys.stderr.write("[dispatch] %s\n" % (fmt % args))

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, indent=2, sort_keys=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=400):
        self._send_json({"error": message}, status=status)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DispatchError("invalid JSON body: %s" % exc, 400)
        if not isinstance(payload, dict):
            raise DispatchError("JSON body must be an object", 400)
        return payload

    def _send_file(self, full_path, status=200):
        try:
            with open(full_path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self._error("not found", 404)
        content_type = CONTENT_TYPES.get(os.path.splitext(full_path)[1].lower(), "application/octet-stream")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, relative, root):
        """Serve a file from a directory, refusing anything that escapes it."""
        safe = os.path.normpath(unquote(relative)).lstrip("/")
        if safe.startswith("..") or os.path.isabs(safe):
            return self._error("bad path", 400)
        full = os.path.join(root, safe)
        if not os.path.isfile(full):
            return self._error("not found", 404)
        return self._send_file(full)

    # ---- routes -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/":
                return self._send_json(
                    {
                        "service": "apex-dispatch",
                        "version": "1.0",
                        "endpoints": [
                            "/board",
                            "/health",
                            "/exceptions",
                            "/shipments/{shipment_id}",
                            "/drivers/available",
                            "/reroutes",
                            "/updates",
                            "/audit",
                            "/openapi.json",
                        ],
                    }
                )
            # The demo board is a read-only HTML view, not a gateway tool: it is
            # deliberately absent from openapi.json so Kong never exposes it.
            if path == "/board":
                return self._send_file(os.path.join(STATIC_DIR, "board.html"))
            if path.startswith("/static/"):
                return self._send_static(path[len("/static/"):], STATIC_DIR)
            if path.startswith("/config/"):
                return self._send_static(path[len("/config/"):], CONFIG_DIR)
            if path == "/health":
                return self._send_json({"ok": True})
            if path == "/openapi.json":
                with open(OPENAPI_PATH, encoding="utf-8") as handle:
                    return self._send_json(json.load(handle))
            if path == "/exceptions":
                return self._send_json({"count": len(DISPATCH.list_exceptions()), "exceptions": DISPATCH.list_exceptions()})
            if path == "/drivers/available":
                rows = DISPATCH.available_drivers(
                    terminal=query.get("terminal"),
                    equipment=query.get("equipment"),
                    endorsement=query.get("endorsement"),
                    shipment_id=query.get("shipment_id"),
                )
                return self._send_json({"count": len(rows), "drivers": rows})
            if path == "/reroutes":
                status = query.get("status")
                rows = DISPATCH.pending_proposals(status=status) if status in (None, "", "pending", "all") else []
                if status == "pending":
                    rows = [r for r in rows if r["decision"] == "pending_human" and r.get("status") != "applied"]
                return self._send_json({"count": len(rows), "proposals": rows})
            if path == "/updates":
                rows = DISPATCH.updates_feed()
                return self._send_json({"count": len(rows), "updates": rows})
            if path == "/audit":
                rows = DISPATCH.audit_log()
                return self._send_json({"count": len(rows), "audit": rows})

            match = SHIPMENT_RE.match(path)
            if match:
                return self._send_json(DISPATCH.shipment_view(unquote(match.group("shipment_id"))))

            return self._error("no route for GET %s" % path, 404)
        except DispatchError as exc:
            return self._error(exc.message, exc.status)
        except Exception as exc:  # pragma: no cover - defensive
            return self._error("internal error: %s" % exc, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            body = self._read_body()

            if path == "/reroutes":
                missing = [k for k in ("shipment_id", "new_driver_id") if not body.get(k)]
                if missing:
                    raise DispatchError("missing required field(s): %s" % ", ".join(missing), 400)
                proposal = DISPATCH.create_reroute(
                    shipment_id=body["shipment_id"],
                    new_driver_id=body["new_driver_id"],
                    reason=body.get("reason"),
                    requested_by=body.get("requested_by"),
                )
                return self._send_json(proposal, status=201)

            match = APPROVE_RE.match(path)
            if match:
                proposal_id = unquote(match.group("proposal_id"))
                approver = body.get("approver")
                if not approver:
                    raise DispatchError("missing required field: approver", 400)
                proposal = DISPATCH.approve(proposal_id, approver=approver, note=body.get("note"))
                return self._send_json(proposal)

            return self._error("no route for POST %s" % path, 404)
        except DispatchError as exc:
            return self._error(exc.message, exc.status)
        except Exception as exc:  # pragma: no cover - defensive
            return self._error("internal error: %s" % exc, 500)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("apex-dispatch listening on http://%s:%d" % (HOST, PORT), flush=True)
    print(
        "loaded %d shipments, %d drivers, %d terminals"
        % (len(DISPATCH.shipments), len(DISPATCH.drivers), len(DISPATCH.terminals)),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
