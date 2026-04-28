#!/usr/bin/env python3
"""
DeCloud Relay VM Management API
Version: 3.0.0 - PEER CLASSIFICATION

Provides REST API for relay monitoring, CGNAT peer management,
and serves the dashboard interface.

IMPROVEMENTS v3.0.0:
- Peer classification: each peer has a type (cgnat-node, system-vm, relay-peer, orchestrator)
- Capacity counts only cgnat-node peers (system VMs don't consume capacity)
- Peers grouped by parent_node_id for dashboard display
- Persistent peer metadata registry (survives restarts)
- Backward-compatible: peers without type default to cgnat-node
"""

import json
import subprocess
import os
import logging
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import sys

# ==================== Configuration ====================
RELAY_ID = "__VM_ID__"
RELAY_NAME = "__VM_NAME__"
RELAY_REGION = "__RELAY_REGION__"
RELAY_CAPACITY = int("__RELAY_CAPACITY__")
NODE_ID = "__NODE_ID__"  # Relay host's node ID (for detecting host system VMs)
WIREGUARD_INTERFACE = "wg-relay-server"
STATIC_DIR = "/opt/decloud-relay/static"
LISTEN_PORT = 8080
PEER_REGISTRY_PATH = "/etc/decloud/peer-registry.json"
PEER_METADATA_PATH = "/etc/decloud/peer-metadata.json"

# ==================== Periodic Cleanup Configuration ====================
CLEANUP_ENABLED = True                    # Enable/disable automatic cleanup
CLEANUP_INTERVAL_SECONDS = 300            # 5 minutes (300 seconds)

# Unified stale threshold for both background and manual cleanup
STALE_THRESHOLD_SECONDS = 300             # 5 minutes - peer with no handshake for this long is stale

# Grace period for newly-added peers
# Peers registered within this time window are never removed, even with no handshake
# This aligns with orchestrator's 30-second grace period and provides buffer
PEER_REGISTRATION_GRACE_PERIOD = 60       # 60 seconds - gives time for client to connect

# System-VM peers (DHT, BlockStore) are generally protected from cleanup,
# but broken registrations (no AllowedIPs or never-handshaked) should still
# be cleaned up after this extended threshold.
SYSTEM_VM_STALE_THRESHOLD_SECONDS = 3600  # 1 hour - generous window for system VMs

ORCHESTRATOR_IP = "10.20.0.1/32"          # Never remove orchestrator peer

# ==================== Logging ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/var/log/decloud-relay-api.log')
    ]
)
logger = logging.getLogger('relay-api')

# ==================== Global State ====================
cleanup_stats = {
    'last_run': None,
    'total_runs': 0,
    'total_removed': 0,
    'last_removed_count': 0,
    'errors': 0
}

# Track when peers were registered (public_key -> timestamp)
peer_registration_times = {}
peer_registration_lock = threading.Lock()

# Persistent relay peer registry (public_key -> metadata)
# Protected from stale cleanup — only removed via explicit API call
relay_peer_registry = {}
relay_peer_lock = threading.Lock()

# Peer metadata registry (public_key -> {peer_type, parent_node_id, ...})
# Tracks classification for ALL peers (cgnat-node, system-vm, relay-peer)
# Persisted to disk so capacity counting survives restarts.
#
# peer_type values:
#   "cgnat-node"  — CGNAT node agent (counts toward capacity)
#   "system-vm"   — DHT/BlockStore VM belonging to a node (does NOT count)
#   "relay-peer"  — cross-peer relay (does NOT count)
#   "orchestrator" — pre-configured orchestrator peer (does NOT count)
peer_metadata = {}
peer_metadata_lock = threading.Lock()


def load_peer_registry():
    """Load relay peer registry from persistent JSON file"""
    global relay_peer_registry
    try:
        if os.path.exists(PEER_REGISTRY_PATH):
            with open(PEER_REGISTRY_PATH, 'r') as f:
                data = json.load(f)
            relay_peer_registry = data.get('relay_peers', {})
            logger.info(f"Loaded {len(relay_peer_registry)} relay peers from registry")
        else:
            relay_peer_registry = {}
            logger.info("No existing peer registry found, starting fresh")
    except Exception as e:
        logger.error(f"Error loading peer registry: {e}")
        relay_peer_registry = {}


def save_peer_registry():
    """Save relay peer registry to persistent JSON file"""
    try:
        os.makedirs(os.path.dirname(PEER_REGISTRY_PATH), exist_ok=True)
        data = {'relay_peers': relay_peer_registry}
        with open(PEER_REGISTRY_PATH, 'w') as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Saved {len(relay_peer_registry)} relay peers to registry")
    except Exception as e:
        logger.error(f"Error saving peer registry: {e}")


def load_peer_metadata():
    """Load peer metadata registry from persistent JSON file"""
    global peer_metadata
    try:
        if os.path.exists(PEER_METADATA_PATH):
            with open(PEER_METADATA_PATH, 'r') as f:
                peer_metadata = json.load(f)
            logger.info(f"Loaded {len(peer_metadata)} peer metadata entries")
        else:
            peer_metadata = {}
            logger.info("No existing peer metadata found, starting fresh")
    except Exception as e:
        logger.error(f"Error loading peer metadata: {e}")
        peer_metadata = {}


def save_peer_metadata():
    """Save peer metadata registry to persistent JSON file"""
    try:
        os.makedirs(os.path.dirname(PEER_METADATA_PATH), exist_ok=True)
        with open(PEER_METADATA_PATH, 'w') as f:
            json.dump(peer_metadata, f, indent=2)
        logger.debug(f"Saved {len(peer_metadata)} peer metadata entries")
    except Exception as e:
        logger.error(f"Error saving peer metadata: {e}")


def set_peer_metadata(public_key, peer_type, parent_node_id=None, description=None):
    """Store classification metadata for a peer"""
    with peer_metadata_lock:
        peer_metadata[public_key] = {
            'peer_type': peer_type,
            'parent_node_id': parent_node_id,
            'description': description,
            'registered_at': int(time.time())
        }
        save_peer_metadata()


def get_peer_metadata(public_key):
    """Get classification metadata for a peer, or None"""
    with peer_metadata_lock:
        return peer_metadata.get(public_key)


def remove_peer_metadata(public_key):
    """Remove classification metadata for a peer"""
    with peer_metadata_lock:
        if public_key in peer_metadata:
            del peer_metadata[public_key]
            save_peer_metadata()


def classify_peer(public_key, allowed_ips):
    """
    Classify a peer using metadata registry, falling back to IP heuristic.
    Returns (peer_type, parent_node_id).
    """
    # Check explicit metadata first
    meta = get_peer_metadata(public_key)
    if meta:
        return meta.get('peer_type', 'cgnat-node'), meta.get('parent_node_id')

    # Check relay peer registry
    if is_relay_peer(public_key):
        return 'relay-peer', None

    # Orchestrator peer
    if ORCHESTRATOR_IP in (allowed_ips or ''):
        return 'orchestrator', None

    # IP heuristic fallback for legacy peers without metadata:
    # .198-.253 in 10.20.x.x/32 = mesh/system-vm peers
    try:
        for aip in (allowed_ips or '').split(','):
            aip = aip.strip()
            if '/32' in aip and aip.startswith('10.20.'):
                parts = aip.replace('/32', '').split('.')
                if len(parts) == 4:
                    last_octet = int(parts[3])
                    if last_octet >= 198:
                        return 'system-vm', None
    except (ValueError, IndexError):
        pass

    # Default: CGNAT node
    return 'cgnat-node', None


def get_capacity_counts():
    """
    Calculate capacity using peer classification.
    Only cgnat-node peers count toward capacity.
    Returns dict with counts by type and grouped nodes.
    """
    try:
        result = subprocess.run(
            ['wg', 'show', WIREGUARD_INTERFACE, 'dump'],
            capture_output=True, text=True, timeout=5
        )

        if result.returncode != 0:
            return {
                'cgnat_node_count': 0,
                'system_vm_count': 0,
                'relay_peer_count': 0,
                'total_peer_count': 0,
                'node_groups': {}
            }

        lines = result.stdout.strip().split('\n')
        counts = {'cgnat-node': 0, 'system-vm': 0, 'relay-peer': 0, 'orchestrator': 0}
        node_groups = {}  # parent_node_id -> list of system-vm info

        for line in lines[1:]:
            if not line.strip():
                continue
            fields = line.split('\t')
            if len(fields) < 7:
                continue

            public_key = fields[0]
            allowed_ips = fields[3]
            peer_type, parent_node_id = classify_peer(public_key, allowed_ips)
            counts[peer_type] = counts.get(peer_type, 0) + 1

            if peer_type == 'system-vm' and parent_node_id:
                if parent_node_id not in node_groups:
                    node_groups[parent_node_id] = []
                meta = get_peer_metadata(public_key) or {}
                node_groups[parent_node_id].append({
                    'public_key': public_key[:16] + '...',
                    'allowed_ips': allowed_ips,
                    'description': meta.get('description', ''),
                    'is_host': parent_node_id == NODE_ID
                })

        return {
            'cgnat_node_count': counts.get('cgnat-node', 0),
            'system_vm_count': counts.get('system-vm', 0),
            'relay_peer_count': counts.get('relay-peer', 0),
            'orchestrator_count': counts.get('orchestrator', 0),
            'total_peer_count': sum(counts.values()),
            'node_groups': node_groups
        }

    except Exception as e:
        logger.error(f"Error calculating capacity counts: {e}")
        return {
            'cgnat_node_count': 0,
            'system_vm_count': 0,
            'relay_peer_count': 0,
            'total_peer_count': 0,
            'node_groups': {}
        }


def is_relay_peer(public_key):
    """Check if a peer is a registered relay peer"""
    with relay_peer_lock:
        return public_key in relay_peer_registry

# ==================== Helper Functions ====================

def record_peer_registration(public_key):
    """Record timestamp when peer is first registered.
    Intentionally non-destructive: if the peer is already tracked, the
    existing timestamp is preserved. This prevents EnsurePeerRegistered
    heartbeat calls from resetting the grace period timer on re-adds.
    The timestamp is only reset when the peer is explicitly removed first.
    """
    with peer_registration_lock:
        if public_key not in peer_registration_times:
            peer_registration_times[public_key] = time.time()
            logger.debug(f"📝 Recorded registration time for peer {public_key[:16]}...")
        else:
            logger.debug(f"⏭️  Peer {public_key[:16]}... already tracked — skipping grace period reset")

def get_peer_age(public_key, current_time):
    """Get age of peer in seconds since registration"""
    with peer_registration_lock:
        registration_time = peer_registration_times.get(public_key)
        
        if registration_time is None:
            # Peer was registered before we started tracking (or tracking failed)
            # Assume it's old enough to evaluate normally
            return float('inf')
        
        return current_time - registration_time

def cleanup_stale_peer_records():
    """Remove registration records for peers that no longer exist"""
    with peer_registration_lock:
        # Get current peers
        try:
            result = subprocess.run(
                ['wg', 'show', WIREGUARD_INTERFACE, 'dump'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                return
            
            lines = result.stdout.strip().split('\n')
            current_peers = set()
            
            for line in lines[1:]:  # Skip interface line
                if not line.strip():
                    continue
                fields = line.split('\t')
                if len(fields) >= 1:
                    current_peers.add(fields[0])
            
            # Remove records for peers that no longer exist
            stale_records = [pk for pk in peer_registration_times.keys() if pk not in current_peers]
            
            for pk in stale_records:
                del peer_registration_times[pk]
                logger.debug(f"🧹 Cleaned up registration record for removed peer {pk[:16]}...")
                
        except Exception as e:
            logger.debug(f"Error cleaning up peer records: {e}")

# ==================== Background Cleanup Thread ====================
class PeerCleanupThread(threading.Thread):
    """Background daemon thread for periodic peer cleanup"""
    
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()
        self.name = "PeerCleanupThread"
    
    def run(self):
        """Main cleanup loop - runs in background"""
        logger.info("🧹 Peer cleanup thread started")
        logger.info(f"   Cleanup interval: {CLEANUP_INTERVAL_SECONDS}s ({CLEANUP_INTERVAL_SECONDS // 60} minutes)")
        logger.info(f"   Stale threshold: {STALE_THRESHOLD_SECONDS}s ({STALE_THRESHOLD_SECONDS // 60} minutes)")
        logger.info(f"   ✅ NEW: Grace period: {PEER_REGISTRATION_GRACE_PERIOD}s for newly-registered peers")
        
        # Wait 30 seconds before first run (let server stabilize)
        time.sleep(30)
        
        while not self.stop_event.is_set():
            try:
                self.cleanup_stale_peers()
                cleanup_stale_peer_records()  # Clean up registration tracking
                
                # Wait for next interval or until stopped
                self.stop_event.wait(CLEANUP_INTERVAL_SECONDS)
                
            except Exception as e:
                logger.error(f"Error in cleanup thread: {e}", exc_info=True)
                cleanup_stats['errors'] += 1
                # Wait a bit before retrying
                self.stop_event.wait(60)
    
    def stop(self):
        """Stop the cleanup thread gracefully"""
        logger.info("Stopping peer cleanup thread...")
        self.stop_event.set()
    
    def cleanup_stale_peers(self):
        """
        Remove peers with no handshake or stale handshake
        Respects grace period for newly-registered peers
        """
        try:
            current_time = int(time.time())
            
            # Get current peers from WireGuard
            result = subprocess.run(
                ['wg', 'show', WIREGUARD_INTERFACE, 'dump'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to get WireGuard peers: {result.stderr}")
                return
            
            lines = result.stdout.strip().split('\n')
            
            removed = []
            kept = []
            skipped_new = []  # Track peers skipped due to grace period
            
            # Skip interface line (first line), process peers
            for line in lines[1:]:
                if not line.strip():
                    continue
                
                fields = line.split('\t')
                if len(fields) < 7:
                    continue
                
                public_key = fields[0]
                allowed_ips = fields[3]
                latest_handshake = int(fields[4]) if fields[4] else 0
                
                # Never remove orchestrator peer
                if ORCHESTRATOR_IP in allowed_ips:
                    kept.append({
                        'public_key': public_key[:16] + '...',
                        'allowed_ips': allowed_ips,
                        'reason': 'orchestrator'
                    })
                    continue
                
                # Never remove relay peers (persistent, managed via API)
                if is_relay_peer(public_key):
                    kept.append({
                        'public_key': public_key[:16] + '...',
                        'allowed_ips': allowed_ips,
                        'reason': 'relay_peer'
                    })
                    continue
                
                # System-vm peers (DHT VMs, BlockStore VMs) are generally protected
                # from cleanup — they may not generate enough traffic to maintain
                # handshake freshness. But broken registrations (no AllowedIPs or
                # never-handshaked after extended period) are clearly failed attempts
                # and should be cleaned up.
                peer_type, _ = classify_peer(public_key, allowed_ips)
                if peer_type == 'system-vm':
                    is_broken = allowed_ips == '(none)' or not allowed_ips
                    if latest_handshake == 0:
                        time_since_handshake_vm = float('inf')
                    else:
                        time_since_handshake_vm = current_time - latest_handshake

                    is_long_stale = time_since_handshake_vm > SYSTEM_VM_STALE_THRESHOLD_SECONDS

                    if is_broken or (is_long_stale and latest_handshake == 0):
                        # Broken registration or never-connected system VM — clean up
                        remove_result = subprocess.run(
                            ['wg', 'set', WIREGUARD_INTERFACE, 'peer', public_key, 'remove'],
                            capture_output=True, text=True, timeout=5
                        )
                        if remove_result.returncode == 0:
                            removed.append({
                                'public_key': public_key[:16] + '...',
                                'allowed_ips': allowed_ips,
                                'handshake': 'never',
                                'reason': 'broken_system_vm'
                            })
                            remove_peer_metadata(public_key)
                            logger.info(
                                f"Removed broken system-vm peer: {public_key[:16]}... "
                                f"(allowed_ips={allowed_ips}, handshake=never)"
                            )
                        continue

                    kept.append({
                        'public_key': public_key[:16] + '...',
                        'allowed_ips': allowed_ips,
                        'reason': 'system_vm'
                    })
                    continue

                # Check if peer is within grace period
                peer_age = get_peer_age(public_key, current_time)
                
                if peer_age < PEER_REGISTRATION_GRACE_PERIOD:
                    skipped_new.append({
                        'public_key': public_key[:16] + '...',
                        'allowed_ips': allowed_ips,
                        'age_seconds': int(peer_age)
                    })
                    logger.debug(
                        f"⏱️  Skipping new peer {public_key[:16]}... "
                        f"(age: {peer_age:.0f}s < {PEER_REGISTRATION_GRACE_PERIOD}s grace period)"
                    )
                    continue
                
                # Calculate time since last handshake
                if latest_handshake == 0:
                    time_since_handshake = float('inf')
                    handshake_status = 'never'
                else:
                    time_since_handshake = current_time - latest_handshake
                    handshake_status = f'{time_since_handshake}s ago'
                
                # Check if peer is stale (and past grace period)
                if time_since_handshake > STALE_THRESHOLD_SECONDS:
                    # Remove stale peer
                    remove_result = subprocess.run(
                        ['wg', 'set', WIREGUARD_INTERFACE, 'peer', public_key, 'remove'],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    
                    if remove_result.returncode == 0:
                        removed.append({
                            'public_key': public_key[:16] + '...',
                            'allowed_ips': allowed_ips,
                            'handshake': handshake_status,
                            'peer_age_seconds': int(peer_age) if peer_age != float('inf') else 'unknown'
                        })
                        remove_peer_metadata(public_key)
                        logger.info(
                            f"Removed stale peer: {public_key[:16]}... "
                            f"({allowed_ips}, handshake: {handshake_status}, peer age: {peer_age:.0f}s)"
                        )
                    else:
                        logger.error(
                            f"Failed to remove peer {public_key[:16]}...: "
                            f"{remove_result.stderr}"
                        )
                else:
                    # Peer is active
                    kept.append({
                        'public_key': public_key[:16] + '...',
                        'allowed_ips': allowed_ips,
                        'handshake': handshake_status
                    })
            
            # Save WireGuard configuration if we removed any peers
            if removed:
                try:
                    subprocess.run(
                        ['wg-quick', 'save', WIREGUARD_INTERFACE],
                        capture_output=True,
                        timeout=5
                    )
                    logger.info("💾 WireGuard configuration saved after cleanup")
                except Exception as e:
                    logger.warning(f"Failed to save WireGuard config: {e}")
            
            # Update statistics
            cleanup_stats['last_run'] = time.time()
            cleanup_stats['total_runs'] += 1
            cleanup_stats['total_removed'] += len(removed)
            cleanup_stats['last_removed_count'] = len(removed)
            
            # Log summary
            if removed or skipped_new:
                logger.info(
                    f"✅ Cleanup completed: removed {len(removed)} stale peers, "
                    f"kept {len(kept)} active peers, skipped {len(skipped_new)} new peers (grace period)"
                )
            else:
                logger.debug(
                    f"✅ Cleanup completed: no stale peers found, "
                    f"{len(kept)} active peers"
                )
            
        except subprocess.TimeoutExpired:
            logger.error("❌ Cleanup timed out")
            cleanup_stats['errors'] += 1
        except Exception as e:
            logger.error(f"❌ Cleanup error: {e}", exc_info=True)
            cleanup_stats['errors'] += 1

# ==================== WireGuard Watchdog Thread ====================
class WireGuardWatchdogThread(threading.Thread):
    """
    Watches the WireGuard interface every 30 s and restarts it if down.
    The relay VM is the only entity that can see its own WG state and act
    on it without depending on the host or orchestrator.
    """

    CHECK_INTERVAL = 30   # seconds between checks
    SETTLE_TIME    = 2    # seconds to wait after restart before verifying

    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()
        self.name = "WireGuardWatchdogThread"

    def run(self):
        logger.info(f"🔍 WireGuard watchdog started (interval: {self.CHECK_INTERVAL}s)")
        # Give the server a moment to finish initialising before first check
        self.stop_event.wait(10)

        while not self.stop_event.is_set():
            try:
                self._check_and_heal()
            except Exception as e:
                logger.error(f"WireGuard watchdog error: {e}", exc_info=True)
            self.stop_event.wait(self.CHECK_INTERVAL)

    def stop(self):
        self.stop_event.set()

    def _check_and_heal(self):
        result = subprocess.run(
            ['wg', 'show', WIREGUARD_INTERFACE],
            capture_output=True, text=True, timeout=5
        )

        if result.returncode == 0:
            logger.debug(f"✓ WireGuard {WIREGUARD_INTERFACE} is up")
            return

        logger.warning(
            f"WireGuard {WIREGUARD_INTERFACE} is DOWN — restarting via systemctl"
        )

        restart = subprocess.run(
            ['systemctl', 'restart', f'wg-quick@{WIREGUARD_INTERFACE}'],
            capture_output=True, text=True, timeout=20
        )

        if restart.returncode != 0:
            logger.error(
                f"systemctl restart failed: {restart.stderr.strip()}"
            )
            return

        # Brief settle, then verify
        time.sleep(self.SETTLE_TIME)

        verify = subprocess.run(
            ['wg', 'show', WIREGUARD_INTERFACE],
            capture_output=True, text=True, timeout=5
        )

        if verify.returncode == 0:
            logger.info(f"✅ WireGuard {WIREGUARD_INTERFACE} recovered successfully")
        else:
            logger.error(
                f"WireGuard still down after restart — manual intervention may be needed"
            )

# ==================== System Metrics Functions ====================

def get_cpu_usage():
    """Get current CPU usage percentage"""
    try:
        # Read CPU stats from /proc/stat
        with open('/proc/stat', 'r') as f:
            cpu_line = f.readline()
        
        # Parse CPU times
        cpu_times = [float(x) for x in cpu_line.split()[1:]]
        idle_time = cpu_times[3]  # idle is 4th value
        total_time = sum(cpu_times)
        
        # Calculate usage (simplified - instant reading)
        cpu_usage = 100.0 - (idle_time / total_time * 100.0) if total_time > 0 else 0.0
        
        return round(cpu_usage, 1)
    except Exception as e:
        logger.debug(f"Error reading CPU usage: {e}")
        return 0.0

def get_memory_usage():
    """Get current memory usage percentage"""
    try:
        # Read memory info from /proc/meminfo
        mem_info = {}
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = int(parts[1].strip().split()[0])  # Remove 'kB' and convert
                    mem_info[key] = value
        
        # Calculate memory usage
        total = mem_info.get('MemTotal', 0)
        available = mem_info.get('MemAvailable', 0)
        
        if total > 0:
            used = total - available
            usage_percent = (used / total) * 100.0
            return round(usage_percent, 1)
        
        return 0.0
    except Exception as e:
        logger.debug(f"Error reading memory usage: {e}")
        return 0.0

# ==================== Request Handler ====================
class RelayAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for relay management API"""
    
    # Suppress default logging
    def log_message(self, format, *args):
        pass  # Disable default request logging
    
    def do_GET(self):
        """Handle GET requests"""
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        
        # Log request
        logger.debug(f"GET {path}")
        
        if path == '/health':
            self.health_check()
        elif path == '/api/relay/status':
            self.get_relay_status()
        elif path == '/api/relay/wireguard':
            status = self.get_wireguard_status()
            self.send_json_response(status)
        elif path == '/api/relay/cleanup/stats':
            self.get_cleanup_stats()
        elif path == '/api/relay/relay-peers':
            self.get_relay_peers()
        elif path == '/' or path == '/index.html':
            self.serve_dashboard()
        elif path.startswith('/static/'):
            self.serve_static_file(path)
        else:
            self.send_error_response(404, 'Not Found', f'Path {path} not found')
    
    def do_POST(self):
        """Handle POST requests"""
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        
        logger.info(f"POST {path}")
        
        if path == '/api/relay/add-peer':
            self.add_cgnat_peer()
        elif path == '/api/relay/remove-peer':
            self.remove_cgnat_peer()
        elif path == '/api/relay/add-relay-peer':
            self.add_relay_peer()
        elif path == '/api/relay/remove-relay-peer':
            self.remove_relay_peer()
        elif path == '/api/relay/cleanup':
            self.manual_cleanup()
        elif path == '/api/relay/add-port-forward':
            self.add_port_forward()
        elif path == '/api/relay/remove-port-forward':
            self.remove_port_forward()
        elif path == '/api/relay/flush-port-forwards':
            self.flush_port_forwards()
        else:
            self.send_error_response(404, 'Not Found', f'Path {path} not found')

    
    # ==================== API Endpoints ====================
    
    def health_check(self):
        """Simple health check"""
        self.send_json_response({
            'status': 'healthy',
            'relay_id': RELAY_ID,
            'timestamp': int(time.time())
        })
    
    def get_relay_status(self):
        """Get comprehensive relay status"""
        try:
            # Get system uptime
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = int(float(f.read().split()[0]))
            
            # Get system metrics
            cpu_percent = get_cpu_usage()
            memory_percent = get_memory_usage()
            
            # Get WireGuard stats
            wg_status = self.get_wireguard_status()

            # Get relay's own WireGuard public key
            wg_pubkey = ''
            try:
                pubkey_result = subprocess.run(
                    ['wg', 'show', WIREGUARD_INTERFACE, 'public-key'],
                    capture_output=True, text=True, timeout=5
                )
                if pubkey_result.returncode == 0:
                    wg_pubkey = pubkey_result.stdout.strip()
            except Exception:
                pass
            
            self.send_json_response({
                'relay_id': RELAY_ID,
                'relay_name': RELAY_NAME,
                'region': RELAY_REGION,
                'max_capacity': RELAY_CAPACITY,
                'current_load': wg_status.get('cgnat_node_count', 0),
                'available_slots': wg_status.get('available_slots', RELAY_CAPACITY),
                'total_peers': wg_status.get('peer_count', 0),
                'cgnat_node_count': wg_status.get('cgnat_node_count', 0),
                'system_vm_count': wg_status.get('system_vm_count', 0),
                'uptime_seconds': uptime_seconds,
                'cpu_percent': cpu_percent,
                'memory_percent': memory_percent,
                'wireguard_interface': WIREGUARD_INTERFACE,
                'wireguard_public_key': wg_pubkey,
                'cleanup_config': {
                    'enabled': CLEANUP_ENABLED,
                    'interval_seconds': CLEANUP_INTERVAL_SECONDS,
                    'stale_threshold_seconds': STALE_THRESHOLD_SECONDS,
                    'grace_period_seconds': PEER_REGISTRATION_GRACE_PERIOD
                },
                'cleanup_stats': cleanup_stats,
                'peer_tracking': {
                    'tracked_peers': len(peer_registration_times),
                    'tracking_enabled': True
                }
            })
        except Exception as e:
            logger.error(f"Error getting relay status: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    
    def get_cleanup_stats(self):
        """Get cleanup statistics"""
        self.send_json_response({
            'cleanup_enabled': CLEANUP_ENABLED,
            'interval_seconds': CLEANUP_INTERVAL_SECONDS,
            'stale_threshold_seconds': STALE_THRESHOLD_SECONDS,
            'grace_period_seconds': PEER_REGISTRATION_GRACE_PERIOD,
            'stats': cleanup_stats,
            'tracked_peers_count': len(peer_registration_times)
        })
    
    def add_cgnat_peer(self):
        """
        Add a WireGuard peer (CGNAT node or system VM).
        Records registration timestamp for grace period tracking.

        Optional fields for peer classification:
          - peer_type: "cgnat-node" (default) or "system-vm"
          - parent_node_id: node ID that owns this peer (for grouping system VMs)
        """
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_error_response(400, 'Bad Request', 'Request body is empty')
                return

            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            # Validate required fields
            required_fields = ['public_key', 'allowed_ips']
            missing_fields = [f for f in required_fields if f not in data]

            if missing_fields:
                self.send_error_response(
                    400,
                    'Missing Required Fields',
                    f"Missing fields: {', '.join(missing_fields)}"
                )
                return

            # Validate allowed_ips is a real IP/CIDR, not empty or bare "/32"
            allowed_ips = data['allowed_ips'].strip()
            if not allowed_ips or allowed_ips == '/32' or not allowed_ips[0].isdigit():
                self.send_error_response(
                    400,
                    'Invalid allowed_ips',
                    f"allowed_ips must be a valid CIDR (got: '{allowed_ips}')"
                )
                return

            public_key = data['public_key']

            # ── Stale peer eviction (re-registration with new keypair) ──────────
            # If another peer already owns this tunnel IP with a different public
            # key, WireGuard silently leaves AllowedIPs on the old key and the
            # new peer gets no AllowedIPs, breaking all traffic routing.
            # Fix: evict the old peer atomically before adding the new one.
            try:
                dump = subprocess.run(
                    ['wg', 'show', WIREGUARD_INTERFACE, 'dump'],
                    capture_output=True, text=True, timeout=5
                )
                if dump.returncode == 0:
                    tunnel_ip = allowed_ips.split('/')[0].strip()
                    for line in dump.stdout.strip().split('\n')[1:]:
                        if not line.strip():
                            continue
                        fields = line.split('\t')
                        if len(fields) < 4:
                            continue
                        existing_key = fields[0]
                        existing_ips = fields[3] if fields[3] != '(none)' else ''
                        # Use exact IP comparison, NOT substring match.
                        # "10.20.1.2" in "10.20.1.200/32" is True in Python —
                        # that would falsely evict DHT/BlockStore peers and other
                        # CGNAT nodes whose IPs share a common prefix.
                        existing_ip_only = existing_ips.split('/')[0].strip()
                        if existing_ip_only == tunnel_ip and existing_key != public_key:
                            evict = subprocess.run(
                                ['wg', 'set', WIREGUARD_INTERFACE, 'peer', existing_key, 'remove'],
                                capture_output=True, text=True, timeout=5
                            )
                            if evict.returncode == 0:
                                remove_peer_metadata(existing_key)
                                with peer_registration_lock:
                                    peer_registration_times.pop(existing_key, None)
                                logger.info(
                                    f"Evicted stale peer {existing_key[:16]}... "
                                    f"— tunnel IP {tunnel_ip} re-registered with new key"
                                )
                            else:
                                logger.warning(
                                    f"Failed to evict stale peer {existing_key[:16]}...: "
                                    f"{evict.stderr.strip()}"
                                )
            except Exception as e:
                logger.warning(f"Stale peer eviction check failed (non-fatal): {e}")
            # ── End stale peer eviction ──────────────────────────────────────────

            # ── Idempotency guard ─────────────────────────────────────────────────────
            # If this peer is already tracked in memory, it is live and connected.
            # Return 200 immediately — do NOT reset its grace period timer.
            # This fast-path handles the common case of EnsurePeerRegistered heartbeat
            # calls arriving while the peer is already active.
            with peer_registration_lock:
                already_tracked = public_key in peer_registration_times

            if already_tracked:
                logger.debug(
                    f"⏭️  Idempotent re-add for already-tracked peer {public_key[:16]}... — skipping"
                )
                self.send_json_response({
                    'success': True,
                    'peer_public_key': public_key[:16] + '...',
                    'allowed_ips': data.get('allowed_ips', ''),
                    'description': data.get('description', 'CGNAT Node'),
                    'idempotent': True
                })
                return
            # ─────────────────────────────────────────────────────────────────────────

            # Build wg set command
            cmd = [
                'wg', 'set', WIREGUARD_INTERFACE,
                'peer', public_key,
                'allowed-ips', allowed_ips
            ]

            # Add optional persistent keepalive
            if 'persistent_keepalive' in data:
                cmd.extend(['persistent-keepalive', str(data['persistent_keepalive'])])

            description = data.get('description', 'CGNAT Node')
            peer_type = data.get('peer_type', 'cgnat-node')
            parent_node_id = data.get('parent_node_id')

            # Validate peer_type
            if peer_type not in ('cgnat-node', 'system-vm'):
                peer_type = 'cgnat-node'

            # Execute command
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode != 0:
                logger.error(f"Failed to add peer: {result.stderr}")
                self.send_error_response(
                    500,
                    'Failed to Add Peer',
                    result.stderr or 'Unknown error'
                )
                return

            # Save configuration
            try:
                subprocess.run(
                    ['wg-quick', 'save', WIREGUARD_INTERFACE],
                    capture_output=True,
                    timeout=5
                )
            except Exception as e:
                logger.warning(f"Failed to save WireGuard config: {e}")

            # Record peer registration timestamp
            record_peer_registration(public_key)

            # Store peer classification metadata
            set_peer_metadata(public_key, peer_type, parent_node_id, description)

            type_label = "system-vm" if peer_type == "system-vm" else "CGNAT"
            parent_label = f" (parent: {parent_node_id})" if parent_node_id else ""
            logger.info(f"Added {type_label} peer: {public_key[:16]}... ({description}){parent_label}")

            self.send_json_response({
                'success': True,
                'peer_public_key': public_key[:16] + '...',
                'allowed_ips': data['allowed_ips'],
                'description': description,
                'peer_type': peer_type,
                'parent_node_id': parent_node_id
            })

        except json.JSONDecodeError as e:
            self.send_error_response(400, 'Invalid JSON', str(e))
        except subprocess.TimeoutExpired:
            self.send_error_response(504, 'Command Timeout', 'WireGuard command timed out')
        except Exception as e:
            logger.error(f"Add peer error: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    
    def add_relay_peer(self):
        """
        Add another relay as WireGuard peer (persistent, protected from cleanup).
        Called by orchestrator during cross-peering.
        """
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_error_response(400, 'Bad Request', 'Request body is empty')
                return
            
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            # Validate required fields
            required_fields = ['public_key', 'endpoint', 'allowed_ips', 'relay_id']
            missing_fields = [f for f in required_fields if f not in data]
            
            if missing_fields:
                self.send_error_response(
                    400,
                    'Missing Required Fields',
                    f"Missing fields: {', '.join(missing_fields)}"
                )
                return
            
            public_key = data['public_key']
            endpoint = data['endpoint']
            allowed_ips = data['allowed_ips']
            relay_id = data['relay_id']
            
            # Validate allowed_ips is a /24 subnet
            if '/24' not in allowed_ips:
                self.send_error_response(
                    400,
                    'Invalid AllowedIPs',
                    'Relay peers must use /24 subnet (e.g., 10.20.2.0/24)'
                )
                return
            
            # Validate subnet is in 10.20.x.0/24 range
            if not allowed_ips.startswith('10.20.'):
                self.send_error_response(
                    400,
                    'Invalid Subnet',
                    'Relay subnet must be in 10.20.0.0/16 range'
                )
                return
            
            # Add peer with persistent keepalive (relay-to-relay needs bidirectional)
            cmd = [
                'wg', 'set', WIREGUARD_INTERFACE,
                'peer', public_key,
                'endpoint', endpoint,
                'allowed-ips', allowed_ips,
                'persistent-keepalive', '25'
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to add relay peer: {result.stderr}")
                self.send_error_response(
                    500,
                    'Failed to Add Relay Peer',
                    result.stderr or 'Unknown error'
                )
                return
            
            # Save WireGuard configuration
            try:
                subprocess.run(
                    ['wg-quick', 'save', WIREGUARD_INTERFACE],
                    capture_output=True,
                    timeout=5
                )
            except Exception as e:
                logger.warning(f"Failed to save WireGuard config: {e}")
            
            # Register in persistent relay peer registry
            with relay_peer_lock:
                relay_peer_registry[public_key] = {
                    'endpoint': endpoint,
                    'allowed_ips': allowed_ips,
                    'relay_id': relay_id,
                    'added_at': int(time.time())
                }
                save_peer_registry()

            # Also track registration time and classification metadata
            record_peer_registration(public_key)
            set_peer_metadata(public_key, 'relay-peer', description=f"relay-{relay_id}")
            
            logger.info(
                f"✅ Added relay peer: {public_key[:16]}... "
                f"(relay: {relay_id}, subnet: {allowed_ips}, endpoint: {endpoint})"
            )
            
            self.send_json_response({
                'success': True,
                'peer_public_key': public_key[:16] + '...',
                'relay_id': relay_id,
                'allowed_ips': allowed_ips,
                'endpoint': endpoint,
                'peer_type': 'relay'
            })
            
        except json.JSONDecodeError as e:
            self.send_error_response(400, 'Invalid JSON', str(e))
        except subprocess.TimeoutExpired:
            self.send_error_response(504, 'Command Timeout', 'WireGuard command timed out')
        except Exception as e:
            logger.error(f"Add relay peer error: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    
    def remove_relay_peer(self):
        """Remove a relay peer from WireGuard and the persistent registry"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_error_response(400, 'Bad Request', 'Request body is empty')
                return
            
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            if 'public_key' not in data:
                self.send_error_response(400, 'Missing Field', 'public_key is required')
                return
            
            public_key = data['public_key']
            
            # Check if peer exists in relay registry
            with relay_peer_lock:
                if public_key not in relay_peer_registry:
                    self.send_error_response(
                        404,
                        'Relay Peer Not Found',
                        'Public key not found in relay peer registry'
                    )
                    return
            
            # Remove from WireGuard
            result = subprocess.run(
                ['wg', 'set', WIREGUARD_INTERFACE, 'peer', public_key, 'remove'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to remove relay peer: {result.stderr}")
                self.send_error_response(
                    500,
                    'Failed to Remove Relay Peer',
                    result.stderr or 'Unknown error'
                )
                return
            
            # Save WireGuard configuration
            try:
                subprocess.run(
                    ['wg-quick', 'save', WIREGUARD_INTERFACE],
                    capture_output=True,
                    timeout=5
                )
            except Exception as e:
                logger.warning(f"Failed to save WireGuard config: {e}")
            
            # Remove from persistent registry
            relay_id = 'unknown'
            with relay_peer_lock:
                entry = relay_peer_registry.pop(public_key, None)
                if entry:
                    relay_id = entry.get('relay_id', 'unknown')
                save_peer_registry()

            # Clean up registration tracking and metadata
            with peer_registration_lock:
                peer_registration_times.pop(public_key, None)
            remove_peer_metadata(public_key)
            
            logger.info(
                f"✅ Removed relay peer: {public_key[:16]}... (relay: {relay_id})"
            )
            
            self.send_json_response({
                'success': True,
                'peer_public_key': public_key[:16] + '...',
                'relay_id': relay_id
            })
            
        except json.JSONDecodeError as e:
            self.send_error_response(400, 'Invalid JSON', str(e))
        except subprocess.TimeoutExpired:
            self.send_error_response(504, 'Command Timeout', 'WireGuard command timed out')
        except Exception as e:
            logger.error(f"Remove relay peer error: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    
    def get_relay_peers(self):
        """Get all registered relay peers with their status"""
        try:
            current_time = int(time.time())
            peers = []
            
            with relay_peer_lock:
                for pub_key, info in relay_peer_registry.items():
                    peers.append({
                        'public_key': pub_key[:16] + '...',
                        'relay_id': info.get('relay_id', 'unknown'),
                        'endpoint': info.get('endpoint', ''),
                        'allowed_ips': info.get('allowed_ips', ''),
                        'added_at': info.get('added_at', 0),
                        'age_seconds': current_time - info.get('added_at', current_time)
                    })
            
            self.send_json_response({
                'relay_id': RELAY_ID,
                'relay_peer_count': len(peers),
                'relay_peers': peers
            })
            
        except Exception as e:
            logger.error(f"Get relay peers error: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    
    def remove_cgnat_peer(self):
        """Remove CGNAT node peer"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_error_response(400, 'Bad Request', 'Request body is empty')
                return
            
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            if 'public_key' not in data:
                self.send_error_response(400, 'Missing Field', 'public_key is required')
                return
            
            public_key = data['public_key']
            
            # Remove peer
            result = subprocess.run(
                ['wg', 'set', WIREGUARD_INTERFACE, 'peer', public_key, 'remove'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to remove peer: {result.stderr}")
                self.send_error_response(
                    500,
                    'Failed to Remove Peer',
                    result.stderr or 'Unknown error'
                )
                return
            
            # Save configuration
            try:
                subprocess.run(
                    ['wg-quick', 'save', WIREGUARD_INTERFACE],
                    capture_output=True,
                    timeout=5
                )
            except Exception as e:
                logger.warning(f"Failed to save WireGuard config: {e}")
            
            # Clean up registration tracking and metadata
            with peer_registration_lock:
                if public_key in peer_registration_times:
                    del peer_registration_times[public_key]

            remove_peer_metadata(public_key)

            logger.info(f"Removed peer: {public_key[:16]}...")

            self.send_json_response({
                'success': True,
                'peer_public_key': public_key[:16] + '...'
            })

        except json.JSONDecodeError as e:
            self.send_error_response(400, 'Invalid JSON', str(e))
        except subprocess.TimeoutExpired:
            self.send_error_response(504, 'Command Timeout', 'WireGuard command timed out')
        except Exception as e:
            logger.error(f"Remove peer error: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    
    def manual_cleanup(self):
        """
        Manually trigger peer cleanup
        Uses consistent STALE_THRESHOLD_SECONDS and respects grace period
        """
        try:
            current_time = int(time.time())
            
            # Get current peers from WireGuard
            result = subprocess.run(
                ['wg', 'show', WIREGUARD_INTERFACE, 'dump'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                self.send_error_response(500, 'WireGuard Error', result.stderr)
                return
            
            lines = result.stdout.strip().split('\n')
            removed = []
            kept = []
            skipped_new = []
            
            # Skip interface line, process peers
            for line in lines[1:]:
                if not line.strip():
                    continue
                
                fields = line.split('\t')
                if len(fields) < 7:
                    continue
                
                public_key = fields[0]
                allowed_ips = fields[3]
                latest_handshake = int(fields[4]) if fields[4] else 0
                
                # Never remove orchestrator peer
                if ORCHESTRATOR_IP in allowed_ips:
                    kept.append({
                        'public_key': public_key[:16] + '...',
                        'allowed_ips': allowed_ips,
                        'reason': 'orchestrator'
                    })
                    continue
                
                # Never remove relay peers (persistent, managed via API)
                if is_relay_peer(public_key):
                    kept.append({
                        'public_key': public_key[:16] + '...',
                        'allowed_ips': allowed_ips,
                        'reason': 'relay_peer'
                    })
                    continue

                # Never remove system-vm peers (DHT VMs, BlockStore VMs)
                peer_type, _ = classify_peer(public_key, allowed_ips)
                if peer_type == 'system-vm':
                    kept.append({
                        'public_key': public_key[:16] + '...',
                        'allowed_ips': allowed_ips,
                        'reason': 'system_vm'
                    })
                    continue

                # Check grace period
                peer_age = get_peer_age(public_key, current_time)
                
                if peer_age < PEER_REGISTRATION_GRACE_PERIOD:
                    skipped_new.append({
                        'public_key': public_key[:16] + '...',
                        'allowed_ips': allowed_ips,
                        'age_seconds': int(peer_age)
                    })
                    continue
                
                # Calculate handshake status
                if latest_handshake == 0:
                    time_since_handshake = float('inf')
                    handshake_status = 'never'
                else:
                    time_since_handshake = current_time - latest_handshake
                    handshake_status = f'{time_since_handshake}s ago'
                
                if time_since_handshake > STALE_THRESHOLD_SECONDS:
                    # Remove stale peer
                    remove_result = subprocess.run(
                        ['wg', 'set', WIREGUARD_INTERFACE, 'peer', public_key, 'remove'],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    
                    if remove_result.returncode == 0:
                        removed.append({
                            'public_key': public_key[:16] + '...',
                            'allowed_ips': allowed_ips,
                            'handshake': handshake_status
                        })
                        remove_peer_metadata(public_key)
                        logger.info(f"Removed stale peer: {public_key[:16]}... ({allowed_ips})")
                    else:
                        logger.error(f"Failed to remove peer {public_key[:16]}...: {remove_result.stderr}")
                else:
                    kept.append({
                        'public_key': public_key[:16] + '...',
                        'allowed_ips': allowed_ips,
                        'handshake': handshake_status
                    })
            
            # Save configuration if we removed any
            if removed:
                try:
                    subprocess.run(
                        ['wg-quick', 'save', WIREGUARD_INTERFACE],
                        capture_output=True,
                        timeout=5
                    )
                except Exception as e:
                    logger.warning(f"Failed to save WireGuard config: {e}")
            
            self.send_json_response({
                'success': True,
                'removed_count': len(removed),
                'kept_count': len(kept),
                'skipped_new_count': len(skipped_new),
                'removed': removed,
                'kept': kept,
                'skipped_new': skipped_new
            })
            
        except subprocess.TimeoutExpired:
            self.send_error_response(504, 'Command Timeout', 'WireGuard command timed out')
        except Exception as e:
            logger.error(f"Cleanup error: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    
    def add_port_forward(self):
        """
        Add port forwarding rule via iptables for CGNAT traffic
        Forwards from public port on relay VM to tunnel IP through WireGuard
        """
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_error_response(400, 'Bad Request', 'Request body is empty')
                return
            
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            # Validate required fields
            required_fields = ['public_port', 'tunnel_ip', 'tunnel_port', 'protocol']
            missing_fields = [f for f in required_fields if f not in data]
            
            if missing_fields:
                self.send_error_response(
                    400,
                    'Missing Required Fields',
                    f"Missing fields: {', '.join(missing_fields)}"
                )
                return
            
            public_port = int(data['public_port'])
            tunnel_ip = data['tunnel_ip']
            tunnel_port = int(data['tunnel_port'])
            protocol = data['protocol'].lower()  # 'tcp' or 'udp'
            
            # Validate protocol
            if protocol not in ['tcp', 'udp', 'both']:
                self.send_error_response(400, 'Invalid Protocol', 'Protocol must be tcp, udp, or both')
                return
            
            # Validate tunnel IP (should be 10.20.x.x)
            if not tunnel_ip.startswith('10.20.'):
                self.send_error_response(400, 'Invalid Tunnel IP', 'Tunnel IP must be in 10.20.0.0/16 range')
                return
            
            # Ensure DECLOUD_PORT_FWD chain exists
            subprocess.run(
                ['iptables', '-t', 'nat', '-N', 'DECLOUD_PORT_FWD'],
                capture_output=True,
                timeout=5
            )
            # Ignore error if chain already exists
            
            # Ensure the chain is called from PREROUTING
            result = subprocess.run(
                ['iptables', '-t', 'nat', '-C', 'PREROUTING', '-j', 'DECLOUD_PORT_FWD'],
                capture_output=True,
                timeout=5
            )
            if result.returncode != 0:
                subprocess.run(
                    ['iptables', '-t', 'nat', '-I', 'PREROUTING', '-j', 'DECLOUD_PORT_FWD'],
                    capture_output=True,
                    timeout=5
                )
            
            protocols = []
            if protocol == 'both':
                protocols = ['tcp', 'udp']
            else:
                protocols = [protocol]
            
            for proto in protocols:
                # Create DNAT rule in custom chain
                cmd = [
                    'iptables', '-t', 'nat', '-A', 'DECLOUD_PORT_FWD',
                    '-p', proto, '--dport', str(public_port),
                    '-j', 'DNAT', '--to-destination', f'{tunnel_ip}:{tunnel_port}'
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                
                if result.returncode != 0:
                    logger.error(f"Failed to add {proto} DNAT rule: {result.stderr}")
                    self.send_error_response(
                        500,
                        'Failed to Add Port Forward',
                        f"Failed to add {proto} rule: {result.stderr}"
                    )
                    return
                
                # Add FORWARD rule to allow traffic
                cmd_forward = [
                    'iptables', '-A', 'FORWARD',
                    '-p', proto, '-d', tunnel_ip, '--dport', str(tunnel_port),
                    '-j', 'ACCEPT'
                ]
                
                subprocess.run(cmd_forward, capture_output=True, timeout=5)
                
                logger.info(f"✅ Added {proto} port forward: :{public_port} → {tunnel_ip}:{tunnel_port}")
            
            # Add MASQUERADE rule for WireGuard interface (needed for return traffic)
            # Check if MASQUERADE rule already exists for wg-relay-server
            masq_check = subprocess.run(
                ['iptables', '-t', 'nat', '-C', 'POSTROUTING', '-o', 'wg-relay-server', '-j', 'MASQUERADE'],
                capture_output=True,
                timeout=5
            )
            
            if masq_check.returncode != 0:
                # Rule doesn't exist, add it
                masq_cmd = [
                    'iptables', '-t', 'nat', '-A', 'POSTROUTING',
                    '-o', 'wg-relay-server',
                    '-j', 'MASQUERADE'
                ]
                
                result = subprocess.run(masq_cmd, capture_output=True, text=True, timeout=5)
                
                if result.returncode == 0:
                    logger.info("✅ Added MASQUERADE rule for wg-relay-server")
                else:
                    logger.warning(f"Failed to add MASQUERADE rule: {result.stderr}")
            
            # Save iptables rules
            try:
                subprocess.run(
                    ['netfilter-persistent', 'save'],
                    capture_output=True,
                    timeout=5
                )
            except Exception as e:
                logger.warning(f"Failed to persist iptables rules: {e}")
            
            self.send_json_response({
                'success': True,
                'public_port': public_port,
                'tunnel_ip': tunnel_ip,
                'tunnel_port': tunnel_port,
                'protocol': protocol
            })
            
        except json.JSONDecodeError as e:
            self.send_error_response(400, 'Invalid JSON', str(e))
        except subprocess.TimeoutExpired:
            self.send_error_response(504, 'Command Timeout', 'iptables command timed out')
        except ValueError as e:
            self.send_error_response(400, 'Invalid Value', str(e))
        except Exception as e:
            logger.error(f"Add port forward error: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    
    def remove_port_forward(self):
        """Remove port forwarding rule via iptables"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_error_response(400, 'Bad Request', 'Request body is empty')
                return
            
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            # Validate required fields
            required_fields = ['public_port', 'tunnel_ip', 'tunnel_port', 'protocol']
            missing_fields = [f for f in required_fields if f not in data]
            
            if missing_fields:
                self.send_error_response(
                    400,
                    'Missing Required Fields',
                    f"Missing fields: {', '.join(missing_fields)}"
                )
                return
            
            public_port = int(data['public_port'])
            tunnel_ip = data['tunnel_ip']
            tunnel_port = int(data['tunnel_port'])
            protocol = data['protocol'].lower()
            
            protocols = []
            if protocol == 'both':
                protocols = ['tcp', 'udp']
            else:
                protocols = [protocol]
            
            for proto in protocols:
                # Remove DNAT rule
                cmd = [
                    'iptables', '-t', 'nat', '-D', 'DECLOUD_PORT_FWD',
                    '-p', proto, '--dport', str(public_port),
                    '-j', 'DNAT', '--to-destination', f'{tunnel_ip}:{tunnel_port}'
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                
                if result.returncode != 0:
                    logger.warning(f"Failed to remove {proto} DNAT rule: {result.stderr}")
                
                # Remove FORWARD rule
                cmd_forward = [
                    'iptables', '-D', 'FORWARD',
                    '-p', proto, '-d', tunnel_ip, '--dport', str(tunnel_port),
                    '-j', 'ACCEPT'
                ]
                
                subprocess.run(cmd_forward, capture_output=True, timeout=5)
                
                logger.info(f"✅ Removed {proto} port forward: :{public_port} → {tunnel_ip}:{tunnel_port}")
            
            # Save iptables rules
            try:
                subprocess.run(
                    ['netfilter-persistent', 'save'],
                    capture_output=True,
                    timeout=5
                )
            except Exception as e:
                logger.warning(f"Failed to persist iptables rules: {e}")
            
            self.send_json_response({
                'success': True,
                'public_port': public_port,
                'tunnel_ip': tunnel_ip,
                'tunnel_port': tunnel_port,
                'protocol': protocol
            })
            
        except json.JSONDecodeError as e:
            self.send_error_response(400, 'Invalid JSON', str(e))
        except subprocess.TimeoutExpired:
            self.send_error_response(504, 'Command Timeout', 'iptables command timed out')
        except ValueError as e:
            self.send_error_response(400, 'Invalid Value', str(e))
        except Exception as e:
            logger.error(f"Remove port forward error: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    

    def flush_port_forwards(self):
        """
        Flush all DeCloud port forwarding rules
        Used during reconciliation to clear stale rules
        """
        try:
            logger.info("POST /api/relay/flush-port-forwards")
            
            # Flush DECLOUD_PORT_FWD chain in NAT table
            flush_cmd = [
                'iptables', '-t', 'nat', '-F', 'DECLOUD_PORT_FWD'
            ]
            
            result = subprocess.run(flush_cmd, capture_output=True, text=True, timeout=5)
            
            if result.returncode != 0:
                logger.error(f"Failed to flush DECLOUD_PORT_FWD chain: {result.stderr}")
                self.send_error_response(
                    500,
                    'Flush Failed',
                    f"Failed to flush port forwarding rules: {result.stderr}")
                return
            
            logger.info("✅ Flushed all DECLOUD_PORT_FWD rules")
            self.send_json_response({
                'success': True,
                'message': 'Port forwarding rules flushed successfully'
            })
            
        except subprocess.TimeoutExpired:
            self.send_error_response(504, 'Command Timeout', 'iptables command timed out')
        except Exception as e:
            logger.error(f"Flush port forwards error: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    

    def get_wireguard_status(self):
        """Get WireGuard interface status with peer classification"""
        try:
            result = subprocess.run(
                ['wg', 'show', WIREGUARD_INTERFACE, 'dump'],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode != 0:
                logger.error(f"wg show failed: {result.stderr}")
                return {
                    'interface': WIREGUARD_INTERFACE,
                    'wireguard_up': False,
                    'relay_id': RELAY_ID,
                    'region': RELAY_REGION,
                    'peer_count': 0,
                    'cgnat_node_count': 0,
                    'system_vm_count': 0,
                    'max_capacity': RELAY_CAPACITY,
                    'available_slots': RELAY_CAPACITY,
                    'peers': []
                }

            lines = result.stdout.strip().split('\n')
            peers = []

            current_time = time.time()

            for line in lines[1:]:
                if not line.strip():
                    continue

                fields = line.split('\t')
                if len(fields) >= 7:
                    public_key = fields[0]
                    allowed_ips = fields[3] if fields[3] != '(none)' else None
                    peer_type, parent_node_id = classify_peer(public_key, allowed_ips)
                    meta = get_peer_metadata(public_key)
                    reg_age = get_peer_age(public_key, current_time)

                    peers.append({
                        'public_key': public_key,
                        'endpoint': fields[2] if fields[2] != '(none)' else None,
                        'allowed_ips': allowed_ips,
                        'latest_handshake': int(fields[4]) if fields[4] and fields[4] != '0' else 0,
                        'rx_bytes': int(fields[5]) if fields[5] else 0,
                        'tx_bytes': int(fields[6]) if fields[6] else 0,
                        'peer_type': peer_type,
                        'parent_node_id': parent_node_id,
                        'description': meta.get('description', '') if meta else '',
                        'registration_age_seconds': int(reg_age) if reg_age != float('inf') else None,
                        # None means peer predates tracking (registered before last service start)
                    })

            # Count by type — only cgnat-node counts toward capacity
            cgnat_count = sum(1 for p in peers if p['peer_type'] == 'cgnat-node')
            system_vm_count = sum(1 for p in peers if p['peer_type'] == 'system-vm')

            return {
                'interface': WIREGUARD_INTERFACE,
                'wireguard_up': True,
                'relay_id': RELAY_ID,
                'region': RELAY_REGION,
                'peer_count': len(peers),
                'cgnat_node_count': cgnat_count,
                'system_vm_count': system_vm_count,
                'max_capacity': RELAY_CAPACITY,
                'available_slots': max(0, RELAY_CAPACITY - cgnat_count),
                'peers': peers
            }

        except Exception as e:
            logger.error(f"Error getting WireGuard status: {e}", exc_info=True)
            return {
                'interface': WIREGUARD_INTERFACE,
                'wireguard_up': False,
                'relay_id': RELAY_ID,
                'region': RELAY_REGION,
                'peer_count': 0,
                'cgnat_node_count': 0,
                'system_vm_count': 0,
                'max_capacity': RELAY_CAPACITY,
                'available_slots': RELAY_CAPACITY,
                'peers': [],
                'error': str(e)
            }
    
    def serve_dashboard(self):
        """Serve dashboard HTML"""
        try:
            dashboard_path = os.path.join(STATIC_DIR, 'dashboard.html')
            
            if not os.path.exists(dashboard_path):
                self.send_error_response(404, 'Not Found', 'Dashboard file not found')
                return
            
            with open(dashboard_path, 'rb') as f:
                content = f.read()
            
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content)
            
        except Exception as e:
            logger.error(f"Error serving dashboard: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    
    def serve_static_file(self, path):
        """Serve static files (CSS, JS, etc.)"""
        try:
            # Remove /static/ prefix
            file_path = path.replace('/static/', '', 1)
            full_path = os.path.join(STATIC_DIR, file_path)
            
            # Security: prevent directory traversal
            if not os.path.abspath(full_path).startswith(os.path.abspath(STATIC_DIR)):
                self.send_error_response(403, 'Forbidden', 'Access denied')
                return
            
            if not os.path.exists(full_path):
                self.send_error_response(404, 'Not Found', f'File not found: {file_path}')
                return
            
            # Determine content type
            content_type = 'application/octet-stream'
            if full_path.endswith('.css'):
                content_type = 'text/css'
            elif full_path.endswith('.js'):
                content_type = 'application/javascript'
            elif full_path.endswith('.json'):
                content_type = 'application/json'
            
            with open(full_path, 'rb') as f:
                content = f.read()
            
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self.wfile.write(content)
            
        except Exception as e:
            logger.error(f"Error serving static file: {e}", exc_info=True)
            self.send_error_response(500, 'Internal Error', str(e))
    
    # ==================== Response Helpers ====================
    
    def send_json_response(self, data, status_code=200):
        """Send JSON response"""
        json_data = json.dumps(data, indent=2)
        
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(json_data.encode('utf-8')))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(json_data.encode('utf-8'))
    
    def send_error_response(self, status_code, error_type, message):
        """Send error response"""
        self.send_json_response({
            'error': error_type,
            'message': message,
            'status': status_code
        }, status_code=status_code)


# ==================== Server Startup ====================
def main():
    """Start the relay API server with background cleanup"""
    
    # Load persistent registries
    load_peer_registry()
    load_peer_metadata()
    
    if not os.path.exists(STATIC_DIR):
        logger.error(f"Static directory not found: {STATIC_DIR}")
        logger.error("Dashboard files may not be accessible")
    
    # Start background cleanup thread if enabled
    cleanup_thread = None
    
    if CLEANUP_ENABLED:
        cleanup_thread = PeerCleanupThread()
        cleanup_thread.start()
        logger.info("✅ Background peer cleanup enabled")
    else:
        logger.warning("⚠️  Background peer cleanup disabled")
    
    # Start HTTP server
    server_address = ('0.0.0.0', LISTEN_PORT)
    httpd = HTTPServer(server_address, RelayAPIHandler)
    
    logger.info('=' * 60)
    logger.info('DeCloud Relay VM Management API')
    logger.info('=' * 60)
    logger.info(f'  Version:      3.0.0 (PEER CLASSIFICATION)')
    logger.info(f'  Relay ID:     {RELAY_ID}')
    logger.info(f'  Relay Name:   {RELAY_NAME}')
    logger.info(f'  Region:       {RELAY_REGION}')
    logger.info(f'  Capacity:     {RELAY_CAPACITY} nodes')
    logger.info(f'  Interface:    {WIREGUARD_INTERFACE}')
    logger.info(f'  Listen:       http://0.0.0.0:{LISTEN_PORT}')
    logger.info(f'  Dashboard:    http://0.0.0.0:{LISTEN_PORT}/')
    logger.info(f'  Static Files: {STATIC_DIR}')
    if CLEANUP_ENABLED:
        logger.info(f'  🧹 Cleanup:    Every {CLEANUP_INTERVAL_SECONDS}s ({CLEANUP_INTERVAL_SECONDS // 60} min)')
        logger.info(f'  ⏱️  Stale After: {STALE_THRESHOLD_SECONDS}s ({STALE_THRESHOLD_SECONDS // 60} min)')
        logger.info(f'  ✅ Grace Period: {PEER_REGISTRATION_GRACE_PERIOD}s for new peers')
    logger.info('=' * 60)
    logger.info('✅ Server started successfully')
    logger.info('')
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info('\n🛑 Shutting down server...')
        
        if cleanup_thread:
            cleanup_thread.stop()
            cleanup_thread.join(timeout=5)

        watchdog_thread.stop()
        watchdog_thread.join(timeout=5)
        
        httpd.shutdown()
        logger.info('✅ Server stopped')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)