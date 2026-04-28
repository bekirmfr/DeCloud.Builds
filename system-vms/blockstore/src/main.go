// DeCloud Block Store Node
//
// A libp2p node providing content-addressed distributed storage
// for VM disk overlay replication (lazysync) and AI model shard distribution.
//
// Storage:  FlatFS (IPFS-compatible, content-addressed blocks)
// Exchange: Bitswap (block fetching from network peers)
// Routing:  Kademlia DHT client (provider record announce/lookup)
// Events:   GossipSub subscription for `decloud/blockstore/new-blocks`
// API:      HTTP on 0.0.0.0:{BLOCKSTORE_API_PORT}
//
// LRU eviction at 85% capacity, hard refuse writes at 95%.
// Adaptive XOR pull threshold based on local capacity utilization.
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	stded25519 "crypto/ed25519"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	bsnetwork "github.com/ipfs/boxo/bitswap/network"
	"github.com/ipfs/boxo/bitswap"
	blockstore "github.com/ipfs/boxo/blockstore"
	blocks "github.com/ipfs/go-block-format"
	"github.com/ipfs/go-cid"
	"github.com/ipfs/go-datastore"
	"github.com/ipfs/go-datastore/mount"
	flatfs "github.com/ipfs/go-ds-flatfs"
	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/crypto"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	dht "github.com/libp2p/go-libp2p-kad-dht"
	pubsub "github.com/libp2p/go-libp2p-pubsub"
	"github.com/multiformats/go-multiaddr"
	"github.com/multiformats/go-multihash"
)

// ═══════════════════════════════════════════════════════════════════
// Constants
// ═══════════════════════════════════════════════════════════════════

const (
	GossipSubTopic         = "decloud/blockstore/new-blocks"
	GossipSubVmDelTopic    = "decloud/blockstore/vm-deleted"
	GossipSubPresenceTopic = "decloud/blockstore/presence"
	IdentityKeyFile     = "identity.key"
	PeerIDFile          = "peer-id"
	BlocksSubdir        = "blocks"
	DagsSubdir          = "dags"   // ResourceManifest JSON files
	OwnersSubdir        = "owners" // Per-VM CID owner index files
	ConfirmedSubdir = "confirmed" // Per-VM confirmed-remote CID files
	StorageDir          = "/var/lib/decloud-blockstore"

	// GC thresholds
	GCTriggerPercent = 85
	GCHardLimit      = 95

	// XOR proximity thresholds (fraction of keyspace, capacity-adaptive)
	XORThresholdFull   = 0.05
	XORThresholdMedium = 0.10
	XORThresholdLight  = 0.20

	// Block size for storage calculations (matches LazysyncDaemon BlockSizeBytes)
	BlockSizeBytes = 1024 * 1024

	// Bitswap fetch timeout
	BitswapTimeout = 30 * time.Second

	// Diagnostics ring buffer capacity
	diagLogCap = 300

	// Worker pool size for GossipSub-triggered bitswap fetches.
	// Fixed pool prevents goroutine explosion on large overlay bursts.
	GossipSubFetchWorkers = 4

	// ReannounceWorkers is separate from GossipSubFetchWorkers.
	// DHT provides are lightweight Kademlia round-trips (no data transfer),
	// so more concurrency is appropriate. Under relay stress, dhtProvide()
	// naturally slows down — workers self-throttle via blocking calls.
	ReannounceWorkers = 16

	// Buffered announcement queue fed by the GossipSub receive loop.
	// Sized to absorb a full large overlay burst without dropping.
	FetchQueueSize = 2000
)

// blockFetcher is the interface implemented by bitswap sessions.
// Defined locally to avoid importing the exchange package.
// Sessions send WANT messages to connected peers directly —
// no DHT FindProviders needed when the peer is already connected.
type blockFetcher interface {
	GetBlock(context.Context, cid.Cid) (blocks.Block, error)
	GetBlocks(context.Context, []cid.Cid) (<-chan blocks.Block, error)
}

// ═══════════════════════════════════════════════════════════════════
// Diagnostic event log
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

// DiagCounters holds aggregate diagnostic counters for the /diagnostics endpoint.
type DiagCounters struct {
	// GossipSub
	GossipSubReceived  int64     `json:"gossipSubReceived"`
	GossipSubPublished int64     `json:"gossipSubPublished"`
	LastGossipSubRxAt  time.Time `json:"lastGossipSubRxAt,omitempty"`
	LastGossipSubTxAt  time.Time `json:"lastGossipSubTxAt,omitempty"`
	// DHT Announce
	DHTAnnounceSuccess int64     `json:"dhtAnnounceSuccess"`
	DHTAnnounceFail    int64     `json:"dhtAnnounceFail"`
	LastAnnounceAt     time.Time `json:"lastAnnounceAt,omitempty"`
	// XOR proximity decisions
	XORAccepted int64 `json:"xorAccepted"`
	XORRejected int64 `json:"xorRejected"`
	// GC
	GCRunCount      int64 `json:"gcRunCount"`
	GCBlocksEvicted int64 `json:"gcBlocksEvicted"`
	GCBytesFreed    int64 `json:"gcBytesFreed"`
	// Reannounce
	ReannounceCount  int64     `json:"reannounceCount"`
	LastReannounceAt time.Time `json:"lastReannounceAt,omitempty"`
}

// ═══════════════════════════════════════════════════════════════════
// Resource types
// ═══════════════════════════════════════════════════════════════════

type ResourceType string

const (
	ResourceTypeVMOverlay     ResourceType = "VMOverlay"
	ResourceTypeModelShard    ResourceType = "ModelShard"
	ResourceTypeLoRAAdapter   ResourceType = "LoRAAdapter"
	ResourceTypeImageTemplate ResourceType = "ImageTemplate"
	ResourceTypeUnknown       ResourceType = "Unknown"
)

// ShardMetadata describes an AI model shard for distributed inference routing.
type ShardMetadata struct {
	ModelName      string `json:"modelName"`
	ModelVersion   string `json:"modelVersion"`
	ShardIndex     int    `json:"shardIndex"`
	TotalShards    int    `json:"totalShards"`
	LayerStart     int    `json:"layerStart"`
	LayerEnd       int    `json:"layerEnd"`
	ParameterCount int64  `json:"parameterCount"`
	QuantBits      int    `json:"quantBits"`
}

// ResourceManifest tracks a stored resource.
type ResourceManifest struct {
	RootCid       string         `json:"rootCid"`
	ResourceType  ResourceType   `json:"resourceType"`
	ResourceID    string         `json:"resourceId"`
	ResourceOwner string         `json:"resourceOwner"`
	Version       int            `json:"version"`
	TotalBytes    int64          `json:"totalBytes"`
	ChunkCIDs     []string       `json:"chunkCids"`
	ShardMeta     *ShardMetadata `json:"shardMeta,omitempty"`
	RegisteredAt  time.Time      `json:"registeredAt"`
	UpdatedAt     time.Time      `json:"updatedAt"`
}

// ═══════════════════════════════════════════════════════════════════
// Config
// ═══════════════════════════════════════════════════════════════════

type Config struct {
	ListenPort      int
	APIPort         int
	AdvertiseIP     string
	StorageBytes    int64
	AuthToken       string
	NodeID          string
	VMID            string
	OrchestratorURL string
	BootstrapPeers  []string
}

func parseConfig() Config {
	return Config{
		ListenPort:      envInt("BLOCKSTORE_LISTEN_PORT", 5001),
		APIPort:         envInt("BLOCKSTORE_API_PORT", 5090),
		AdvertiseIP:     envStr("BLOCKSTORE_ADVERTISE_IP", ""),
		StorageBytes:    0, // resolved from NodeAgent obligation state in main()
		AuthToken:       envStr("BLOCKSTORE_AUTH_TOKEN", ""),
		NodeID:          envStr("BLOCKSTORE_NODE_ID", ""),
		VMID:            envStr("BLOCKSTORE_VM_ID", ""),
		OrchestratorURL: envStr("ORCHESTRATOR_URL", ""),
		BootstrapPeers:  splitPeers(envStr("BLOCKSTORE_BOOTSTRAP_PEERS", "")),
	}
}

// fetchStorageQuotaFromNodeAgent reads storageQuotaBytes from the NodeAgent
// obligation state endpoint. This is the authoritative source — it matches
// the value computed by the orchestrator at obligation creation time (5% of
// node storage) and survives VM redeployments without label re-injection.
func fetchStorageQuotaFromNodeAgent() (int64, error) {
	gw, err := defaultGateway()
	if err != nil {
		return 0, fmt.Errorf("no default gateway: %w", err)
	}
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(fmt.Sprintf("http://%s:5100/api/obligations/blockstore/state", gw))
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("NodeAgent returned HTTP %d", resp.StatusCode)
	}
	var state struct {
		StorageQuotaBytes int64 `json:"storageQuotaBytes"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&state); err != nil {
		return 0, err
	}
	return state.StorageQuotaBytes, nil
}

// fetchAuthTokenFromNodeAgent fetches the HMAC auth token from the NodeAgent
// obligation state endpoint. This is the authoritative source — the token is
// NOT injected as an env var label (by design) to avoid exposing it in VM metadata.
// Retries for up to 2 minutes to handle NodeAgent startup races.
func fetchAuthTokenFromNodeAgent() (string, error) {
	gw, err := defaultGateway()
	if err != nil {
		return "", fmt.Errorf("no default gateway: %w", err)
	}
	url := fmt.Sprintf("http://%s:5100/api/obligations/blockstore/state", gw)

	client := &http.Client{Timeout: 5 * time.Second}
	for i := 0; i < 24; i++ {
		resp, err := client.Get(url)
		if err == nil && resp.StatusCode == 200 {
			var state struct {
				AuthToken string `json:"authToken"`
			}
			if err := json.NewDecoder(resp.Body).Decode(&state); err == nil {
				resp.Body.Close()
				if state.AuthToken != "" {
					return state.AuthToken, nil
				}
			}
			resp.Body.Close()
		}
		time.Sleep(5 * time.Second)
	}
	return "", fmt.Errorf("auth token not available after 2 minutes")
}

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func splitPeers(s string) []string {
	if s == "" {
		return nil
	}
	var out []string
	for _, p := range strings.Split(s, ",") {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

// ═══════════════════════════════════════════════════════════════════
// BlockNode — main node state
// ═══════════════════════════════════════════════════════════════════

type BlockNode struct {
	cfg    Config
	host   host.Host
	dht    *dht.IpfsDHT
	pubsub         *pubsub.PubSub
	newBlocksTopic *pubsub.Topic
	bstore         blockstore.Blockstore
	bsExch         *bitswap.Bitswap

	// Bitswap sessions per libp2p peer ID — created on first use,
	// reused to avoid repeated DHT FindProviders for the same peer.
	// Sessions send WANT messages directly to connected peers.
	peerSessions   map[peer.ID]blockFetcher
	peerSessionsMu sync.Mutex

	// LRU tracking
	accessMu    sync.Mutex
	accessTimes map[string]time.Time
	blockSizes  map[string]int64

	// Manifest registry
	manifestsMu sync.RWMutex
	manifests   map[string]*ResourceManifest

	// Fetch worker pool — GossipSubFetchWorkers goroutines drain this queue.
	// Replaces the per-announcement goroutine + semaphore pattern, which
	// caused goroutine explosion on large bursts and lost blocks when the
	// 5-minute semaphore wait expired before a slot became available.
	fetchQueue chan NewBlockAnnouncement

	// Counters
	mu              sync.RWMutex
	bitswapReceived uint64

	// Diagnostics
	diagLog *DiagLog
	diag    DiagCounters

	// Per-VM catchup state: debounced diff-check triggered on fetch completion.
	// Key: vmId, Value: timer reset channel (send to reschedule, close to cancel).
	catchupMu    sync.Mutex
	catchupTimers map[string]chan struct{}

	// Retry queue for failed GossipSub bitswap fetches (cold DHT / race on first fetch).
	// Drained sequentially every 60s by startRetryLoop. Cap 500 — sized for several
	// full overlay bursts. Per-CID attempt count capped at 3 via retryAttempts.
	retryQueue    chan cid.Cid
	retryAttempts sync.Map // string(cid) → int
}

// ── Debounced per-VM catchup ─────────────────────────────────────────────────
// Triggered on each successful fetch completion. Resets a 10-second timer per VM.
// When the timer fires (burst settled), does ONE diff check against the source peer
// and enqueues any missing CIDs. Self-repeating: new fetches from the enqueued CIDs
// reschedule the timer, looping until the diff is empty.
const catchupSettleDelay = 10 * time.Second

func (n *BlockNode) scheduleCatchup(ctx context.Context, vmId, sourcePeerID string) {
	n.catchupMu.Lock()
	resetCh, exists := n.catchupTimers[vmId]
	if !exists {
		// First fetch for this VM in this burst — launch the catchup goroutine
		resetCh = make(chan struct{}, 1)
		n.catchupTimers[vmId] = resetCh
		go n.runVMCatchup(ctx, vmId, sourcePeerID, resetCh)
	}
	n.catchupMu.Unlock()

	// Signal the goroutine to reset its settle timer
	select {
	case resetCh <- struct{}{}:
	default: // already a reset pending — fine
	}
}

func (n *BlockNode) runVMCatchup(ctx context.Context, vmId, sourcePeerID string, resetCh chan struct{}) {
	defer func() {
		n.catchupMu.Lock()
		delete(n.catchupTimers, vmId)
		n.catchupMu.Unlock()
	}()

	timer := time.NewTimer(catchupSettleDelay)
	defer timer.Stop()

	for {
		select {
		case <-resetCh:
			// Fetch activity still ongoing — reset the settle window
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			timer.Reset(catchupSettleDelay)

		case <-timer.C:
			// Burst settled — do ONE diff check
			queued := n.diffAndEnqueue(ctx, vmId, sourcePeerID)
			if queued == 0 {
				// Fully caught up — exit
				return
			}
			// Missing blocks enqueued; their fetch completions will reschedule
			// this goroutine via scheduleCatchup. Exit now — don't double-run.
			return

		case <-ctx.Done():
			return
		}
	}
}

// diffAndEnqueue compares the source peer's owner index for vmId against our
// local owner index and enqueues any CIDs the peer has that we're missing.
func (n *BlockNode) diffAndEnqueue(ctx context.Context, vmId, sourcePeerID string) int {
	// Resolve peer HTTP API URL from peerstore addrs
	peerID, err := peer.Decode(sourcePeerID)
	if err != nil {
		return 0
	}
	apiURL := n.resolveBlockstoreAPIURL(peerID)
	if apiURL == "" {
		return 0
	}

	// Fetch peer's CID list for this VM
	reqCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet,
		fmt.Sprintf("%s/owners/%s", apiURL, vmId), nil)
	if err != nil {
		return 0
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil || resp.StatusCode != http.StatusOK {
		return 0
	}
	defer resp.Body.Close()

	var ownerCIDs struct {
		CIDs []string `json:"cids"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&ownerCIDs); err != nil {
		return 0
	}

	// Load our local CID set for this VM
	localCIDs := n.loadLocalCIDSet(vmId)

	queued := 0
	for _, cidStr := range ownerCIDs.CIDs {
		if _, have := localCIDs[cidStr]; have {
			continue
		}
		c, err := cid.Decode(cidStr)
		if err != nil {
			continue
		}
		if exists, _ := n.bstore.Has(ctx, c); exists {
			continue
		}
		select {
		case n.fetchQueue <- NewBlockAnnouncement{
			CID:          cidStr,
			Size:         BlockSizeBytes,
			SourcePeerID: sourcePeerID,
			VMId:         vmId,
		}:
			queued++
		default:
			// Queue full — will catch next periodic cycle
		}
	}

	if queued > 0 {
		log.Printf("catchup: VM %s — %d missing blocks enqueued from peer %s",
			vmId, queued, sourcePeerID[:12])
	}
	return queued
}

// resolveBlockstoreAPIURL derives the blockstore HTTP API URL (port 5090) from
// the peer's libp2p multiaddrs stored in the peerstore.
func (n *BlockNode) resolveBlockstoreAPIURL(peerID peer.ID) string {
	for _, ma := range n.host.Peerstore().Addrs(peerID) {
		parts := strings.Split(ma.String(), "/")
		for i, p := range parts {
			if p == "ip4" && i+1 < len(parts) {
				return fmt.Sprintf("http://%s:5090", parts[i+1])
			}
		}
	}
	return ""
}

// loadLocalCIDSet reads the owner CID index for a specific VM.
func (n *BlockNode) loadLocalCIDSet(vmId string) map[string]struct{} {
	result := make(map[string]struct{})
	data, err := os.ReadFile(filepath.Join(StorageDir, OwnersSubdir, vmId+".cids"))
	if err != nil {
		return result
	}
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			result[line] = struct{}{}
		}
	}
	return result
}

func cidShort(s string) string {
	if len(s) <= 12 {
		return s
	}
	return s[:12]
}

// ═══════════════════════════════════════════════════════════════════
// main
// ═══════════════════════════════════════════════════════════════════

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Println("DeCloud Block Store Node starting...")

	cfg := parseConfig()

	// Resolve storage quota: NodeAgent obligation state → env var → default 10 GB.
	if quota, err := fetchStorageQuotaFromNodeAgent(); err == nil && quota > 0 {
		log.Printf("Storage quota from NodeAgent obligation state: %d bytes (%.1f GB)",
			quota, float64(quota)/(1024*1024*1024))
		cfg.StorageBytes = quota
	} else {
		if err != nil {
			log.Printf("NodeAgent storage quota unavailable (%v) — using env/default", err)
		}
		if cfg.StorageBytes <= 0 {
			cfg.StorageBytes = 10 * 1024 * 1024 * 1024 // 10 GB default
			log.Printf("Storage quota defaulting to 10 GB")
		}
	}
	// Resolve auth token from NodeAgent obligation state.
	// Intentionally not injected as an env var label — fetched at runtime like the storage quota.
	if token, err := fetchAuthTokenFromNodeAgent(); err == nil && token != "" {
		cfg.AuthToken = token
	} else if err != nil {
		log.Printf("NodeAgent auth token unavailable (%v) — orchestrator join will fail auth", err)
	}

	log.Printf("Config: listenPort=%d apiPort=%d storageBytes=%d nodeID=%s vmID=%s",
		cfg.ListenPort, cfg.APIPort, cfg.StorageBytes, cfg.NodeID, cfg.VMID)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	node, err := setup(ctx, cfg)
	if err != nil {
		log.Fatalf("Setup failed: %v", err)
	}
	defer node.host.Close()

	node.loadExistingAccessTimes(ctx)
	node.loadExistingManifests(ctx)
	node.connectBootstrapPeers(ctx)
	go node.reannounceExistingBlocks(ctx)
	go node.startRetryLoop(ctx)

	if err := node.startGossipSubSubscription(ctx); err != nil {
		log.Printf("Warning: GossipSub subscription failed: %v", err)
	}
	node.startPresenceTopic(ctx)
	go node.startVmDeletedSubscription(ctx)
	node.startHTTPServer(ctx)
	go node.startGCLoop(ctx)
	for i := 0; i < GossipSubFetchWorkers; i++ {
		go node.runFetchWorker(ctx)
	}

	for _, addr := range node.host.Addrs() {
		log.Printf("Listening: %s/p2p/%s", addr, node.host.ID())
	}
	log.Printf("Block store node ready — peer ID: %s", node.host.ID())

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	log.Println("Shutting down block store node...")
	cancel()
	time.Sleep(500 * time.Millisecond)
}

// ═══════════════════════════════════════════════════════════════════
// Setup
// ═══════════════════════════════════════════════════════════════════

func setup(ctx context.Context, cfg Config) (*BlockNode, error) {
	for _, sub := range []string{"", BlocksSubdir, DagsSubdir, OwnersSubdir, ConfirmedSubdir} {
		if err := os.MkdirAll(filepath.Join(StorageDir, sub), 0755); err != nil {
			return nil, fmt.Errorf("create dir %s: %w", sub, err)
		}
	}

	priv, err := loadOrCreateIdentity(StorageDir)
	if err != nil {
		return nil, fmt.Errorf("identity: %w", err)
	}

	blocksDir := filepath.Join(StorageDir, BlocksSubdir)
	shardFn, err := flatfs.ParseShardFunc("/repo/flatfs/shard/v1/next-to-last/2")
	if err != nil {
		return nil, fmt.Errorf("shard func: %w", err)
	}
	fds, err := flatfs.CreateOrOpen(blocksDir, shardFn, false)
	if err != nil {
		return nil, fmt.Errorf("open flatfs: %w", err)
	}
	// blockstore.NewBlockstore prepends "/blocks/" to all CID keys.
	// FlatFS only supports single-segment keys and rejects path-style keys.
	// Mount flatfs at "/blocks" so the prefix is stripped before reaching flatfs,
	// giving it single-segment keys like "/CIQB23..." it can handle.
	mds := mount.New([]mount.Mount{
		{
			Prefix:    datastore.NewKey("/blocks"),
			Datastore: fds,
		},
	})
	bs := blockstore.NewIdStore(blockstore.NewBlockstore(mds))

	// Wait up to 30s for the WG mesh interface to be assigned the advertise IP.
	if cfg.AdvertiseIP != "" {
		deadline := time.Now().Add(30 * time.Second)
		for time.Now().Before(deadline) {
			if isIPOnAnyInterface(cfg.AdvertiseIP) {
				log.Printf("WG mesh interface ready — advertise IP %s is up", cfg.AdvertiseIP)
				break
			}
			log.Printf("Waiting for WG mesh interface (%s)...", cfg.AdvertiseIP)
			time.Sleep(2 * time.Second)
		}
		if !isIPOnAnyInterface(cfg.AdvertiseIP) {
			log.Printf("Warning: advertise IP %s not found on any interface after 30s", cfg.AdvertiseIP)
		}
	}

	listenAddr := fmt.Sprintf("/ip4/0.0.0.0/tcp/%d", cfg.ListenPort)
	opts := []libp2p.Option{
		libp2p.ListenAddrStrings(listenAddr),
		libp2p.Identity(priv),
	}

	if cfg.AdvertiseIP != "" {
		extAddr := fmt.Sprintf("/ip4/%s/tcp/%d", cfg.AdvertiseIP, cfg.ListenPort)
		extMA, maErr := multiaddr.NewMultiaddr(extAddr)
		if maErr == nil {
			opts = append(opts, libp2p.AddrsFactory(func(_ []multiaddr.Multiaddr) []multiaddr.Multiaddr {
				return []multiaddr.Multiaddr{extMA}
			}))
		}
	}

	h, err := libp2p.New(opts...)
	if err != nil {
		return nil, fmt.Errorf("create libp2p host: %w", err)
	}

	log.Printf("libp2p peer ID: %s", h.ID())

	if err := os.WriteFile(filepath.Join(StorageDir, PeerIDFile),
		[]byte(h.ID().String()), 0644); err != nil {
		log.Printf("Warning: could not write peer-id file: %v", err)
	}

	kadDHT, err := dht.New(ctx, h,
		dht.Mode(dht.ModeClient),
		dht.ProtocolPrefix("/decloud"),
	)
	if err != nil {
		return nil, fmt.Errorf("create DHT: %w", err)
	}
	if err := kadDHT.Bootstrap(ctx); err != nil {
		log.Printf("Warning: DHT bootstrap: %v", err)
	}

	bsNet := bsnetwork.NewFromIpfsHost(h, kadDHT)
	bsExch := bitswap.New(ctx, bsNet, bs)

	ps, err := pubsub.NewGossipSub(ctx, h)
	if err != nil {
		return nil, fmt.Errorf("create pubsub: %w", err)
	}

	return &BlockNode{
		cfg:          cfg,
		host:         h,
		dht:          kadDHT,
		pubsub:       ps,
		bstore:       bs,
		bsExch:       bsExch,
		accessTimes:  make(map[string]time.Time),
		blockSizes:   make(map[string]int64),
		manifests:    make(map[string]*ResourceManifest),
		peerSessions: make(map[peer.ID]blockFetcher),
		diagLog:       newDiagLog(diagLogCap),
		fetchQueue:    make(chan NewBlockAnnouncement, FetchQueueSize),
		catchupTimers: make(map[string]chan struct{}),
		retryQueue:    make(chan cid.Cid, 500),
	}, nil
}

// ═══════════════════════════════════════════════════════════════════
// Identity
// ═══════════════════════════════════════════════════════════════════

// loadOrCreateIdentity loads the Ed25519 peer identity.
// Priority: NodeAgent obligation state → disk cache → generate new.
func loadOrCreateIdentity(dir string) (crypto.PrivKey, error) {
	keyPath := filepath.Join(dir, IdentityKeyFile)

	// 1. NodeAgent obligation state — authoritative, survives redeployments
	if priv, err := loadIdentityFromNodeAgent("blockstore"); err == nil {
		log.Printf("Loaded BlockStore identity from NodeAgent obligation state")
		cacheIdentityToDisk(priv, keyPath)
		return priv, nil
	} else {
		log.Printf("NodeAgent obligation state unavailable (%v) — falling back to disk", err)
	}

	// 2. Disk cache
	if data, err := os.ReadFile(keyPath); err == nil {
		if priv, err := crypto.UnmarshalPrivateKey(data); err == nil {
			log.Printf("Loaded cached identity from %s", keyPath)
			return priv, nil
		}
	}

	// 3. Generate new (first boot)
	priv, _, err := crypto.GenerateEd25519Key(nil)
	if err != nil {
		return nil, fmt.Errorf("generate identity key: %w", err)
	}
	log.Printf("Generated new Ed25519 identity (obligation state not yet available)")
	cacheIdentityToDisk(priv, keyPath)
	return priv, nil
}

func loadIdentityFromNodeAgent(role string) (crypto.PrivKey, error) {
	gw, err := defaultGateway()
	if err != nil {
		return nil, fmt.Errorf("no default gateway: %w", err)
	}
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(fmt.Sprintf("http://%s:5100/api/obligations/%s/state", gw, role))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	var state struct {
		Ed25519PrivateKeyBase64 string `json:"ed25519PrivateKeyBase64"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&state); err != nil {
		return nil, err
	}
	if state.Ed25519PrivateKeyBase64 == "" {
		return nil, fmt.Errorf("empty ed25519PrivateKeyBase64")
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

func defaultGateway() (string, error) {
	f, err := os.Open("/proc/net/route")
	if err != nil {
		return "", err
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Scan()
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

func cacheIdentityToDisk(priv crypto.PrivKey, keyPath string) {
	data, err := crypto.MarshalPrivateKey(priv)
	if err != nil {
		return
	}
	_ = os.MkdirAll(filepath.Dir(keyPath), 0o700)
	_ = os.WriteFile(keyPath, data, 0600)
}

// isIPOnAnyInterface checks if ip is assigned to any local network interface.
func isIPOnAnyInterface(ip string) bool {
	ifaces, err := net.Interfaces()
	if err != nil {
		return false
	}
	for _, iface := range ifaces {
		addrs, _ := iface.Addrs()
		for _, addr := range addrs {
			var ifIP net.IP
			switch v := addr.(type) {
			case *net.IPNet:
				ifIP = v.IP
			case *net.IPAddr:
				ifIP = v.IP
			}
			if ifIP != nil && ifIP.String() == ip {
				return true
			}
		}
	}
	return false
}

// ═══════════════════════════════════════════════════════════════════
// Bootstrap peers
// ═══════════════════════════════════════════════════════════════════

func (n *BlockNode) connectBootstrapPeers(ctx context.Context) {
	if len(n.cfg.BootstrapPeers) == 0 {
		log.Println("No bootstrap peers configured — running as genesis node")
		return
	}
	log.Printf("Connecting to %d bootstrap peer(s)...", len(n.cfg.BootstrapPeers))
	for _, addrStr := range n.cfg.BootstrapPeers {
		maddr, err := multiaddr.NewMultiaddr(addrStr)
		if err != nil {
			log.Printf("Invalid bootstrap peer addr %q: %v", addrStr, err)
			continue
		}
		pi, err := peer.AddrInfoFromP2pAddr(maddr)
		if err != nil {
			log.Printf("Parse bootstrap peer info %q: %v", addrStr, err)
			continue
		}
		connCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
		if err := n.host.Connect(connCtx, *pi); err != nil {
			log.Printf("Connect to bootstrap peer %s: %v", pi.ID, err)
		} else {
			log.Printf("Connected to bootstrap peer %s", pi.ID)
		}
		cancel()
	}
}

// ═══════════════════════════════════════════════════════════════════
// GossipSub — new-blocks subscription
// ═══════════════════════════════════════════════════════════════════

type NewBlockAnnouncement struct {
	CID             string `json:"cid"`
	Size            int64  `json:"size"`
	SourceNodeID    string `json:"sourceNodeId"`
	SourcePeerID    string `json:"sourcePeerId"`
	VMId            string `json:"vmId,omitempty"`            // owner VM — set by publisher, used by receiver to build owner index
	ManifestVersion int    `json:"manifestVersion,omitempty"` // lazysync version at publish time — receivers
	                                                           // drop announcements where their local version
	                                                           // already equals or exceeds this value, avoiding
	                                                           // redundant pulls of blocks from stale cycles
}

func (n *BlockNode) startGossipSubSubscription(ctx context.Context) error {
	topic, err := n.pubsub.Join(GossipSubTopic)
	if err == nil {
		n.newBlocksTopic = topic
	}
	if err != nil {
		return fmt.Errorf("join gossipsub topic: %w", err)
	}
	sub, err := topic.Subscribe()
	if err != nil {
		return fmt.Errorf("subscribe to topic: %w", err)
	}
	go func() {
		log.Printf("Subscribed to GossipSub topic: %s", GossipSubTopic)
		for {
			msg, err := sub.Next(ctx)
			if err != nil {
				if ctx.Err() != nil {
					return
				}
				log.Printf("GossipSub receive error: %v", err)
				continue
			}
			if msg.ReceivedFrom == n.host.ID() {
				continue
			}
			var ann NewBlockAnnouncement
			if err := json.Unmarshal(msg.Data, &ann); err != nil {
				continue
			}

			// Track receive in diagnostics
			n.mu.Lock()
			n.diag.GossipSubReceived++
			n.diag.LastGossipSubRxAt = time.Now()
			n.mu.Unlock()
			n.diagLog.Add("gossipsub_receive", map[string]interface{}{
				"cid":    cidShort(ann.CID),
				"size":   ann.Size,
				"source": cidShort(ann.SourceNodeID),
			})

			// Enqueue for fetch worker pool. Non-blocking: if the queue is
			// full (FetchQueueSize exceeded) the announcement is dropped.
			// This is extremely unlikely — FetchQueueSize covers several full
			// overlay cycles — and preferable to unbounded goroutine growth.
			select {
			case n.fetchQueue <- ann:
			default:
				n.diagLog.Add("fetch_queue_full", map[string]interface{}{
					"cid": cidShort(ann.CID),
				})
			}
		}
	}()
	return nil
}

// runFetchWorker is one of GossipSubFetchWorkers goroutines that drain
// fetchQueue. Workers run for the lifetime of the node context.
func (n *BlockNode) runFetchWorker(ctx context.Context) {
	for {
		select {
		case ann := <-n.fetchQueue:
			n.handleNewBlockAnnouncement(ctx, ann)
		case <-ctx.Done():
			return
		}
	}
}

func (n *BlockNode) handleNewBlockAnnouncement(ctx context.Context, ann NewBlockAnnouncement) {
	c, err := cid.Decode(ann.CID)
	if err != nil {
		return
	}
	has, _ := n.bstore.Has(ctx, c)
	if has {
		return
	}

	// Version filtering removed: bstore.Has() below already skips blocks this
	// node holds, making the version file check redundant. The version filter
	// caused a migration regression — the ex-host's version file reflected the
	// pre-migration manifest version (e.g. 5), causing it to drop all
	// announcements from the new publisher which restarted at version 1.
	usedBytes, _ := n.usedBytes(ctx)
	usagePct := float64(usedBytes) / float64(n.cfg.StorageBytes) * 100
	if usagePct >= GCHardLimit {
		n.diagLog.Add("xor_reject", map[string]interface{}{
			"cid":    cidShort(ann.CID),
			"reason": "capacity_full",
			"usage":  fmt.Sprintf("%.1f%%", usagePct),
		})
		return
	}
	if !n.isWithinXORThreshold(c, usagePct) {
		n.mu.Lock()
		n.diag.XORRejected++
		n.mu.Unlock()
		n.diagLog.Add("xor_reject", map[string]interface{}{
			"cid":    cidShort(ann.CID),
			"reason": "xor_too_far",
			"usage":  fmt.Sprintf("%.1f%%", usagePct),
		})
		return
	}
	n.mu.Lock()
	n.diag.XORAccepted++
	n.mu.Unlock()

	// Use a peer-specific session if the source peer ID is known and connected.
	// ann.SourcePeerID is the libp2p peer ID of the publishing blockstore —
	// set at publish time from n.host.ID(). This is reliable because it comes
	// from the announcement payload, not from msg.ReceivedFrom (which is the
	// relay hop). Targeting the source directly avoids the DHT FindProviders
	// walk and resolves in milliseconds via WANT-BLOCK to the connected peer.
	// Falls back to NewSession (DHT walk) if the peer ID is missing or the
	// peer is not yet connected.
	var fetcher blockFetcher
	if ann.SourcePeerID != "" {
		sourcePeer, parseErr := peer.Decode(ann.SourcePeerID)
		if parseErr == nil && n.host.Network().Connectedness(sourcePeer) == network.Connected {
			fetcher = n.sessionForPeer(ctx, sourcePeer)
		}
	}
	if fetcher == nil {
		fetcher = n.bsExch.NewSession(ctx)
	}

	fetchStart := time.Now()
	pullCtx, cancel := context.WithTimeout(ctx, BitswapTimeout)
	blk, err := fetcher.GetBlock(pullCtx, c)
	fetchMs := time.Since(fetchStart).Milliseconds()
	cancel()

	if err != nil {
		n.diagLog.Add("bitswap_fetch_fail", map[string]interface{}{
			"cid":         cidShort(ann.CID),
			"error":       err.Error(),
			"duration_ms": fetchMs,
		})
		select {
		case n.retryQueue <- c:
		default:
			n.diagLog.Add("retry_queue_full", map[string]interface{}{"cid": cidShort(ann.CID)})
		}
		return
	}
	n.diagLog.Add("bitswap_fetch", map[string]interface{}{
		"cid":         cidShort(ann.CID),
		"bytes":       len(blk.RawData()),
		"duration_ms": fetchMs,
		"source":      cidShort(ann.SourceNodeID),
	})

	if err := n.bstore.Put(ctx, blk); err != nil {
		return
	}
	n.touchBlock(c.String(), int64(len(blk.RawData())))
	n.mu.Lock()
	n.bitswapReceived++
	n.mu.Unlock()

	// Announce ourselves as a provider to the DHT.
	// dhtProvide retries with backoff to handle the DHT VM startup race.
	announceErr := n.dhtProvide(ctx, c)
	n.mu.Lock()
	if announceErr != nil {
		n.diag.DHTAnnounceFail++
		n.diagLog.Add("dht_announce_fail", map[string]interface{}{
			"cid":   cidShort(c.String()),
			"error": announceErr.Error(),
			"role":  "gossipsub_pull",
		})
	} else {
		n.diag.DHTAnnounceSuccess++
		n.diag.LastAnnounceAt = time.Now()
		n.diagLog.Add("dht_announce", map[string]interface{}{
			"cid":  cidShort(c.String()),
			"role": "gossipsub_pull",
		})
	}
	n.mu.Unlock()

	log.Printf("Pulled block %s (%d bytes) via GossipSub + bitswap", ann.CID[:12], len(blk.RawData()))

	// Schedule a debounced catchup diff-check against the source peer.
	// Each successful fetch resets the timer — the diff runs once the burst
	// settles, avoiding redundant GET /owners calls during an active seeding.
	if ann.VMId != "" && ann.SourcePeerID != "" {
		n.scheduleCatchup(ctx, ann.VMId, ann.SourcePeerID)
	}

	// Write to owner index so this node's /manifests endpoint reflects
	// which tenant VMs it holds blocks for. This is the only mechanism
	// by which remote (receiver) blockstores build their manifest view —
	// no fan-out from the publisher; each node tracks ownership locally.
	if ann.VMId != "" {
		go func(ownerID, cidStr string) {
			ownerFile := filepath.Join(StorageDir, OwnersSubdir, ownerID+".cids")
			f, err := os.OpenFile(ownerFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
			if err != nil {
				log.Printf("owner index: bitswap pull: failed to open %s: %v", ownerFile, err)
				return
			}
			defer f.Close()
			if _, err := fmt.Fprintln(f, cidStr); err != nil {
				log.Printf("owner index: bitswap pull: failed to write CID %s: %v", cidStr[:12], err)
			}
		}(ann.VMId, ann.CID)
	}
}

// ═══════════════════════════════════════════════════════════════════
// GossipSub — publisher
// ═══════════════════════════════════════════════════════════════════

func (n *BlockNode) publishNewBlock(c cid.Cid, size int64, ownerVMId string, manifestVersion int) {
	topic := n.newBlocksTopic
	if topic == nil {
		return
	}
	ann := NewBlockAnnouncement{CID: c.String(), Size: size, SourceNodeID: n.cfg.NodeID, SourcePeerID: n.host.ID().String(), VMId: ownerVMId, ManifestVersion: manifestVersion}
	data, err := json.Marshal(ann)
	if err != nil {
		return
	}
	pubCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	pubErr := topic.Publish(pubCtx, data)
	n.mu.Lock()
	n.diag.GossipSubPublished++
	n.diag.LastGossipSubTxAt = time.Now()
	n.mu.Unlock()
	if pubErr != nil {
		n.diagLog.Add("gossipsub_publish_fail", map[string]interface{}{
			"cid":   cidShort(c.String()),
			"error": pubErr.Error(),
		})
	} else {
		n.diagLog.Add("gossipsub_publish", map[string]interface{}{
			"cid":  cidShort(c.String()),
			"size": size,
		})
	}
}

// ═══════════════════════════════════════════════════════════════════
// VM-deleted GossipSub subscription
// ═══════════════════════════════════════════════════════════════════

type VmDeletedEvent struct {
	VmId      string `json:"vmId"`
	NodeId    string `json:"nodeId"`
	Timestamp string `json:"timestamp"`
	Signature string `json:"signature"`
}

func (n *BlockNode) startVmDeletedSubscription(ctx context.Context) {
	topic, err := n.pubsub.Join(GossipSubVmDelTopic)
	if err != nil {
		log.Printf("vm-deleted: failed to join topic: %v", err)
		return
	}
	sub, err := topic.Subscribe()
	if err != nil {
		log.Printf("vm-deleted: failed to subscribe: %v", err)
		return
	}
	log.Printf("Subscribed to GossipSub topic: %s", GossipSubVmDelTopic)

	for {
		msg, err := sub.Next(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			log.Printf("vm-deleted: receive error: %v", err)
			continue
		}
		if msg.ReceivedFrom == n.host.ID() {
			continue
		}
		var evt VmDeletedEvent
		if err := json.Unmarshal(msg.Data, &evt); err != nil {
			log.Printf("vm-deleted: invalid payload: %v", err)
			continue
		}
		if err := n.verifyVmDeletedEvent(evt); err != nil {
			log.Printf("vm-deleted: rejected event for VM %s: %v", evt.VmId, err)
			continue
		}
		go n.deleteOwnerBlocks(ctx, evt.VmId)
	}
}

func (n *BlockNode) verifyVmDeletedEvent(evt VmDeletedEvent) error {
	if evt.VmId == "" || evt.NodeId == "" || evt.Timestamp == "" || evt.Signature == "" {
		return fmt.Errorf("missing required fields")
	}
	ts, err := time.Parse(time.RFC3339, evt.Timestamp)
	if err != nil {
		return fmt.Errorf("invalid timestamp: %w", err)
	}
	diff := time.Since(ts)
	if diff < 0 {
		diff = -diff
	}
	if diff > 5*time.Minute {
		return fmt.Errorf("timestamp too old or too far in future: %v", diff)
	}
	if n.cfg.AuthToken == "" {
		return fmt.Errorf("no auth token configured — cannot verify event")
	}
	message := fmt.Sprintf("%s:%s:%s", evt.VmId, evt.NodeId, evt.Timestamp)
	mac := hmac.New(sha256.New, []byte(n.cfg.AuthToken))
	mac.Write([]byte(message))
	expected := base64.StdEncoding.EncodeToString(mac.Sum(nil))
	if !hmac.Equal([]byte(evt.Signature), []byte(expected)) {
		return fmt.Errorf("invalid signature")
	}
	return nil
}

func (n *BlockNode) deleteOwnerBlocks(ctx context.Context, vmId string) {
	ownerFile := filepath.Join(StorageDir, OwnersSubdir, vmId+".cids")
	data, err := os.ReadFile(ownerFile)
	if err != nil {
		if os.IsNotExist(err) {
			return
		}
		log.Printf("deleteOwnerBlocks: read owner file failed for VM %s: %v", vmId, err)
		return
	}

	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	deleted := 0
	skipped := 0

	for _, cidStr := range lines {
		cidStr = strings.TrimSpace(cidStr)
		if cidStr == "" {
			continue
		}
		c, err := cid.Decode(cidStr)
		if err != nil {
			continue
		}
		others, _ := n.cidHasOtherOwners(vmId, cidStr)
		if others {
			skipped++
			continue
		}
		if err := n.bstore.DeleteBlock(ctx, c); err != nil {
			continue
		}
		n.accessMu.Lock()
		delete(n.accessTimes, cidStr)
		delete(n.blockSizes, cidStr)
		n.accessMu.Unlock()
		go func(c cid.Cid) {
			wCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			_ = n.dht.Provide(wCtx, c, false)
		}(c)
		deleted++
	}

	if err := os.Remove(ownerFile); err != nil && !os.IsNotExist(err) {
		log.Printf("deleteOwnerBlocks: failed to remove owner file for VM %s: %v", vmId, err)
	}
	log.Printf("deleteOwnerBlocks: VM %s — deleted %d blocks, skipped %d (shared owners)", vmId, deleted, skipped)
}

// ═══════════════════════════════════════════════════════════════════
// XOR proximity
// ═══════════════════════════════════════════════════════════════════

// presenceHeartbeat is published periodically on the presence topic.
// Carries the HTTP API URL so peers can call /owners for block catchup.
type presenceHeartbeat struct {
	PeerID string `json:"peerId"`
	APIURL string `json:"apiUrl"`
}

// startPresenceTopic joins the blockstore-only presence topic.
// DHT VMs never join this topic — ListPeers(presence) = exact blockstore count.
// Also publishes heartbeats and triggers owner-based catchup for new peers.
func (n *BlockNode) startPresenceTopic(ctx context.Context) {
	topic, err := n.pubsub.Join(GossipSubPresenceTopic)
	if err != nil {
		log.Printf("Warning: could not join presence topic: %v", err)
		return
	}
	sub, err := topic.Subscribe()
	if err != nil {
		log.Printf("Warning: could not subscribe to presence topic: %v", err)
		return
	}
	log.Printf("Subscribed to GossipSub topic: %s", GossipSubPresenceTopic)

	// Publish heartbeat so peers know our API URL for catchup.
	apiURL := fmt.Sprintf("http://%s:%d", n.cfg.AdvertiseIP, n.cfg.APIPort)
	hb, _ := json.Marshal(presenceHeartbeat{
		PeerID: n.host.ID().String(),
		APIURL: apiURL,
	})
	go func() {
		time.Sleep(2 * time.Second) // wait for mesh to form
		_ = topic.Publish(ctx, hb)
		ticker := time.NewTicker(60 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				_ = topic.Publish(ctx, hb)
			}
		}
	}()

	// Watch for peers. Re-trigger catchup whenever a peer's API URL changes —
	// this handles redeploys that preserve the peer ID (same identity.key on
	// persistent disk) but represent a new VM instance with a fresh owner index.
	// Repeated heartbeats with the same peerID+URL are de-duplicated by the map.
	go func() {
		seenPeerURLs := make(map[string]string) // peerID → last seen APIURL
		for {
			msg, err := sub.Next(ctx)
			if err != nil {
				return
			}
			if msg.ReceivedFrom == n.host.ID() {
				continue
			}
			var hb presenceHeartbeat
			if err := json.Unmarshal(msg.Data, &hb); err != nil || hb.APIURL == "" {
				continue
			}
			if seenPeerURLs[hb.PeerID] == hb.APIURL {
				continue
			}
			seenPeerURLs[hb.PeerID] = hb.APIURL
			libp2pPeerID, parseErr := peer.Decode(hb.PeerID)
			if parseErr != nil {
				log.Printf("Presence: could not parse peer ID %s: %v", hb.PeerID, parseErr)
				continue
			}
			log.Printf("Presence: peer %s at %s — starting owner catchup",
				cidShort(hb.PeerID), hb.APIURL)
			go n.performCatchupFromPeer(ctx, hb.APIURL, libp2pPeerID)
		}
	}()
}

// performCatchupFromPeer fetches owner CID lists from a peer via HTTP
// (metadata only) and pulls missing blocks via bitswap session.
// The session sends WANT messages directly to peerID — no DHT walk.
// Called once per newly discovered peer, never repeated for the same peer.
func (n *BlockNode) performCatchupFromPeer(ctx context.Context, peerAPIURL string, peerID peer.ID) {
	client := &http.Client{Timeout: 30 * time.Second}
	// Persistent session for this peer — WANT messages go directly to
	// peerID without DHT FindProviders, even for historical blocks.
	session := n.sessionForPeer(ctx, peerID)

	// Step 1: discover which VMs the peer has blocks for
	resp, err := client.Get(peerAPIURL + "/owners")
	if err != nil {
		log.Printf("catchup: failed to fetch owner list from %s: %v", peerAPIURL, err)
		return
	}
	var ownerList struct {
		VmIds []string `json:"vmIds"`
	}
	json.NewDecoder(resp.Body).Decode(&ownerList)
	resp.Body.Close()

	if len(ownerList.VmIds) == 0 {
		return
	}
	log.Printf("catchup: peer has %d VM(s) — checking for missing blocks", len(ownerList.VmIds))

	pulled, skipped := 0, 0

	// Step 2: for each VM, collect missing CIDs and batch-fetch via session
	for _, vmId := range ownerList.VmIds {
		if ctx.Err() != nil {
			return
		}
		resp, err := client.Get(peerAPIURL + "/owners/" + vmId)
		if err != nil {
			continue
		}
		var ownerCids struct {
			Cids []string `json:"cids"`
		}
		json.NewDecoder(resp.Body).Decode(&ownerCids)
		resp.Body.Close()

		// Filter to blocks we're responsible for and don't yet have
		var missingCIDs []cid.Cid
		for _, cidStr := range ownerCids.Cids {
			if ctx.Err() != nil {
				return
			}
			c, err := cid.Decode(cidStr)
			if err != nil {
				continue
			}
			if has, _ := n.bstore.Has(ctx, c); has {
				skipped++
				continue
			}
			usedBytes, _ := n.usedBytes(ctx)
			usagePct := float64(usedBytes) / float64(n.cfg.StorageBytes) * 100
			if usagePct >= GCHardLimit {
				log.Printf("catchup: storage full — stopping")
				return
			}
			if !n.isWithinXORThreshold(c, usagePct) {
				skipped++
				continue
			}
			missingCIDs = append(missingCIDs, c)
		}

		if len(missingCIDs) == 0 {
			continue
		}

		// Batch-fetch all missing blocks via the peer session.
		// GetBlocks sends a single WANT list to the peer — far more
		// efficient than one GetBlock call per CID.
		fetchTimeout := time.Duration(len(missingCIDs)+1) * 10 * time.Second
		fetchCtx, cancelFetch := context.WithTimeout(ctx, fetchTimeout)
		blockCh, err := session.GetBlocks(fetchCtx, missingCIDs)
		if err != nil {
			cancelFetch()
			log.Printf("catchup: GetBlocks failed for VM %s: %v", vmId, err)
			continue
		}
		for blk := range blockCh {
			if err := n.bstore.Put(ctx, blk); err != nil {
				continue
			}
			n.touchBlock(blk.Cid().String(), int64(len(blk.RawData())))
			n.mu.Lock()
			n.bitswapReceived++
			n.mu.Unlock()
			go func(c cid.Cid) {
				aCtx, aCancel := context.WithTimeout(context.Background(), 15*time.Second)
				defer aCancel()
				_ = n.dht.Provide(aCtx, c, true)
			}(blk.Cid())
			n.diagLog.Add("catchup_fetch", map[string]interface{}{
				"cid": cidShort(blk.Cid().String()), "vmId": vmId,
			})
			pulled++
		}
		cancelFetch()
	}
	log.Printf("catchup: complete — pulled %d, skipped %d", pulled, skipped)
}

// sessionForPeer returns a cached bitswap session for the given peer,
// creating one if it doesn't exist. Sessions remember which peers
// have which blocks and send WANT messages directly to connected
// peers — no DHT FindProviders walk needed.
func (n *BlockNode) sessionForPeer(ctx context.Context, p peer.ID) blockFetcher {
	n.peerSessionsMu.Lock()
	defer n.peerSessionsMu.Unlock()
	if s, ok := n.peerSessions[p]; ok {
		return s
	}
	s := n.bsExch.NewSession(ctx)
	n.peerSessions[p] = s
	return s
}

// dhtProvide announces CID c to the DHT with retry backoff.
// Retries handle the race between blockstore and DHT VM startup after
// a NodeAgent restart — the DHT routing table may be empty for the
// first few minutes if the DHT VM boots after the blockstore.
// Max wall time: 3 attempts × 15s timeout + 10s + 20s backoff = ~75s.
func (n *BlockNode) dhtProvide(ctx context.Context, c cid.Cid) error {
	const maxAttempts = 3
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			select {
			case <-time.After(time.Duration(attempt) * 10 * time.Second):
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		announceCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
		lastErr = n.dht.Provide(announceCtx, c, true)
		cancel()
		if lastErr == nil {
			return nil
		}
	}
	return lastErr
}

// blockstorePeerCount returns the number of other blockstore nodes
// currently in the GossipSub presence mesh. Updates live as nodes
// join and leave — no polling, no orchestrator intervention needed.
func (n *BlockNode) blockstorePeerCount() int {
	return len(n.pubsub.ListPeers(GossipSubPresenceTopic))
}

func (n *BlockNode) isWithinXORThreshold(c cid.Cid, usagePct float64) bool {
	peerHash := sha256.Sum256([]byte(n.host.ID()))
	cidHash := sha256.Sum256(c.Bytes())
	xorBytes := make([]byte, 32)
	for i := range xorBytes {
		xorBytes[i] = peerHash[i] ^ cidHash[i]
	}
	xorInt := new(big.Int).SetBytes(xorBytes)
	maxInt := new(big.Int).Lsh(big.NewInt(1), 256)
	xorFraction, _ := new(big.Float).Quo(
		new(big.Float).SetInt(xorInt),
		new(big.Float).SetInt(maxInt),
	).Float64()

	// Dynamic threshold: each node covers 1/N of the keyspace where
	// N = self + live mesh peers. Self-healing — threshold widens
	// automatically when peers leave, narrows when they join.
	// Capacity scaling: emptier nodes accept a larger share (up to 3x base).
	networkSize := n.blockstorePeerCount() + 1 // +1 for self
	baseThreshold := 1.0 / float64(networkSize)
	freePct := 1.0 - (usagePct / 100.0)
	threshold := baseThreshold * (1.0 + 2.0*freePct)

	// Solo or near-solo: accept everything — no other node to hold the block.
	if threshold >= 1.0 {
		return true
	}
	return xorFraction <= threshold
}

// ═══════════════════════════════════════════════════════════════════
// LRU eviction
// ═══════════════════════════════════════════════════════════════════

func (n *BlockNode) touchBlock(cidStr string, size int64) {
	n.accessMu.Lock()
	defer n.accessMu.Unlock()
	n.accessTimes[cidStr] = time.Now()
	if size > 0 {
		n.blockSizes[cidStr] = size
	}
}

func (n *BlockNode) startGCLoop(ctx context.Context) {
	ticker := time.NewTicker(5 * time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			usedBytes, _ := n.usedBytes(ctx)
			if float64(usedBytes)/float64(n.cfg.StorageBytes)*100 >= GCTriggerPercent {
				freed, err := n.runGC(ctx)
				if err != nil {
					log.Printf("GC error: %v", err)
				} else if freed > 0 {
					log.Printf("GC freed %d bytes", freed)
				}
			}
		}
	}
}

func (n *BlockNode) runGC(ctx context.Context) (int64, error) {
	n.accessMu.Lock()
	type entry struct {
		cid        string
		lastAccess time.Time
		size       int64
	}
	var entries []entry
	for c, t := range n.accessTimes {
		entries = append(entries, entry{c, t, n.blockSizes[c]})
	}
	usedBefore, _ := n.usedBytes(ctx)
	usagePctBefore := float64(usedBefore) / float64(n.cfg.StorageBytes) * 100
	n.accessMu.Unlock()
 
	var freed int64
	var evicted int64
 
	// ── Priority 1: evict confirmed-remote blocks ─────────────────────────────
	// Blocks in confirmed/*.cids have ≥N remote providers — they are safe to
	// remove locally. Evicting them first frees space for blocks still scattering,
	// turning the local blockstore into a true write-through cache.
	confirmedSet := n.loadConfirmedCIDs()
	if len(confirmedSet) > 0 {
		for _, e := range entries {
			if !confirmedSet[e.cid] {
				continue
			}
			// Only evict if still above trigger threshold
			usedBytes, _ := n.usedBytes(ctx)
			if float64(usedBytes)/float64(n.cfg.StorageBytes)*100 < GCTriggerPercent {
				break
			}
			c, err := cid.Decode(e.cid)
			if err != nil {
				continue
			}
			if err := n.bstore.DeleteBlock(ctx, c); err != nil {
				continue
			}
			// Withdraw DHT provider record so peers don't route requests here
			go func(c cid.Cid) {
				wCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				_ = n.dht.Provide(wCtx, c, false)
			}(c)
			freed += e.size
			evicted++
			n.accessMu.Lock()
			delete(n.accessTimes, e.cid)
			delete(n.blockSizes, e.cid)
			n.accessMu.Unlock()
		}
		if evicted > 0 {
			n.diagLog.Add("gc_confirmed_evict", map[string]interface{}{
				"blocks_evicted": evicted,
				"bytes_freed":    freed,
			})
		}
	}
 
	// ── Priority 2: LRU eviction (existing behaviour, fallback) ──────────────
	// Re-sort remaining entries by last access time ascending (LRU first).
	n.accessMu.Lock()
	entries = entries[:0]
	for c, t := range n.accessTimes {
		entries = append(entries, entry{c, t, n.blockSizes[c]})
	}
	n.accessMu.Unlock()
 
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].lastAccess.Before(entries[j].lastAccess)
	})
 
	var lruEvicted int64
	var lruFreed int64
	for _, e := range entries {
		usedBytes, _ := n.usedBytes(ctx)
		if float64(usedBytes)/float64(n.cfg.StorageBytes)*100 < GCTriggerPercent {
			break
		}
		c, err := cid.Decode(e.cid)
		if err != nil {
			continue
		}
		if err := n.bstore.DeleteBlock(ctx, c); err != nil {
			continue
		}
		lruFreed += e.size
		lruEvicted++
		n.accessMu.Lock()
		delete(n.accessTimes, e.cid)
		delete(n.blockSizes, e.cid)
		n.accessMu.Unlock()
	}
 
	totalEvicted := evicted + lruEvicted
	freed += lruFreed
 
	if totalEvicted > 0 {
		n.mu.Lock()
		n.diag.GCRunCount++
		n.diag.GCBlocksEvicted += totalEvicted
		n.diag.GCBytesFreed += freed
		n.mu.Unlock()
		n.diagLog.Add("gc_run", map[string]interface{}{
			"blocks_evicted":         totalEvicted,
			"blocks_evicted_confirmed": evicted,
			"blocks_evicted_lru":     lruEvicted,
			"bytes_freed":            freed,
			"usage_before":           fmt.Sprintf("%.1f%%", usagePctBefore),
		})
	}
	return freed, nil
}

// loadConfirmedCIDs reads all confirmed/*.cids files and returns a set of CID
// strings that have been confirmed as remotely replicated (≥N providers).
func (n *BlockNode) loadConfirmedCIDs() map[string]bool {
	confirmedDir := filepath.Join(StorageDir, ConfirmedSubdir)
	entries, err := os.ReadDir(confirmedDir)
	if err != nil {
		return nil // confirmed/ dir doesn't exist yet — no priority eviction
	}
	set := make(map[string]bool)
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".cids") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(confirmedDir, e.Name()))
		if err != nil {
			continue
		}
		for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
			if line = strings.TrimSpace(line); line != "" {
				set[line] = true
			}
		}
	}
	return set
}

// ═══════════════════════════════════════════════════════════════════
// Storage stats
// ═══════════════════════════════════════════════════════════════════

func (n *BlockNode) usedBytes(ctx context.Context) (int64, error) {
	n.accessMu.Lock()
	defer n.accessMu.Unlock()
	var total int64
	for _, size := range n.blockSizes {
		total += size
	}
	return total, nil
}

func (n *BlockNode) blockCount(ctx context.Context) (int, error) {
	ch, err := n.bstore.AllKeysChan(ctx)
	if err != nil {
		return 0, err
	}
	count := 0
	for range ch {
		count++
	}
	return count, nil
}

// ═══════════════════════════════════════════════════════════════════
// LRU bootstrap from existing blocks
// ═══════════════════════════════════════════════════════════════════

func (n *BlockNode) loadExistingAccessTimes(ctx context.Context) {
	ch, err := n.bstore.AllKeysChan(ctx)
	if err != nil {
		return
	}
	n.accessMu.Lock()
	defer n.accessMu.Unlock()
	for c := range ch {
		cidStr := c.String()
		if _, exists := n.accessTimes[cidStr]; !exists {
			n.accessTimes[cidStr] = time.Now()
			buf := new(bytes.Buffer)
			_ = binary.Write(buf, binary.BigEndian, int64(len(c.Bytes())))
			n.blockSizes[cidStr] = 1024
		}
	}
}

// ═══════════════════════════════════════════════════════════════════
// Reannounce existing blocks on startup
// ═══════════════════════════════════════════════════════════════════

// startRetryLoop drains retryQueue every 60s (after a 30s startup delay for
// DHT routing table warmup). Sequential processing is intentional: the first
// retry performs the DHT FindProviders walk; subsequent retries in the same
// batch hit the warm cache and resolve in milliseconds. Max 3 attempts per CID;
// gives up with retry_give_up after that.
func (n *BlockNode) startRetryLoop(ctx context.Context) {
	const (
		startupDelay = 30 * time.Second
		tickInterval = 60 * time.Second
		fetchTimeout = 60 * time.Second
		maxAttempts  = 3
	)
	select {
	case <-time.After(startupDelay):
	case <-ctx.Done():
		return
	}
	ticker := time.NewTicker(tickInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			pending := len(n.retryQueue)
			for i := 0; i < pending; i++ {
				var c cid.Cid
				var got bool
				select {
				case c, got = <-n.retryQueue:
				default:
				}
				if !got {
					break
				}
				key := c.String()
				raw, _ := n.retryAttempts.LoadOrStore(key, 0)
				attempts := raw.(int)
				if attempts >= maxAttempts {
					n.retryAttempts.Delete(key)
					n.diagLog.Add("retry_give_up", map[string]interface{}{"cid": cidShort(key)})
					log.Printf("retry: giving up on %s after %d attempts", cidShort(key), maxAttempts)
					continue
				}
				n.retryAttempts.Store(key, attempts+1)
				n.diagLog.Add("retry_fetch", map[string]interface{}{"cid": cidShort(key), "attempt": attempts + 1})

				fetchCtx, cancel := context.WithTimeout(ctx, fetchTimeout)
				session := n.bsExch.NewSession(fetchCtx)
				blk, err := session.GetBlock(fetchCtx, c)
				cancel()

				if err != nil {
					n.diagLog.Add("retry_fetch_fail", map[string]interface{}{
						"cid":     cidShort(key),
						"attempt": attempts + 1,
						"error":   err.Error(),
					})
					if attempts+1 < maxAttempts {
						select {
						case n.retryQueue <- c:
						default:
						}
					}
					continue
				}

				n.retryAttempts.Delete(key)
				if putErr := n.bstore.Put(ctx, blk); putErr != nil {
					continue
				}
				n.touchBlock(key, int64(len(blk.RawData())))
				n.mu.Lock()
				n.bitswapReceived++
				n.mu.Unlock()
				_ = n.dhtProvide(ctx, c)
				n.diagLog.Add("dht_announce", map[string]interface{}{"cid": cidShort(key), "role": "retry_pull"})
				log.Printf("retry: pulled %s on attempt %d", cidShort(key), attempts+1)
			}
		}
	}
}

// reannounceExistingBlocks re-publishes DHT provider records for all locally
// stored blocks after startup. Called after connectBootstrapPeers so the
// routing table has time to populate before Provide() calls are issued.
// Handles: binary restart, DHT VM redeployment, 24h record expiry.
func (n *BlockNode) reannounceExistingBlocks(ctx context.Context) {
	time.Sleep(15 * time.Second) // wait for Kademlia routing table to populate

	ch, err := n.bstore.AllKeysChan(ctx)
	if err != nil {
		log.Printf("reannounce: failed to list blocks: %v", err)
		return
	}

	queue := make(chan cid.Cid, GossipSubFetchWorkers)
	var wg sync.WaitGroup
	for i := 0; i < ReannounceWorkers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for c := range queue {
				_ = n.dhtProvide(ctx, c)
			}
		}()
	}
	count := 0
	for c := range ch {
		if ctx.Err() != nil {
			break
		}
		queue <- c
		count++
	}
	close(queue)
	wg.Wait()
	log.Printf("reannounce: complete — %d blocks re-announced to DHT", count)

	n.mu.Lock()
	n.diag.ReannounceCount++
	n.diag.LastReannounceAt = time.Now()
	n.mu.Unlock()
	n.diagLog.Add("reannounce_complete", map[string]interface{}{
		"blocks": count,
	})
}

// ═══════════════════════════════════════════════════════════════════
// Manifest registry
// ═══════════════════════════════════════════════════════════════════

func (n *BlockNode) loadExistingManifests(ctx context.Context) {
	dagsDir := filepath.Join(StorageDir, DagsSubdir)
	entries, err := os.ReadDir(dagsDir)
	if err != nil {
		return
	}
	count := 0
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(dagsDir, e.Name()))
		if err != nil {
			continue
		}
		var m ResourceManifest
		if err := json.Unmarshal(data, &m); err != nil {
			continue
		}
		n.manifests[m.RootCid] = &m
		count++
	}
	if count > 0 {
		log.Printf("Loaded %d manifests from disk", count)
	}
}

func (n *BlockNode) saveManifest(m *ResourceManifest) error {
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	path := filepath.Join(StorageDir, DagsSubdir, m.RootCid+".json")
	return os.WriteFile(path, data, 0644)
}
// ═══════════════════════════════════════════════════════════════════
// HTTP API
// ═══════════════════════════════════════════════════════════════════

func (n *BlockNode) startHTTPServer(ctx context.Context) {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", n.handleHealth)
	mux.HandleFunc("/blocks/", n.handleBlock)
	mux.HandleFunc("/blocks", n.handleBlocks)
	mux.HandleFunc("/stats", n.handleStats)
	mux.HandleFunc("/gc", n.handleGC)
	mux.HandleFunc("/connect", n.handleConnect)
	mux.HandleFunc("/manifests/", n.handleManifestByID)
	mux.HandleFunc("/manifests", n.handleManifests)
	mux.HandleFunc("/owners", n.handleOwnerList)
	mux.HandleFunc("/owners/", n.handleOwnerOps)
	mux.HandleFunc("/diagnostics", n.handleDiagnostics)
	mux.HandleFunc("/confirmed/", n.handleConfirmed)

	// Bind to all interfaces so the NodeAgent host can reach the API
	// via the virbr0 bridge (192.168.122.x). The API port is not
	// exposed via nginx and is not publicly reachable.
	addr := fmt.Sprintf("0.0.0.0:%d", n.cfg.APIPort)
	srv := &http.Server{
		Addr:         addr,
		Handler:      mux,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
	}
	go func() {
		log.Printf("HTTP API listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("HTTP server error: %v", err)
		}
	}()
	go func() {
		<-ctx.Done()
		_ = srv.Shutdown(context.Background())
	}()
}

// ── GET /health ──────────────────────────────────────────────────────────────
func (n *BlockNode) handleHealth(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	usedBytes, _ := n.usedBytes(ctx)
	count, _ := n.blockCount(ctx)
	bsStat, _ := n.bsExch.Stat()
	n.mu.RLock()
	recv := n.bitswapReceived
	n.mu.RUnlock()
	sent := bsStat.BlocksSent

	peerID := n.host.ID().String()
	if data, err := os.ReadFile(filepath.Join(StorageDir, PeerIDFile)); err == nil {
		peerID = strings.TrimSpace(string(data))
	}

	n.manifestsMu.RLock()
	manifestCount := len(n.manifests)
	n.manifestsMu.RUnlock()

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"status":          "ok",
		"peerId":          peerID,
		"connectedPeers":  len(n.host.Network().Peers()),
		"capacityBytes":   n.cfg.StorageBytes,
		"usedBytes":       usedBytes,
		"usagePercent":    float64(usedBytes) / float64(n.cfg.StorageBytes) * 100,
		"blockCount":      count,
		"manifestCount":   manifestCount,
		"bitswapSent":     sent,
		"bitswapReceived": recv,
		"nodeId":          n.cfg.NodeID,
		"vmId":            n.cfg.VMID,
	})
}

// ── GET|DELETE /blocks/{cid} ─────────────────────────────────────────────────
func (n *BlockNode) handleBlock(w http.ResponseWriter, r *http.Request) {
	cidStr := strings.TrimPrefix(r.URL.Path, "/blocks/")
	if cidStr == "" {
		http.Error(w, "missing CID", http.StatusBadRequest)
		return
	}
	c, err := cid.Decode(cidStr)
	if err != nil {
		http.Error(w, "invalid CID", http.StatusBadRequest)
		return
	}
	switch r.Method {
	case http.MethodGet:
		n.getBlock(w, r, c)
	case http.MethodDelete:
		n.deleteBlock(w, r, c)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (n *BlockNode) getBlock(w http.ResponseWriter, r *http.Request, c cid.Cid) {
	ctx := r.Context()
	blk, err := n.bstore.Get(ctx, c)
	if err != nil {
		fetchCtx, cancel := context.WithTimeout(ctx, BitswapTimeout)
		defer cancel()
		blk, err = n.bsExch.GetBlock(fetchCtx, c)
		if err != nil {
			http.Error(w, "block not found", http.StatusNotFound)
			return
		}
		_ = n.bstore.Put(ctx, blk)
		n.touchBlock(c.String(), int64(len(blk.RawData())))
		n.mu.Lock()
		n.bitswapReceived++
		n.mu.Unlock()
	} else {
		n.touchBlock(c.String(), int64(len(blk.RawData())))
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("X-Block-CID", c.String())
	_, _ = w.Write(blk.RawData())
}

func (n *BlockNode) deleteBlock(w http.ResponseWriter, r *http.Request, c cid.Cid) {
	ctx := r.Context()
	if err := n.bstore.DeleteBlock(ctx, c); err != nil {
		http.Error(w, "delete failed", http.StatusInternalServerError)
		return
	}
	n.accessMu.Lock()
	delete(n.accessTimes, c.String())
	delete(n.blockSizes, c.String())
	n.accessMu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

// ── POST /blocks ─────────────────────────────────────────────────────────────
func (n *BlockNode) handleBlocks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	ctx := r.Context()
	usedBytes, _ := n.usedBytes(ctx)
	if float64(usedBytes)/float64(n.cfg.StorageBytes)*100 >= GCHardLimit {
		http.Error(w, "storage full", http.StatusInsufficientStorage)
		return
	}
	data, err := io.ReadAll(io.LimitReader(r.Body, 64*1024*1024))
	if err != nil {
		http.Error(w, "read error", http.StatusBadRequest)
		return
	}
	mhash, err := multihash.Sum(data, multihash.SHA2_256, -1)
	if err != nil {
		http.Error(w, "hash error", http.StatusInternalServerError)
		return
	}
	c := cid.NewCidV1(cid.Raw, mhash)
	blk, err := blocks.NewBlockWithCid(data, c)
	if err != nil {
		http.Error(w, "block error", http.StatusInternalServerError)
		return
	}
	if err := n.bstore.Put(ctx, blk); err != nil {
		log.Printf("bstore.Put error: %v", err)
		http.Error(w, fmt.Sprintf("store error: %v", err), http.StatusInternalServerError)
		return
	}
	n.touchBlock(c.String(), int64(len(data)))

	// Append CID to owner index if ?owner= query param is present.
	owner := r.URL.Query().Get("owner")
	if owner != "" {
		go func(ownerID, cidStr string) {
			ownerFile := filepath.Join(StorageDir, OwnersSubdir, ownerID+".cids")
			f, err := os.OpenFile(ownerFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
			if err != nil {
				log.Printf("owner index: failed to open %s: %v", ownerFile, err)
				return
			}
			defer f.Close()
			if _, err := fmt.Fprintln(f, cidStr); err != nil {
				log.Printf("owner index: failed to write CID %s: %v", cidStr[:12], err)
			}
		}(owner, c.String())
	}

	// Announce to DHT (with diagnostics tracking).
	// dhtProvide retries with backoff — 60s context covers 3 attempts.
	go func(c cid.Cid) {
		announceCtx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		announceErr := n.dhtProvide(announceCtx, c)
		n.mu.Lock()
		if announceErr != nil {
			n.diag.DHTAnnounceFail++
			n.diagLog.Add("dht_announce_fail", map[string]interface{}{
				"cid":   cidShort(c.String()),
				"error": announceErr.Error(),
				"role":  "http_write",
			})
		} else {
			n.diag.DHTAnnounceSuccess++
			n.diag.LastAnnounceAt = time.Now()
			n.diagLog.Add("dht_announce", map[string]interface{}{
				"cid":  cidShort(c.String()),
				"role": "http_write",
			})
		}
		n.mu.Unlock()
	}(c)

	// Broadcast via GossipSub (with diagnostics tracking inside publishNewBlock)
	manifestVersion := 0
	if mv := r.URL.Query().Get("manifestVersion"); mv != "" {
		if n, err := strconv.Atoi(mv); err == nil {
			manifestVersion = n
		}
	}
	go n.publishNewBlock(c, int64(len(data)), owner, manifestVersion)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"cid": c.String()})
}

// ── GET /stats ───────────────────────────────────────────────────────────────
func (n *BlockNode) handleStats(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	usedBytes, _ := n.usedBytes(ctx)
	count, _ := n.blockCount(ctx)
	bsStat, _ := n.bsExch.Stat()
	n.mu.RLock()
	recv := n.bitswapReceived
	n.mu.RUnlock()
	sent := bsStat.BlocksSent
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"capacityBytes":   n.cfg.StorageBytes,
		"usedBytes":       usedBytes,
		"usagePercent":    float64(usedBytes) / float64(n.cfg.StorageBytes) * 100,
		"blockCount":      count,
		"connectedPeers":  len(n.host.Network().Peers()),
		"bitswapSent":     sent,
		"bitswapReceived": recv,
	})
}

// ── POST /confirmed/{vmId} ────────────────────────────────────────────────────
// Receives confirmed CID list from NodeAgent (forwarded from orchestrator).
// Writes to confirmed/{vmId}.cids — read by runGC() for priority eviction.
func (n *BlockNode) handleConfirmed(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	vmId := strings.TrimPrefix(r.URL.Path, "/confirmed/")
	vmId = strings.TrimSpace(vmId)
	if vmId == "" {
		http.Error(w, "missing vmId", http.StatusBadRequest)
		return
	}
 
	var req struct {
		Cids []string `json:"cids"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if len(req.Cids) == 0 {
		w.WriteHeader(http.StatusNoContent)
		return
	}
 
	confirmedFile := filepath.Join(StorageDir, ConfirmedSubdir, vmId+".cids")
	f, err := os.OpenFile(confirmedFile, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0644)
	if err != nil {
		log.Printf("confirmed: failed to open %s: %v", confirmedFile, err)
		http.Error(w, "write failed", http.StatusInternalServerError)
		return
	}
	defer f.Close()
 
	for _, cid := range req.Cids {
		if cid = strings.TrimSpace(cid); cid != "" {
			fmt.Fprintln(f, cid)
		}
	}
 
	log.Printf("confirmed: wrote %d CIDs for VM %s", len(req.Cids), vmId)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{"success": true, "count": len(req.Cids)})
}

// ── POST /gc ─────────────────────────────────────────────────────────────────
func (n *BlockNode) handleGC(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	freed, err := n.runGC(r.Context())
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	usedBytes, _ := n.usedBytes(r.Context())
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"freedBytes":     freed,
		"remainingBytes": usedBytes,
	})
}

// ── POST /connect ─────────────────────────────────────────────────────────────
func (n *BlockNode) handleConnect(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Peers []string `json:"peers"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	results := make([]map[string]any, 0, len(req.Peers))
	connected := 0
	for _, addrStr := range req.Peers {
		maddr, err := multiaddr.NewMultiaddr(addrStr)
		if err != nil {
			results = append(results, map[string]any{"addr": addrStr, "ok": false, "error": err.Error()})
			continue
		}
		pi, err := peer.AddrInfoFromP2pAddr(maddr)
		if err != nil {
			results = append(results, map[string]any{"addr": addrStr, "ok": false, "error": err.Error()})
			continue
		}
		connCtx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		err = n.host.Connect(connCtx, *pi)
		cancel()
		if err != nil {
			results = append(results, map[string]any{"addr": addrStr, "ok": false, "error": err.Error()})
		} else {
			results = append(results, map[string]any{"addr": addrStr, "ok": true})
			connected++
		}
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"results":   results,
		"connected": connected,
		"total":     len(req.Peers),
	})
}

// ── GET /manifests, POST /manifests ──────────────────────────────────────────
func (n *BlockNode) handleManifests(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	switch r.Method {
	case http.MethodGet:
		n.manifestsMu.RLock()
		list := make([]*ResourceManifest, 0, len(n.manifests))
		inManifests := make(map[string]bool) // keyed by resourceId (vmId)
		for _, m := range n.manifests {
			list = append(list, m)
			inManifests[m.ResourceID] = true
		}
		n.manifestsMu.RUnlock()

		// Synthesize manifests from owner index for VMs whose blocks were
		// pulled via bitswap (remote/receiver nodes). These nodes never
		// receive a POST /manifests call — they build their view locally
		// from the owner index written during bitswap pulls.
		ownersDir := filepath.Join(StorageDir, OwnersSubdir)
		if entries, err := os.ReadDir(ownersDir); err == nil {
			for _, e := range entries {
				if e.IsDir() || !strings.HasSuffix(e.Name(), ".cids") {
					continue
				}
				vmId := strings.TrimSuffix(e.Name(), ".cids")
				if inManifests[vmId] {
					continue // already have a richer manifest from POST /manifests
				}
				data, err := os.ReadFile(filepath.Join(ownersDir, e.Name()))
				if err != nil {
					continue
				}
				var cids []string
				for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
					if line = strings.TrimSpace(line); line != "" {
						cids = append(cids, line)
					}
				}
				if len(cids) == 0 {
					continue
				}
				info, _ := e.Info()
				updatedAt := time.Now().UTC()
				if info != nil {
					updatedAt = info.ModTime().UTC()
				}
				// Read true manifest version from the version file written by
				// handleManifests POST. Fall back to 0 if not yet received.
				manifestVersion := 0
				versionFile := filepath.Join(StorageDir, OwnersSubdir, vmId+".version")
				if vdata, err := os.ReadFile(versionFile); err == nil {
					if v, err := strconv.Atoi(strings.TrimSpace(string(vdata))); err == nil {
						manifestVersion = v
					}
				}

				list = append(list, &ResourceManifest{
					ResourceType: ResourceTypeVMOverlay,
					ResourceID:   vmId,
					Version:      manifestVersion,
					TotalBytes:   int64(len(cids)) * BlockSizeBytes,
					ChunkCIDs:    cids[max(0, len(cids)-50):], // last 50 CIDs
					RegisteredAt: updatedAt,
					UpdatedAt:    updatedAt,
				})
			}
		}

		sort.Slice(list, func(i, j int) bool {
			return list[i].UpdatedAt.After(list[j].UpdatedAt)
		})

		json.NewEncoder(w).Encode(map[string]any{
			"manifests": list,
			"count":     len(list),
		})

	case http.MethodPost:
		var m ResourceManifest
		if err := json.NewDecoder(r.Body).Decode(&m); err != nil {
			http.Error(w, "invalid JSON", http.StatusBadRequest)
			return
		}
		if m.RootCid == "" {
			http.Error(w, "rootCid required", http.StatusBadRequest)
			return
		}
		if m.ResourceType == "" {
			m.ResourceType = ResourceTypeUnknown
		}
		now := time.Now().UTC()
		n.manifestsMu.Lock()
		if existing, ok := n.manifests[m.RootCid]; ok {
			m.RegisteredAt = existing.RegisteredAt
		} else {
			m.RegisteredAt = now
		}
		m.UpdatedAt = now
		n.manifests[m.RootCid] = &m

		// Evict older versions for the same resourceId — keep only the latest.
		// The map is keyed by rootCid so each lazysync cycle adds a new entry.
		// Without pruning this grows unboundedly (one entry per cycle, forever).
		if m.ResourceID != "" {
			for k, v := range n.manifests {
				if v.ResourceID == m.ResourceID && k != m.RootCid && v.Version < m.Version {
					delete(n.manifests, k)
					_ = os.Remove(filepath.Join(StorageDir, DagsSubdir, k+".json"))
				}
			}
		}
		n.manifestsMu.Unlock()

		if err := n.saveManifest(&m); err != nil {
			log.Printf("Warning: failed to persist manifest %s: %v", m.RootCid[:12], err)
		}

		// Version file removed — see handleNewBlockAnnouncement.

		json.NewEncoder(w).Encode(map[string]any{
			"success": true,
			"rootCid": m.RootCid,
			"version": m.Version,
		})

	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

// ── GET /manifests/{rootCid} ─────────────────────────────────────────────────
func (n *BlockNode) handleManifestByID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	rootCid := strings.TrimPrefix(r.URL.Path, "/manifests/")
	if rootCid == "" {
		http.Error(w, "rootCid required", http.StatusBadRequest)
		return
	}
	n.manifestsMu.RLock()
	m, ok := n.manifests[rootCid]
	n.manifestsMu.RUnlock()
	if !ok {
		http.Error(w, "manifest not found", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(m)
}

// ── GET /owners ───────────────────────────────────────────────────────────────
// Returns list of vmIds this node has owner index files for.
// Called by joining peers to discover which VMs to catch up on.
func (n *BlockNode) handleOwnerList(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}
	entries, err := os.ReadDir(filepath.Join(StorageDir, OwnersSubdir))
	if err != nil {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"vmIds": []string{}})
		return
	}
	vmIds := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".cids") {
			vmIds = append(vmIds, strings.TrimSuffix(e.Name(), ".cids"))
		}
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{"vmIds": vmIds})
}

// ── GET|DELETE /owners/{vmId} ────────────────────────────────────────────────
// GET    → returns all CIDs in the owner index (used by peer catchup)
// DELETE → deletes all blocks owned by the VM
func (n *BlockNode) handleOwnerOps(w http.ResponseWriter, r *http.Request) {
	vmId := strings.TrimSpace(strings.TrimPrefix(r.URL.Path, "/owners/"))
	if vmId == "" {
		http.Error(w, "missing vmId", http.StatusBadRequest)
		return
	}
	ownerFile := filepath.Join(StorageDir, OwnersSubdir, vmId+".cids")
	switch r.Method {
	case http.MethodGet:
		data, err := os.ReadFile(ownerFile)
		if err != nil {
			if os.IsNotExist(err) {
				http.Error(w, "vmId not found", http.StatusNotFound)
				return
			}
			http.Error(w, "read error", http.StatusInternalServerError)
			return
		}
		var cids []string
		for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
			if line = strings.TrimSpace(line); line != "" {
				cids = append(cids, line)
			}
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"vmId": vmId, "cids": cids})
	case http.MethodDelete:
		if _, err := os.Stat(ownerFile); os.IsNotExist(err) {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		n.deleteOwnerBlocks(r.Context(), vmId)
		w.WriteHeader(http.StatusNoContent)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

// cidHasOtherOwners returns true if any owner file other than excludeOwner
// contains the given CID string.
func (n *BlockNode) cidHasOtherOwners(excludeOwner, cidStr string) (bool, error) {
	ownersDir := filepath.Join(StorageDir, OwnersSubdir)
	entries, err := os.ReadDir(ownersDir)
	if err != nil {
		return false, err
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".cids") {
			continue
		}
		ownerID := strings.TrimSuffix(e.Name(), ".cids")
		if ownerID == excludeOwner {
			continue
		}
		data, err := os.ReadFile(filepath.Join(ownersDir, e.Name()))
		if err != nil {
			continue
		}
		if strings.Contains(string(data), cidStr) {
			return true, nil
		}
	}
	return false, nil
}

// ── GET /diagnostics ─────────────────────────────────────────────────────────
// Full diagnostic snapshot: event log + aggregate counters + peer details.
// Used by the dashboard's Export feature and for debugging replication issues.
func (n *BlockNode) handleDiagnostics(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}

	diag := n.diag // value copy — avoids holding lock during JSON encode
	bsStat, _ := n.bsExch.Stat()
	n.mu.RLock()
	recv := n.bitswapReceived
	n.mu.RUnlock()
	sent := bsStat.BlocksSent

	// GossipSub topics this node has joined
	topics := n.pubsub.GetTopics()

	// Peer details with addresses
	peers := n.host.Network().Peers()
	peerDetails := make([]map[string]interface{}, 0, len(peers))
	for _, p := range peers {
		addrs := n.host.Network().Peerstore().Addrs(p)
		addrStrs := make([]string, len(addrs))
		for i, a := range addrs {
			addrStrs[i] = a.String()
		}
		peerDetails = append(peerDetails, map[string]interface{}{
			"id":    p.String(),
			"addrs": addrStrs,
		})
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"timestamp":      time.Now().UTC().Format(time.RFC3339),
		"connectedPeers": len(peers),
		"gossipSub": map[string]interface{}{
			"topics":            topics,
			"messagesReceived":  diag.GossipSubReceived,
			"messagesPublished": diag.GossipSubPublished,
			"lastReceivedAt":    diag.LastGossipSubRxAt,
			"lastPublishedAt":   diag.LastGossipSubTxAt,
		},
		"dhtAnnounce": map[string]interface{}{
			"success":          diag.DHTAnnounceSuccess,
			"fail":             diag.DHTAnnounceFail,
			"lastAt":           diag.LastAnnounceAt,
			"reannounceCount":  diag.ReannounceCount,
			"lastReannounceAt": diag.LastReannounceAt,
		},
		"xor": map[string]interface{}{
			"accepted": diag.XORAccepted,
			"rejected": diag.XORRejected,
		},
		"bitswap": map[string]interface{}{
			"received":  recv,
			"sent":      sent,
		},
		"gc": map[string]interface{}{
			"runCount":      diag.GCRunCount,
			"blocksEvicted": diag.GCBlocksEvicted,
			"bytesFreed":    diag.GCBytesFreed,
		},
		"peers":    peerDetails,
		"eventLog": n.diagLog.Snapshot(),
	})
}