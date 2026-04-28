#!/usr/bin/env python3
"""
DeCloud Relay HTTP Proxy
Routes wildcard subdomain traffic to CGNAT nodes' VMs through WireGuard tunnel
"""

import json
import logging
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request
import urllib.error
from threading import Thread, Lock

# Configuration
ORCHESTRATOR_URL = os.environ.get('ORCHESTRATOR_URL', 'http://10.20.0.1:5000')
RELAY_ID = os.environ.get('RELAY_ID', '__NODE_ID__')
RELAY_TOKEN_FILE = '/etc/wireguard/private.key'
LISTEN_PORT = 8081
ROUTING_CACHE_TTL = 30  # seconds

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/var/log/decloud-relay-proxy.log')
    ]
)
logger = logging.getLogger('relay-http-proxy')

# Global routing map cache
routing_map = {}
routing_map_lock = Lock()
last_routing_update = 0


class HTTPProxyHandler(BaseHTTPRequestHandler):
    """HTTP proxy handler for CGNAT VM traffic"""

    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")

    def do_GET(self):
        self.proxy_request()

    def do_POST(self):
        self.proxy_request()

    def do_PUT(self):
        self.proxy_request()

    def do_DELETE(self):
        self.proxy_request()

    def do_HEAD(self):
        self.proxy_request()

    def do_OPTIONS(self):
        self.proxy_request()

    def proxy_request(self):
        """Proxy the request to the appropriate CGNAT node"""
        try:
            # Extract subdomain from Host header
            host = self.headers.get('Host', '')
            subdomain = extract_subdomain(host)

            if not subdomain:
                self.send_error_response(400, "Missing or invalid Host header")
                return

            logger.info(f"Proxying request for subdomain: {subdomain}")

            # Look up routing info
            route = get_route_for_subdomain(subdomain)
            if not route:
                logger.warning(f"No route found for subdomain: {subdomain}")
                self.send_error_response(404, f"VM not found for subdomain '{subdomain}'")
                return

            # Build target URL (CGNAT node agent's internal proxy endpoint)
            target_url = f"http://{route['cgnat_node_tunnel_ip']}:{route['node_agent_port']}/api/vms/{route['vm_id']}/proxy/http/{route['target_port']}{self.path}"

            logger.info(f"Forwarding {subdomain} → {target_url}")

            # Forward the request
            self.forward_request(target_url, route)

        except Exception as e:
            logger.error(f"Error proxying request: {e}", exc_info=True)
            self.send_error_response(502, f"Proxy error: {str(e)}")

    def forward_request(self, target_url, route):
        """Forward the HTTP request to the target"""
        try:
            # Prepare headers
            headers = {}
            for key, value in self.headers.items():
                if key.lower() not in ['host', 'connection', 'transfer-encoding']:
                    headers[key] = value

            # Add DeCloud headers
            headers['X-Forwarded-For'] = self.client_address[0]
            headers['X-Forwarded-Proto'] = 'http'
            headers['X-Forwarded-Host'] = self.headers.get('Host', '')

            # Read request body if present
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            # Make upstream request
            req = urllib.request.Request(
                target_url,
                data=body,
                headers=headers,
                method=self.command
            )

            with urllib.request.urlopen(req, timeout=30) as response:
                # Send response back to client
                self.send_response(response.status)

                # Forward response headers
                for key, value in response.headers.items():
                    if key.lower() not in ['connection', 'transfer-encoding']:
                        self.send_header(key, value)

                self.end_headers()

                # Stream response body
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

        except urllib.error.HTTPError as e:
            # Forward HTTP errors from upstream
            self.send_response(e.code)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(e.read())

        except Exception as e:
            logger.error(f"Error forwarding request: {e}")
            raise

    def send_error_response(self, code, message):
        """Send an error response"""
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        error_body = json.dumps({
            'error': 'Proxy Error',
            'message': message,
            'relay_id': RELAY_ID
        })
        self.wfile.write(error_body.encode('utf-8'))


def extract_subdomain(host):
    """Extract subdomain from Host header"""
    if not host:
        return None

    # Remove port if present
    host = host.split(':')[0]

    # Extract subdomain (e.g., de2.vms.stackfi.tech → de2)
    parts = host.split('.')
    if len(parts) >= 3:  # Has subdomain
        return parts[0]

    return None


def get_route_for_subdomain(subdomain):
    """Look up routing info for a subdomain"""
    with routing_map_lock:
        # Check if we need to refresh the routing map
        global last_routing_update
        if time.time() - last_routing_update > ROUTING_CACHE_TTL:
            refresh_routing_map()

        # Find matching route
        for route in routing_map.get('routes', []):
            route_subdomain = extract_subdomain(route.get('subdomain', ''))
            if route_subdomain == subdomain:
                return route

    return None

def compute_auth_token(relay_id, relay_token):
    """Compute HMAC-SHA256 token matching C# implementation"""
    import base64
    import hmac
    import hashlib
    
    message = f"{relay_id}:relay-http-proxy"
    token = hmac.new(
        relay_token.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).digest()
    return base64.b64encode(token).decode('utf-8')

def refresh_routing_map():
    """Fetch the latest routing map from orchestrator"""
    global routing_map, last_routing_update

    try:
        logger.info("Refreshing routing map from orchestrator...")

        # Read relay token
        with open(RELAY_TOKEN_FILE, 'r') as f:
            relay_token = f.read().strip()

        # Compute authentication token
        token = compute_auth_token(RELAY_ID, relay_token)

        # Fetch routing map
        url = f"{ORCHESTRATOR_URL}/api/relay/{RELAY_ID}/routing-map"
        req = urllib.request.Request(url)
        req.add_header('X-Relay-Token', token)

        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))

            if data.get('success'):
                routing_map = data.get('data', {})
                last_routing_update = time.time()
                logger.info(f"✓ Routing map updated: {len(routing_map.get('routes', []))} routes")
            else:
                logger.warning(f"Failed to fetch routing map: {data.get('error')}")

    except Exception as e:
        logger.error(f"Error refreshing routing map: {e}")
        # Keep using cached map if available


def background_refresh():
    """Background thread to periodically refresh routing map"""
    while True:
        time.sleep(ROUTING_CACHE_TTL)
        with routing_map_lock:
            refresh_routing_map()


def main():
    """Start the HTTP proxy server"""
    # Initial routing map fetch
    with routing_map_lock:
        refresh_routing_map()

    # Start background refresh thread
    refresh_thread = Thread(target=background_refresh, daemon=True)
    refresh_thread.start()

    # Start HTTP server
    server_address = ('0.0.0.0', LISTEN_PORT)
    httpd = HTTPServer(server_address, HTTPProxyHandler)

    logger.info('=' * 60)
    logger.info('DeCloud Relay HTTP Proxy')
    logger.info('=' * 60)
    logger.info(f'  Relay ID:        {RELAY_ID}')
    logger.info(f'  Orchestrator:    {ORCHESTRATOR_URL}')
    logger.info(f'  Listen:          http://0.0.0.0:{LISTEN_PORT}')
    logger.info(f'  Routes loaded:   {len(routing_map.get("routes", []))}')
    logger.info('=' * 60)
    logger.info('✓ Proxy started successfully')

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info('Shutting down proxy...')
        httpd.shutdown()
        logger.info('✓ Proxy stopped')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)