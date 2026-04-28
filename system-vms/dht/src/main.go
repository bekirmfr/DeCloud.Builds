package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"crypto/sha256"
	"math/big"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	stded25519 "crypto/ed25519"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/ipfs/go-cid"
	leveldb "github.com/ipfs/go-ds-leveldb"
	"github.com/libp2p/go-libp2p"
	dht "github.com/libp2p/go-libp2p-kad-dht"
	pubsub "github.com/libp2p/go-libp2p-pubsub"
	"github.com/libp2p/go-libp2p/core/crypto"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/peer"
	"github.com/libp2p/go-libp2p/p2p/discovery/mdns"
	multiaddr "github.com/multiformats/go-multiaddr"
)

const (
	protocolPrefix = "/decloud"
	keyFileName    = "identity.key"
)

// ═══════════════════════════════════════════════════════════════════
// Diagnostic event log — ring buffer for real-time debugging
// ═══════════════════════════════════════════════════════════════════

// DiagEvent is a single timestamped diagnostic event.
type DiagEvent struct {
	TS      string                 `json:"ts"`
	Event   string                 `json:"event"`
	Details map[string]interface{} `json:"details,omitempty"`
}

// DiagLog is a thread-safe fixed-capacity ring buffer (newest overwrites oldest).
type DiagLog struct {
	mu     sync.RWMutex
	events []DiagEvent
	cap    int
	head   int
	count  int
}

func newDiagLog(capacity int) *DiagLog {
	return &DiagLog{cap: capacity, events: make([]DiagEvent, capacity)}
}

func (l *DiagLog) Add(event string, details map[string]interface{}) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.events[l.head] = DiagEvent{
		TS:      time.Now().UTC().Format(time.RFC3339),
		Event:   event,
		Details: details,
	}
	l.head = (l.head + 1) % l.cap
	if l.count < l.cap {
		l.count++
	}
}

// Snapshot returns events in chronological order (oldest → newest).
func (l *DiagLog) Snapshot() []DiagEvent {
	l.mu.RLock()
	defer l.mu.RUnlock()
	if l.count == 0 {
		return nil
	}
	result := make([]DiagEvent, l.count)
	if l.count < l.cap {
		copy(result, l.events[:l.count])
	} else {
		for i := 0; i < l.cap; i++ {
			result[i] = l.events[(l.head+i)%l.cap]
		}
	}
	return result
}

// DHTDiagCounters holds aggregate diagnostic counters.
type DHTDiagCounters struct {
	// Bootstrap
	BootstrapAttempts int64     `json:"bootstrapAttempts"`
	BootstrapSuccess  int64     `json:"bootstrapSuccess"`
	LastBootstrapAt   time.Time `json:"lastBootstrapAt,omitempty"`
	// Peer lifecycle
	PeerConnects    int64 `json:"peerConnects"`
	PeerDisconnects int64 `json:"peerDisconnects"`
	// Provider lookups (from /providers/ API)
	ProviderLookups    int64 `json:"providerLookups"`
	ProviderLookupFail int64 `json:"providerLookupFail"`
	// GossipSub relay
	GossipSubMessages int64     `json:"gossipSubMessages"`
	LastGossipSubAt   time.Time `json:"lastGossipSubAt,omitempty"`
}

// ═══════════════════════════════════════════════════════════════════
// Config and NodeState
// ═══════════════════════════════════════════════════════════════════

// Config holds the DHT node configuration from environment variables.
type Config struct {
	ListenPort     string
	APIPort        string
	AdvertiseIP    string
	BootstrapPeers string
	DataDir        string
	NodeID         string
	Region         string
}

// NodeState tracks runtime state of the DHT node.
type NodeState struct {
	mu             sync.RWMutex
	host           host.Host
	dht            *dht.IpfsDHT
	pubsub         *pubsub.PubSub
	eventTopic     *pubsub.Topic
	startTime      time.Time
	connectedPeers int
	status         string

	// Diagnostics
	diagLog *DiagLog
	diag    DHTDiagCounters
}

// ═══════════════════════════════════════════════════════════════════
// main
// ═══════════════════════════════════════════════════════════════════

func main() {
	log.SetFlags(log.Ldate | log.Ltime | log.Lmsgprefix)
	log.SetPrefix("[dht-node] ")

	cfg := loadConfig()
	log.Printf("Starting DeCloud DHT node (nodeId=%s, region=%s)", cfg.NodeID, cfg.Region)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Load or generate persistent identity
	privKey, err := loadOrCreateIdentity(cfg.DataDir)
	if err != nil {
		log.Fatalf("Failed to load identity: %v", err)
	}

	// Build libp2p host
	listenAddr := fmt.Sprintf("/ip4/0.0.0.0/tcp/%s", cfg.ListenPort)
	opts := []libp2p.Option{
		libp2p.Identity(privKey),
		libp2p.ListenAddrStrings(listenAddr),
	}

	if cfg.AdvertiseIP != "" {
		extAddr := fmt.Sprintf("/ip4/%s/tcp/%s", cfg.AdvertiseIP, cfg.ListenPort)
		extMA, err := multiaddr.NewMultiaddr(extAddr)
		if err == nil {
			// Only advertise the WG tunnel IP — don't expose unreachable
			// libvirt bridge or localhost addresses to other peers.
			opts = append(opts, libp2p.AddrsFactory(func(addrs []multiaddr.Multiaddr) []multiaddr.Multiaddr {
				return []multiaddr.Multiaddr{extMA}
			}))
		}
	}

	h, err := libp2p.New(opts...)
	if err != nil {
		log.Fatalf("Failed to create libp2p host: %v", err)
	}
	defer h.Close()

	log.Printf("Peer ID: %s", h.ID())
	for _, addr := range h.Addrs() {
		log.Printf("Listening on: %s/p2p/%s", addr, h.ID())
	}

	// Initialize persistent LevelDB datastore for DHT key-value records.
	// Pure Go (syndtr/goleveldb) — works with CGO_ENABLED=0 cross-compilation.
	dhtDatastorePath := filepath.Join(cfg.DataDir, "datastore")
	if err := os.MkdirAll(dhtDatastorePath, 0o700); err != nil {
		log.Fatalf("Failed to create DHT datastore directory: %v", err)
	}
	dhtDS, err := leveldb.NewDatastore(dhtDatastorePath, nil)
	if err != nil {
		log.Fatalf("Failed to open LevelDB datastore: %v", err)
	}
	defer dhtDS.Close()
	log.Printf("Opened persistent datastore at %s", dhtDatastorePath)

	// Initialize Kademlia DHT in server mode
	kadDHT, err := dht.New(ctx, h,
		dht.Mode(dht.ModeServer),
		dht.ProtocolPrefix(protocolPrefix),
		dht.Datastore(dhtDS),
	)
	if err != nil {
		log.Fatalf("Failed to create DHT: %v", err)
	}

	if err := kadDHT.Bootstrap(ctx); err != nil {
		log.Fatalf("Failed to bootstrap DHT: %v", err)
	}

	// Connect to bootstrap peers
	connectBootstrapPeers(ctx, h, cfg.BootstrapPeers)

	// Initialize GossipSub
	ps, err := pubsub.NewGossipSub(ctx, h)
	if err != nil {
		log.Fatalf("Failed to create GossipSub: %v", err)
	}

	// Join the DeCloud events topic
	topic, err := ps.Join(fmt.Sprintf("%s/events/%s", protocolPrefix, cfg.Region))
	if err != nil {
		log.Fatalf("Failed to join events topic: %v", err)
	}

	// Subscribe to receive events (required for topic participation)
	sub, err := topic.Subscribe()
	if err != nil {
		log.Fatalf("Failed to subscribe to events topic: %v", err)
	}

	state := &NodeState{
		host:       h,
		dht:        kadDHT,
		pubsub:     ps,
		eventTopic: topic,
		startTime:  time.Now(),
		status:     "active",
		diagLog:    newDiagLog(200),
	}

	go handleEvents(ctx, sub, state)

	// Start mDNS discovery for local peers
	mdnsService := mdns.NewMdnsService(h, protocolPrefix, &mdnsNotifee{h: h, ctx: ctx})
	if err := mdnsService.Start(); err != nil {
		log.Printf("mDNS discovery failed to start (non-fatal): %v", err)
	} else {
		defer mdnsService.Close()
	}

	// Start background peer counter
	go trackPeers(ctx, state)

	// Subscribe to blockstore GossipSub topics so this DHT VM participates
	// in the mesh as a relay between blockstore nodes.
	// DHT VMs sit between blockstores in the libp2p topology — without
	// subscribing, GossipSub treats them as dead ends and messages never
	// propagate across nodes. No processing needed — subscription alone
	// is sufficient for mesh routing.
	for _, bsTopic := range []string{
		"decloud/blockstore/new-blocks",
		"decloud/blockstore/vm-deleted",
	} {
		if t, err := ps.Join(bsTopic); err == nil {
			if bsSub, err := t.Subscribe(); err == nil {
				go drainSubscription(ctx, bsSub, bsTopic, state)
			} else {
				log.Printf("Warning: failed to subscribe to %s: %v", bsTopic, err)
			}
		} else {
			log.Printf("Warning: failed to join topic %s: %v", bsTopic, err)
		}
	}

	// Start bootstrap retry goroutine — handles the race condition where
	// this node deployed before other DHT nodes' PeerIds were captured.
	// Periodically reads dynamic-peers file for runtime peer injection.
	go retryBootstrap(ctx, h, cfg, state)

	// Start HTTP API server (localhost-only — no external access)
	go startAPIServer(cfg.APIPort, state)

	log.Printf("DHT node is ready (peer ID: %s)", h.ID())

	// Wait for shutdown signal
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	log.Println("Shutting down DHT node...")
	state.mu.Lock()
	state.status = "shutting_down"
	state.mu.Unlock()

	cancel()
	kadDHT.Close()
}

// ═══════════════════════════════════════════════════════════════════
// Config loading
// ═══════════════════════════════════════════════════════════════════

func loadConfig() Config {
	return Config{
		ListenPort:     envOrDefault("DHT_LISTEN_PORT", "4001"),
		APIPort:        envOrDefault("DHT_API_PORT", "5080"),
		AdvertiseIP:    os.Getenv("DHT_ADVERTISE_IP"),
		BootstrapPeers: os.Getenv("DHT_BOOTSTRAP_PEERS"),
		DataDir:        envOrDefault("DHT_DATA_DIR", "/var/lib/decloud-dht"),
		NodeID:         os.Getenv("DECLOUD_NODE_ID"),
		Region:         envOrDefault("DECLOUD_REGION", "default"),
	}
}

func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// ═══════════════════════════════════════════════════════════════════
// Identity
// ═══════════════════════════════════════════════════════════════════

// loadOrCreateIdentity loads the Ed25519 peer identity.
// Priority:
//   1. NodeAgent obligation state (http://gateway:5100/api/obligations/dht/state)
//      — authoritative source, survives VM redeployments. Cached to disk after fetch.
//   2. Disk cache — used when NodeAgent is temporarily unreachable (e.g. agent restart).
//   3. Generate new key — first boot before obligation state is available.
func loadOrCreateIdentity(dataDir string) (crypto.PrivKey, error) {
	keyPath := filepath.Join(dataDir, keyFileName)

	// 1. Try NodeAgent obligation state
	if privKey, err := loadIdentityFromNodeAgent("dht"); err == nil {
		log.Printf("Loaded DHT identity from NodeAgent obligation state")
		cacheIdentityToDisk(privKey, keyPath)
		return privKey, nil
	} else {
		log.Printf("NodeAgent obligation state unavailable (%v) — falling back to disk", err)
	}

	// 2. Disk cache
	if data, err := os.ReadFile(keyPath); err == nil {
		if privKey, err := crypto.UnmarshalPrivateKey(data); err == nil {
			log.Printf("Loaded cached identity from %s", keyPath)
			return privKey, nil
		}
	}

	// 3. Generate new key (first boot)
	privKey, _, err := crypto.GenerateEd25519Key(rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("failed to generate Ed25519 key: %w", err)
	}
	log.Printf("Generated new Ed25519 identity (obligation state not yet available)")
	cacheIdentityToDisk(privKey, keyPath)
	return privKey, nil
}

// loadIdentityFromNodeAgent fetches the Ed25519 private key from the NodeAgent
// obligation state endpoint served over virbr0. The key is stored as a
// base64-encoded 32-byte Ed25519 seed in the state JSON.
func loadIdentityFromNodeAgent(role string) (crypto.PrivKey, error) {
	gateway, err := defaultGateway()
	if err != nil {
		return nil, fmt.Errorf("no default gateway: %w", err)
	}

	url := fmt.Sprintf("http://%s:5100/api/obligations/%s/state", gateway, role)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return nil, fmt.Errorf("NodeAgent request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("NodeAgent returned HTTP %d", resp.StatusCode)
	}

	var state struct {
		Ed25519PrivateKeyBase64 string `json:"ed25519PrivateKeyBase64"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&state); err != nil {
		return nil, fmt.Errorf("decode state: %w", err)
	}
	if state.Ed25519PrivateKeyBase64 == "" {
		return nil, fmt.Errorf("state missing ed25519PrivateKeyBase64")
	}

	seed, err := base64.StdEncoding.DecodeString(state.Ed25519PrivateKeyBase64)
	if err != nil {
		return nil, fmt.Errorf("decode Ed25519 seed: %w", err)
	}
	// obligation state stores a 32-byte seed — expand to 64-byte private key
	// before passing to go-libp2p which expects the full expanded key.
	if len(seed) == stded25519.SeedSize {
		seed = stded25519.NewKeyFromSeed(seed)
	}
	return crypto.UnmarshalEd25519PrivateKey(seed)
}

// defaultGateway reads the default route from /proc/net/route.
func defaultGateway() (string, error) {
	f, err := os.Open("/proc/net/route")
	if err != nil {
		return "", err
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	scanner.Scan() // skip header
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) >= 3 && fields[1] == "00000000" {
			b, err := hex.DecodeString(fields[2])
			if err != nil || len(b) < 4 {
				continue
			}
			return fmt.Sprintf("%d.%d.%d.%d", b[3], b[2], b[1], b[0]), nil
		}
	}
	return "", fmt.Errorf("default gateway not found")
}

// cacheIdentityToDisk writes the identity key to disk for resilience.
func cacheIdentityToDisk(privKey crypto.PrivKey, keyPath string) {
	keyBytes, err := crypto.MarshalPrivateKey(privKey)
	if err != nil {
		log.Printf("Warning: failed to marshal identity for disk cache: %v", err)
		return
	}
	_ = os.MkdirAll(filepath.Dir(keyPath), 0o700)
	if err := os.WriteFile(keyPath, keyBytes, 0o600); err != nil {
		log.Printf("Warning: failed to cache identity to disk: %v", err)
	}
}

// ═══════════════════════════════════════════════════════════════════
// Bootstrap
// ═══════════════════════════════════════════════════════════════════

func connectBootstrapPeers(ctx context.Context, h host.Host, peersStr string) {
	if peersStr == "" {
		log.Println("No bootstrap peers configured (first node in network)")
		return
	}

	peers := strings.Split(peersStr, ",")
	var connected int
	for _, p := range peers {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}

		ma, err := multiaddr.NewMultiaddr(p)
		if err != nil {
			log.Printf("Invalid bootstrap peer address %q: %v", p, err)
			continue
		}

		pi, err := peer.AddrInfoFromP2pAddr(ma)
		if err != nil {
			log.Printf("Failed to parse peer info from %q: %v", p, err)
			continue
		}

		if err := h.Connect(ctx, *pi); err != nil {
			log.Printf("Failed to connect to bootstrap peer %s: %v", pi.ID.String()[:12], err)
		} else {
			log.Printf("Connected to bootstrap peer: %s", pi.ID.String()[:12])
			connected++
		}
	}
	log.Printf("Connected to %d/%d bootstrap peers", connected, len(peers))
}

// ═══════════════════════════════════════════════════════════════════
// Event handling / GossipSub
// ═══════════════════════════════════════════════════════════════════

// handleEvents processes the region events topic. Now also updates diagnostic counters.
func handleEvents(ctx context.Context, sub *pubsub.Subscription, state *NodeState) {
	for {
		msg, err := sub.Next(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			log.Printf("Error reading from events subscription: %v", err)
			continue
		}
		// Log event receipt (could be extended to handle specific event types)
		log.Printf("Received event from %s (%d bytes)", msg.GetFrom().String()[:12], len(msg.Data))
	}
}

// drainSubscription consumes and discards messages from a subscription while
// tracking GossipSub relay statistics for the diagnostics endpoint.
func drainSubscription(ctx context.Context, sub *pubsub.Subscription, topicName string, state *NodeState) {
	defer sub.Cancel()
	for {
		msg, err := sub.Next(ctx)
		if err != nil {
			return
		}
		// Count relayed GossipSub messages for diagnostics
		if msg.ReceivedFrom != state.host.ID() {
			state.mu.Lock()
			state.diag.GossipSubMessages++
			state.diag.LastGossipSubAt = time.Now()
			state.mu.Unlock()
		}
	}
}

// ═══════════════════════════════════════════════════════════════════
// Peer tracking
// ═══════════════════════════════════════════════════════════════════

func trackPeers(ctx context.Context, state *NodeState) {
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			state.mu.Lock()
			prev := state.connectedPeers
			current := len(state.host.Network().Peers())
			state.connectedPeers = current

			if current > prev {
				delta := current - prev
				state.diag.PeerConnects += int64(delta)
				state.diagLog.Add("peer_connect", map[string]interface{}{
					"count": current,
					"delta": delta,
				})
			} else if current < prev {
				delta := prev - current
				state.diag.PeerDisconnects += int64(delta)
				state.diagLog.Add("peer_disconnect", map[string]interface{}{
					"count": current,
					"delta": delta,
				})
			}
			state.mu.Unlock()
		}
	}
}

// ═══════════════════════════════════════════════════════════════════
// Bootstrap retry
// ═══════════════════════════════════════════════════════════════════

// retryBootstrap periodically attempts to connect to peers when isolated.
// Handles two scenarios:
//  1. Bootstrap race: this node deployed before other DHT nodes' PeerIds were captured,
//     so it booted with 0 bootstrap peers. Retry original list in case they come online.
//  2. Dynamic peers: reads /var/lib/decloud-dht/dynamic-peers file for runtime injection.
//     The node agent can write new peers to this file when the orchestrator discovers them.
func retryBootstrap(ctx context.Context, h host.Host, cfg Config, state *NodeState) {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	dynamicPeersFile := filepath.Join(cfg.DataDir, "dynamic-peers")

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			// Check Kademlia routing table, not raw peer count.
			// Blockstores dial into DHT VMs as libp2p peers, so
			// h.Network().Peers() > 0 even when the routing table is
			// empty and dht.Provide() calls are all failing.
			if state.dht.RoutingTable().Size() > 0 {
				continue // routing table healthy, no retry needed
			}

			log.Println("Routing table empty — attempting peer discovery...")
			// Log retry attempt
			state.mu.Lock()
			state.diag.BootstrapAttempts++
			state.diagLog.Add("bootstrap_retry", map[string]interface{}{
				"reason": "isolated",
			})
			state.mu.Unlock()

			// Try original bootstrap peers
			if cfg.BootstrapPeers != "" {
				connectBootstrapPeers(ctx, h, cfg.BootstrapPeers)
			}

			// Try dynamic peers file (written by node agent)
			if data, err := os.ReadFile(dynamicPeersFile); err == nil {
				dynamicPeers := strings.TrimSpace(string(data))
				if dynamicPeers != "" {
					log.Printf("Found dynamic peers file with %d bytes", len(dynamicPeers))
					connectBootstrapPeers(ctx, h, dynamicPeers)
				}
			}

			// Check if we successfully connected after retry
			if state.dht.RoutingTable().Size() > 0 {
				state.mu.Lock()
				state.diag.BootstrapSuccess++
				state.diag.LastBootstrapAt = time.Now()
				state.diagLog.Add("bootstrap_success", map[string]interface{}{
					"peers": len(h.Network().Peers()),
				})
				state.mu.Unlock()
			}
		}
	}
}

// ═══════════════════════════════════════════════════════════════════
// HTTP API
// ═══════════════════════════════════════════════════════════════════

// startAPIServer runs the HTTP health/status API.
// Binds to 127.0.0.1 only — only localhost processes (dashboard, bootstrap-poll,
// health checks) can reach it. External peers communicate via the libp2p TCP
// port, not HTTP. No auth needed: if an attacker has code execution inside this
// system VM, they already have access to the identity key and can kill the process.
func startAPIServer(port string, state *NodeState) {
	mux := http.NewServeMux()

	// ── GET /health ──────────────────────────────────────────────────────────
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		state.mu.RLock()
		defer state.mu.RUnlock()

		resp := map[string]interface{}{
			"status":         state.status,
			"peerId":         state.host.ID().String(),
			"connectedPeers": state.connectedPeers,
			"uptime":         time.Since(state.startTime).String(),
			"uptimeSeconds":  int(time.Since(state.startTime).Seconds()),
			"addresses":      formatAddresses(state.host),
			"routingTable":   state.dht.RoutingTable().Size(),
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	// ── GET /peers ───────────────────────────────────────────────────────────
	mux.HandleFunc("/peers", func(w http.ResponseWriter, r *http.Request) {
		state.mu.RLock()
		defer state.mu.RUnlock()

		peers := state.host.Network().Peers()
		peerList := make([]string, len(peers))
		for i, p := range peers {
			peerList[i] = p.String()
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"count": len(peerList),
			"peers": peerList,
		})
	})

	// ── POST /connect ─────────────────────────────────────────────────────────
	mux.HandleFunc("/connect", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}

		var payload struct {
			Peers []string `json:"peers"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			http.Error(w, "invalid JSON", http.StatusBadRequest)
			return
		}

		if len(payload.Peers) == 0 {
			http.Error(w, "no peers provided", http.StatusBadRequest)
			return
		}

		results := make([]map[string]interface{}, 0, len(payload.Peers))
		var connected int
		for _, p := range payload.Peers {
			p = strings.TrimSpace(p)
			if p == "" {
				continue
			}

			ma, err := multiaddr.NewMultiaddr(p)
			if err != nil {
				results = append(results, map[string]interface{}{
					"addr":  p,
					"error": fmt.Sprintf("invalid multiaddr: %v", err),
				})
				continue
			}

			pi, err := peer.AddrInfoFromP2pAddr(ma)
			if err != nil {
				results = append(results, map[string]interface{}{
					"addr":  p,
					"error": fmt.Sprintf("invalid peer addr: %v", err),
				})
				continue
			}

			// Skip self
			if pi.ID == state.host.ID() {
				results = append(results, map[string]interface{}{
					"addr":    p,
					"skipped": "self",
				})
				continue
			}

			// Skip already-connected peers
			if state.host.Network().Connectedness(pi.ID) == 1 { // Connected
				results = append(results, map[string]interface{}{
					"addr":      p,
					"peerId":    pi.ID.String(),
					"connected": true,
					"skipped":   "already connected",
				})
				connected++
				continue
			}

			if err := state.host.Connect(context.Background(), *pi); err != nil {
				log.Printf("POST /connect: failed to connect to %s: %v", pi.ID.String()[:12], err)
				results = append(results, map[string]interface{}{
					"addr":      p,
					"peerId":    pi.ID.String(),
					"connected": false,
					"error":     err.Error(),
				})
			} else {
				log.Printf("POST /connect: connected to peer %s", pi.ID.String()[:12])
				results = append(results, map[string]interface{}{
					"addr":      p,
					"peerId":    pi.ID.String(),
					"connected": true,
				})
				connected++
			}
		}

		// Trigger Kademlia routing table refresh after new connections.
		// host.Connect() establishes the libp2p connection but does not
		// walk the new peer's k-buckets. Bootstrap() does the DHT walk
		// so routingTable > 0 immediately — essential for FindProviders
		// and provider record announcement to work correctly.
		if connected > 0 {
			state.mu.Lock()
			state.diag.BootstrapSuccess++
			state.diag.LastBootstrapAt = time.Now()
			state.diagLog.Add("bootstrap_connect", map[string]interface{}{
				"connected": connected,
				"total":     len(payload.Peers),
			})
			state.mu.Unlock()

			go func() {
				refreshCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
				defer cancel()
				if err := state.dht.Bootstrap(refreshCtx); err != nil {
					log.Printf("POST /connect: DHT bootstrap refresh error: %v", err)
				} else {
					log.Printf("POST /connect: DHT routing table refreshed after %d new peer(s)", connected)
				}
			}()
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"results":   results,
			"connected": connected,
			"total":     len(payload.Peers),
		})
	})

	// ── POST /publish ─────────────────────────────────────────────────────────
	mux.HandleFunc("/publish", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}

		var payload struct {
			Topic string `json:"topic"` // optional — defaults to eventTopic
			Data  string `json:"data"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			http.Error(w, "invalid JSON", http.StatusBadRequest)
			return
		}

		var publishTopic *pubsub.Topic
		if payload.Topic == "" {
			// Default: region events topic (backwards compatible)
			state.mu.RLock()
			publishTopic = state.eventTopic
			state.mu.RUnlock()
		} else {
			// Arbitrary topic — join on demand
			t, err := state.pubsub.Join(payload.Topic)
			if err != nil {
				http.Error(w, fmt.Sprintf("join topic failed: %v", err), http.StatusInternalServerError)
				return
			}
			publishTopic = t
		}

		if err := publishTopic.Publish(context.Background(), []byte(payload.Data)); err != nil {
			http.Error(w, fmt.Sprintf("publish failed: %v", err), http.StatusInternalServerError)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"status": "published"})
	})

	// ── GET /providers/{cid} ─────────────────────────────────────────────────
	// Query the DHT for provider records for a given CID.
	// Used by LazysyncManager (orchestrator) to audit chunk replication.
	// Returns provider count + peer IDs. Timeout: 30s.
	mux.HandleFunc("/providers/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}

		// Extract CID from path: /providers/{cid}
		cidStr := strings.TrimPrefix(r.URL.Path, "/providers/")
		cidStr = strings.TrimSpace(cidStr)
		if cidStr == "" {
			http.Error(w, "missing CID", http.StatusBadRequest)
			return
		}

		c, err := cid.Decode(cidStr)
		if err != nil {
			http.Error(w, fmt.Sprintf("invalid CID: %v", err), http.StatusBadRequest)
			return
		}

		// FindProvidersAsync returns a channel of peer.AddrInfo.
		// Cap at 20 providers — enough to verify replication factor.
		// 30s timeout guards against slow DHT walks.
		findCtx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
		defer cancel()

		state.mu.RLock()
		kadDHT := state.dht
		state.mu.RUnlock()

		providerCh := kadDHT.FindProvidersAsync(findCtx, c, 20)

		var providers []string
		for p := range providerCh {
			providers = append(providers, p.ID.String())
		}

		// Track provider lookup in diagnostics
		state.mu.Lock()
		shortCid := cidStr
		if len(shortCid) > 12 {
			shortCid = shortCid[:12]
		}
		if len(providers) == 0 {
			state.diag.ProviderLookupFail++
			state.diagLog.Add("provider_lookup_fail", map[string]interface{}{
				"cid": shortCid,
			})
		} else {
			state.diag.ProviderLookups++
			state.diagLog.Add("provider_lookup", map[string]interface{}{
				"cid":   shortCid,
				"count": len(providers),
			})
		}
		state.mu.Unlock()

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"cid":       cidStr,
			"count":     len(providers),
			"providers": providers,
		})
	})

	// ── GET /proximity/{cid} ─────────────────────────────────────────────────
	// Kademlia XOR proximity of this DHT node's peer ID to a given CID.
	// Used by the blockstore binary for neighborhood scan decisions and
	// diagnostics ("why did/didn't this node store block X?").
	mux.HandleFunc("/proximity/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}

		cidStr := strings.TrimPrefix(r.URL.Path, "/proximity/")
		cidStr = strings.TrimSpace(cidStr)
		if cidStr == "" {
			http.Error(w, "missing CID", http.StatusBadRequest)
			return
		}

		c, err := cid.Decode(cidStr)
		if err != nil {
			http.Error(w, fmt.Sprintf("invalid CID: %v", err), http.StatusBadRequest)
			return
		}

		state.mu.RLock()
		myID := state.host.ID()
		connectedPeers := state.host.Network().Peers()
		state.mu.RUnlock()

		// Compute XOR distance: SHA-256(myPeerID) XOR SHA-256(CID multihash).
		// Matches the Kademlia keyspace metric used internally by go-libp2p-kad-dht.
		myHash := sha256.Sum256([]byte(myID))
		cidHash := sha256.Sum256(c.Hash())

		xorBytes := make([]byte, 32)
		for i := range xorBytes {
			xorBytes[i] = myHash[i] ^ cidHash[i]
		}
		distance := new(big.Int).SetBytes(xorBytes)

		// Count connected peers that are closer to the CID than we are.
		closerCount := 0
		for _, p := range connectedPeers {
			pHash := sha256.Sum256([]byte(p))
			pXor := make([]byte, 32)
			for i := range pXor {
				pXor[i] = pHash[i] ^ cidHash[i]
			}
			if new(big.Int).SetBytes(pXor).Cmp(distance) < 0 {
				closerCount++
			}
		}

		const kValue = 20
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"cid":              cidStr,
			"distance":         fmt.Sprintf("0x%x", distance),
			"closerKnownPeers": closerCount,
			"estimatedRank":    closerCount + 1,
			"kValue":           kValue,
			"routingTableSize": state.dht.RoutingTable().Size(),
		})
	})

	// ── GET /diagnostics ─────────────────────────────────────────────────────
	// Full diagnostic snapshot: event log + aggregate counters + peer details.
	// Used by dashboard export and for debugging replication issues.
	mux.HandleFunc("/diagnostics", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "GET only", http.StatusMethodNotAllowed)
			return
		}

		state.mu.RLock()
		diag := state.diag // value copy
		connectedPeers := state.connectedPeers
		rtSize := state.dht.RoutingTable().Size()
		state.mu.RUnlock()

		// GossipSub topics this node has joined
		topics := state.pubsub.GetTopics()

		// Peer details with addresses
		peers := state.host.Network().Peers()
		peerDetails := make([]map[string]interface{}, 0, len(peers))
		for _, p := range peers {
			addrs := state.host.Network().Peerstore().Addrs(p)
			addrStrs := make([]string, len(addrs))
			for i, a := range addrs {
				addrStrs[i] = a.String()
			}
			peerDetails = append(peerDetails, map[string]interface{}{
				"id":    p.String(),
				"addrs": addrStrs,
			})
		}

		resp := map[string]interface{}{
			"timestamp":      time.Now().UTC().Format(time.RFC3339),
			"connectedPeers": connectedPeers,
			"routingTable": map[string]interface{}{
				"size": rtSize,
			},
			"gossipSub": map[string]interface{}{
				"topics":          topics,
				"messagesRelayed": diag.GossipSubMessages,
				"lastMessageAt":   diag.LastGossipSubAt,
			},
			"bootstrap": map[string]interface{}{
				"attempts":  diag.BootstrapAttempts,
				"successes": diag.BootstrapSuccess,
				"lastAt":    diag.LastBootstrapAt,
			},
			"peerEvents": map[string]interface{}{
				"connects":    diag.PeerConnects,
				"disconnects": diag.PeerDisconnects,
			},
			"providerLookups": map[string]interface{}{
				"success": diag.ProviderLookups,
				"fail":    diag.ProviderLookupFail,
			},
			"peers":    peerDetails,
			"eventLog": state.diagLog.Snapshot(),
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	addr := fmt.Sprintf("127.0.0.1:%s", port)
	log.Printf("HTTP API listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("HTTP API server failed: %v", err)
	}
}

// ═══════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════

func formatAddresses(h host.Host) []string {
	addrs := h.Addrs()
	result := make([]string, len(addrs))
	for i, a := range addrs {
		result[i] = fmt.Sprintf("%s/p2p/%s", a, h.ID())
	}
	return result
}

// mdnsNotifee handles mDNS peer discovery.
type mdnsNotifee struct {
	h   host.Host
	ctx context.Context
}

func (n *mdnsNotifee) HandlePeerFound(pi peer.AddrInfo) {
	if pi.ID == n.h.ID() {
		return
	}
	log.Printf("mDNS: discovered peer %s", pi.ID.String()[:12])
	if err := n.h.Connect(n.ctx, pi); err != nil {
		log.Printf("mDNS: failed to connect to %s: %v", pi.ID.String()[:12], err)
	}
}