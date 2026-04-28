#!/usr/bin/env python3
"""
DeCloud DHT Node Dashboard Server
Version: 2.0.0 - Added /diagnostics proxy endpoint

Lightweight HTTP server that serves the dashboard UI and proxies
API requests to the DHT Go binary running on localhost.
"""

import json
import os
import sys
import logging
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from urllib.request import urlopen, Request
from urllib.error import URLError

# ==================== Configuration ====================
DHT_API_PORT = int(os.environ.get("DHT_API_PORT", "__DHT_API_PORT__"))
STATIC_DIR = "/opt/decloud-dht/static"
LISTEN_PORT = 8080

# API endpoints to proxy to the Go binary (read-only only).
# Mutating endpoints (/connect, /publish) are not exposed — they require
# a Bearer token and are only called by internal services (dht-bootstrap-poll.sh).
PROXY_PATHS = {"/health", "/peers", "/diagnostics"}

# /providers/ is proxied with prefix match (see do_GET below)

# ==================== Logging ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [dht-dashboard] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("dht-dashboard")

# ==================== Request Handler ====================
class DhtDashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path

        if path in PROXY_PATHS or path.startswith("/providers/") or path.startswith("/proximity/"):
            self._proxy_to_dht(path)
        elif path == "/" or path == "/index.html":
            self._serve_dashboard()
        elif path.startswith("/static/"):
            self._serve_static(path)
        else:
            self._send_error(404, "Not Found")

    def do_POST(self):
        path = urlparse(self.path).path

        if path in PROXY_PATHS:
            self._proxy_to_dht(path, method="POST")
        else:
            self._send_error(404, "Not Found")

    # ---- Dashboard & Static Files ----

    def _serve_dashboard(self):
        fpath = os.path.join(STATIC_DIR, "dashboard.html")
        if not os.path.exists(fpath):
            self._send_error(404, "Dashboard not found")
            return
        self._send_file(fpath, "text/html; charset=utf-8")

    def _serve_static(self, url_path):
        rel = url_path.replace("/static/", "", 1)
        fpath = os.path.join(STATIC_DIR, rel)

        if not os.path.abspath(fpath).startswith(os.path.abspath(STATIC_DIR)):
            self._send_error(403, "Forbidden")
            return
        if not os.path.isfile(fpath):
            self._send_error(404, "File not found")
            return

        ct = "application/octet-stream"
        if fpath.endswith(".css"):
            ct = "text/css"
        elif fpath.endswith(".js"):
            ct = "application/javascript"
        elif fpath.endswith(".html"):
            ct = "text/html; charset=utf-8"

        self._send_file(fpath, ct)

    def _send_file(self, fpath, content_type):
        with open(fpath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    # ---- API Proxy ----

    def _proxy_to_dht(self, path, method="GET"):
        url = "http://127.0.0.1:{port}{path}".format(port=DHT_API_PORT, path=path)
        try:
            body = None
            if method == "POST":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else None

            req = Request(url, data=body, method=method)
            req.add_header("Accept", "application/json")
            if body:
                req.add_header("Content-Type", "application/json")

            resp = urlopen(req, timeout=35)  # /providers/ can take up to 30s
            data = resp.read()

            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(data))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        except URLError as e:
            logger.warning("Proxy to DHT API failed: %s", e)
            self._send_json(502, {"error": "DHT node unreachable", "detail": str(e)})
        except Exception as e:
            logger.error("Proxy error: %s", e)
            self._send_json(500, {"error": "Internal proxy error", "detail": str(e)})

    # ---- Helpers ----

    def _send_error(self, code, msg):
        self._send_json(code, {"error": msg, "status": code})

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


# ==================== Main ====================
def main():
    if not os.path.isdir(STATIC_DIR):
        logger.warning("Static directory missing: %s", STATIC_DIR)

    server = HTTPServer(("0.0.0.0", LISTEN_PORT), DhtDashboardHandler)
    logger.info("DHT Dashboard server listening on :%d", LISTEN_PORT)
    logger.info("  Proxying API to DHT binary on :%d", DHT_API_PORT)
    logger.info("  Proxied paths: %s + /providers/*", ", ".join(sorted(PROXY_PATHS)))
    logger.info("  Serving static files from %s", STATIC_DIR)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        logger.info("Server stopped")


if __name__ == "__main__":
    main()