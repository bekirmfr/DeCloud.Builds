#!/usr/bin/env python3
"""
DeCloud Generic Leaderboard Service (Python stdlib).

Delivery: shipped as an inline data: URI Script artifact. The tenant artifact
pipeline now lives in TenantVmTemplateSeeder (one tree -> one seeder, mirroring
SystemVmTemplateSeeder), so tenant-vms/compute-artifact-constants.sh emits the
LeaderboardApiPy* constants into TenantVmTemplateSeeder.Artifacts.cs. The node
SHA256-verifies the artifact on prefetch and serve; the role layer's runcmd
fetches it from the node-local cache to /opt/leaderboard/leaderboard-api.py and
runs it under systemd. No compiler, no module fetch, no build step. Standard
library only (http.server, sqlite3, json, hmac, secrets).

Config comes from the environment (set by the systemd unit's EnvironmentFile);
this script is NOT processed by cloud-init variable substitution.

TRUST BOUNDARY (read before deploying):
  - OPERATOR (deploy password -> ADMIN_TOKEN) mints/revokes apps.
  - An APP secret authenticates writes + board management for that app only.
  - PUBLIC reads need only the opaque board key.
  - This service authenticates the DEPLOYER'S SERVER, not end users. member_id
    and name are whatever the caller passes. Verifying a portal player token
    (CrazyGames/Poki/Yandex/etc.) is the deployer's backend's job. The service
    guarantees authenticated, persisted, and ranked — never that a submitted
    score is legitimate. Anti-cheat lives in the game layer. Submit from your
    server, not directly from an untrusted game client.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ── Limits (named, not magic — tune in one place) ───────────────────────────
MAX_BODY_BYTES = 16 * 1024          # a score submission is tiny
MAX_MEMBER_ID_LEN = 128
MAX_METADATA_BYTES = 1024
MAX_BOARD_NAME_LEN = 128
MAX_APP_LABEL_LEN = 128
MAX_LIST_COUNT = 100
DEFAULT_LIST_COUNT = 10
MAX_AROUND = 50
TOKEN_BYTES = 32                    # 256-bit secrets

SESSION_TTL_MS = 12 * 60 * 60 * 1000     # admin console session lifetime (12h)
SESSION_COOKIE = "lb_sid"
LOGIN_FAIL_MAX = 8                       # consecutive failures before lockout
LOGIN_FAIL_WINDOW_MS = 5 * 60 * 1000     # ...counted within this window
LOGIN_LOCK_MS = 5 * 60 * 1000            # lockout duration once tripped
LOGIN_DELAY_S = 0.4                      # per-failure delay (throttle + timing floor)

# Rights the operator can grant on an access key, per (app, board). Board
# lifecycle (create/delete) is operator-only and deliberately NOT grantable.
VALID_KEY_SCOPES = ("submit", "member:delete")
DEFAULT_KEY_SCOPES = ("submit",)

# What happens when a member that already has a row submits again:
#   keep_best  - update only if the new score beats the old (per direction_method)
#   overwrite  - the latest submission always replaces
#   first      - lock to the first submission; later submits are ignored
VALID_WRITE_POLICIES = ("keep_best", "overwrite", "first")

PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "/var/lib/leaderboard/leaderboard.db")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


# ── Storage ─────────────────────────────────────────────────────────────────

def connect():
    # Connection per request: cheap for SQLite, avoids cross-thread sharing.
    # WAL + busy_timeout let concurrent readers and one writer coexist without
    # us inventing our own locking — align to SQLite's own boundary.
    # isolation_level=None → autocommit: each statement is durable the moment it
    # returns, so a write is visible to the next request's connection before we
    # send the HTTP response. (Handlers reply inside the connection scope; without
    # autocommit a fast client can read-after-write on a new connection and miss
    # the not-yet-committed row.) No handler needs multi-statement atomicity.
    conn = sqlite3.connect(DB_PATH, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS apps (
              app_id      TEXT PRIMARY KEY,
              label       TEXT NOT NULL DEFAULT '',
              created_at  INTEGER NOT NULL,
              revoked_at  INTEGER
            );
            CREATE TABLE IF NOT EXISTS boards (
              board_key                 TEXT PRIMARY KEY,
              name                      TEXT NOT NULL DEFAULT '',
              direction_method          TEXT NOT NULL DEFAULT 'descending',
              write_policy              TEXT NOT NULL DEFAULT 'keep_best',
              allow_public_submit       INTEGER NOT NULL DEFAULT 0,
              created_at                INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scores (
              board_key  TEXT NOT NULL REFERENCES boards(board_key) ON DELETE CASCADE,
              member_id  TEXT NOT NULL,
              score      INTEGER NOT NULL,
              metadata   TEXT NOT NULL DEFAULT '',
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (board_key, member_id)
            );
            CREATE INDEX IF NOT EXISTS idx_scores_rank
              ON scores(board_key, score, updated_at, member_id);
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY,
              created_at INTEGER NOT NULL,
              expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS access_keys (
              key_id      TEXT PRIMARY KEY,
              app_id      TEXT NOT NULL REFERENCES apps(app_id) ON DELETE CASCADE,
              board_key   TEXT NOT NULL REFERENCES boards(board_key) ON DELETE CASCADE,
              scopes      TEXT NOT NULL DEFAULT 'submit',
              secret_hash TEXT NOT NULL UNIQUE,
              created_at  INTEGER NOT NULL,
              revoked_at  INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_keys_app   ON access_keys(app_id);
            CREATE INDEX IF NOT EXISTS idx_keys_board ON access_keys(board_key);
            """
        )
        _migrate(conn)


def _migrate(conn):
    # v1 → v2: boards are project-wide (drop owner app_id) and apps no longer
    # carry a secret (credentials moved to access_keys). SQLite 3.35+ supports
    # DROP COLUMN; guarded on table_info so it is idempotent. Leaderboard data
    # (boards, scores) is preserved; legacy app secrets stop working — the
    # credential model changed, so the operator re-issues access keys.
    def cols(table):
        return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    if "app_id" in cols("boards"):
        conn.execute("DROP INDEX IF EXISTS idx_boards_app")
        conn.execute("ALTER TABLE boards DROP COLUMN app_id")
    if "secret_hash" in cols("apps"):
        conn.execute("ALTER TABLE apps DROP COLUMN secret_hash")
    # v2 → v3: per-board browser-submit policy. Default 0 = cors-default
    # (server-to-server only); the operator opts a board into cors-public.
    if "allow_public_submit" not in cols("boards"):
        conn.execute(
            "ALTER TABLE boards ADD COLUMN allow_public_submit INTEGER NOT NULL DEFAULT 0"
        )
    # v3 → v4: collapse the overwrite_score_on_submit boolean into a 3-valued
    # write_policy. Backfill overwrite=1 → 'overwrite', else the default
    # 'keep_best', then drop the legacy column.
    if "write_policy" not in cols("boards"):
        conn.execute(
            "ALTER TABLE boards ADD COLUMN write_policy TEXT NOT NULL DEFAULT 'keep_best'"
        )
        if "overwrite_score_on_submit" in cols("boards"):
            conn.execute(
                "UPDATE boards SET write_policy = 'overwrite'"
                " WHERE overwrite_score_on_submit = 1"
            )
    if "overwrite_score_on_submit" in cols("boards"):
        conn.execute("ALTER TABLE boards DROP COLUMN overwrite_score_on_submit")


# ── Small helpers ─────────────────────────────────────────────────────────

def now_ms():
    return int(time.time() * 1000)


def new_token(nbytes):
    return secrets.token_hex(nbytes)


def hash_token(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def ct_equal(a, b):
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def parse_cookies(header):
    out = {}
    if not header:
        return out
    for part in header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


# Admin-login throttle. The VM sits behind CentralIngress, so per-IP XFF is
# untrusted — throttle the login endpoint as a whole. Global, lock-guarded.
_login_lock = threading.Lock()
_login_fails = []
_login_locked_until = 0


def login_locked(now):
    with _login_lock:
        return now < _login_locked_until


def login_register_failure(now):
    global _login_locked_until
    with _login_lock:
        _login_fails.append(now)
        cutoff = now - LOGIN_FAIL_WINDOW_MS
        while _login_fails and _login_fails[0] < cutoff:
            _login_fails.pop(0)
        if len(_login_fails) >= LOGIN_FAIL_MAX:
            _login_locked_until = now + LOGIN_LOCK_MS
            _login_fails.clear()


def login_reset():
    global _login_locked_until
    with _login_lock:
        _login_fails.clear()
        _login_locked_until = 0


def ranked_cte(direction):
    # direction is validated to one of two literals before reaching here, so the
    # interpolation is injection-safe. One deterministic total order for every
    # read: score (per direction), earliest-to-reach-it, then member_id.
    score_order = "ASC" if direction == "ascending" else "DESC"
    return (
        "WITH ranked AS ("
        " SELECT member_id, score, metadata,"
        f" ROW_NUMBER() OVER (ORDER BY score {score_order}, updated_at ASC, member_id ASC) AS rank"
        " FROM scores WHERE board_key = ?"
        ") "
    )


def member_entry(conn, board_key, direction, member_id):
    row = conn.execute(
        ranked_cte(direction)
        + "SELECT rank, member_id, score, metadata FROM ranked WHERE member_id = ?",
        (board_key, member_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "rank": row["rank"],
        "member_id": row["member_id"],
        "score": row["score"],
        "metadata": row["metadata"],
    }


# ── Request handler ─────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "DeCloudLeaderboard/1.0"

    # Routes: (method, compiled-regex, handler-name, auth-level)
    ROUTES = []  # populated after class definition

    # — response helpers —
    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Cross-origin is allowed only on routes the route table marks "cors":
        # the public reads, and POST /submit (a board the operator opted into
        # browser writes with a submit-only key). Admin, key-management, and
        # member:delete responses never carry this header.
        if getattr(self, "_cors", False):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, msg):
        self._json(code, {"error": msg})

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY_BYTES:
            self._err(413, "request body too large")
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError
            return obj
        except (ValueError, json.JSONDecodeError):
            self._err(400, "invalid JSON body")
            return None

    # — auth —
    def _bearer(self):
        # x-session-token (LootLocker convention), then x-admin-token, then Bearer.
        t = self.headers.get("x-session-token")
        if t:
            return t
        t = self.headers.get("x-admin-token")
        if t:
            return t
        a = self.headers.get("Authorization", "")
        if a.startswith("Bearer "):
            return a[len("Bearer "):]
        return ""

    def _resolve_key(self, conn):
        # An access key is a secret bound to one (app, board) with granted scopes.
        # Live only if the key itself AND its parent app are both un-revoked.
        secret = self._bearer()
        if not secret:
            return None
        return conn.execute(
            "SELECT k.key_id, k.app_id, k.board_key, k.scopes"
            " FROM access_keys k JOIN apps a ON a.app_id = k.app_id"
            " WHERE k.secret_hash = ? AND k.revoked_at IS NULL AND a.revoked_at IS NULL",
            (hash_token(secret),),
        ).fetchone()

    # — board lookup + the multi-tenant isolation invariant —
    def _lookup_board(self, conn, key):
        return conn.execute(
            "SELECT board_key, name, direction_method, write_policy"
            " FROM boards WHERE board_key = ?",
            (key,),
        ).fetchone()

    # ── Admin console: session + security helpers ────────────────
    def _cookie(self, name):
        return parse_cookies(self.headers.get("Cookie")).get(name)

    def _session_ok(self, conn):
        tok = self._cookie(SESSION_COOKIE)
        if not tok:
            return False
        now = now_ms()
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        return conn.execute(
            "SELECT 1 FROM sessions WHERE token_hash = ? AND expires_at > ?",
            (hash_token(tok), now),
        ).fetchone() is not None

    def _admin_via_header(self):
        tok = self.headers.get("x-admin-token")
        return bool(ADMIN_TOKEN) and bool(tok) and ct_equal(tok, ADMIN_TOKEN)

    def _csrf_ok(self):
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if origin:
            return urlparse(origin).netloc == host
        ref = self.headers.get("Referer")
        if ref:
            return urlparse(ref).netloc == host
        return False

    def _set_cookie(self, value, max_age):
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={value}; Max-Age={max_age}; Path=/; "
            "HttpOnly; Secure; SameSite=Strict",
        )

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def h_login(self, conn, match, qs):
        now = now_ms()
        if login_locked(now):
            return self._err(429, "too many attempts, try again later")
        body = self._read_json()
        if body is None:
            return
        pw = str(body.get("password", ""))
        if not (ADMIN_TOKEN and ct_equal(pw, ADMIN_TOKEN)):
            login_register_failure(now)
            time.sleep(LOGIN_DELAY_S)
            return self._err(401, "invalid credentials")
        login_reset()
        token = new_token(TOKEN_BYTES)
        conn.execute(
            "INSERT INTO sessions(token_hash, created_at, expires_at) VALUES(?,?,?)",
            (hash_token(token), now, now + SESSION_TTL_MS),
        )
        self.send_response(204)
        self._set_cookie(token, SESSION_TTL_MS // 1000)
        self._security_headers()
        self.end_headers()

    def h_logout(self, conn, match, qs):
        tok = self._cookie(SESSION_COOKIE)
        if tok:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(tok),))
        self.send_response(204)
        self._set_cookie("", 0)
        self._security_headers()
        self.end_headers()

    def h_console(self, conn, match, qs):
        nonce = secrets.token_urlsafe(16)
        body = CONSOLE_HTML.replace("__NONCE__", nonce).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            "style-src 'unsafe-inline'; "
            "connect-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ── Shared board insert (app + admin create) ────────────────
    def _create_board(self, conn):
        body = self._read_json()
        if body is None:
            return None
        name = str(body.get("name", ""))
        if len(name) > MAX_BOARD_NAME_LEN:
            self._err(400, "name too long")
            return None
        direction = str(body.get("direction_method", "") or "descending").strip().lower()
        if direction not in ("ascending", "descending"):
            self._err(400, "direction_method must be 'ascending' or 'descending'")
            return None
        policy = str(body.get("write_policy", "") or "keep_best").strip().lower()
        if policy not in VALID_WRITE_POLICIES:
            self._err(400, "write_policy must be one of: " + ", ".join(VALID_WRITE_POLICIES))
            return None
        allow_public = 1 if body.get("allow_public_submit") else 0
        key = "lb_" + new_token(12)
        conn.execute(
            "INSERT INTO boards(board_key, name, direction_method,"
            " write_policy, allow_public_submit, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (key, name, direction, policy, allow_public, now_ms()),
        )
        return {
            "key": key, "name": name, "direction_method": direction,
            "write_policy": policy,
            "allow_public_submit": bool(allow_public),
        }

    # ── Admin board management (operator owns every app on the VM) ─────
    def h_admin_list_boards(self, conn, match, qs):
        rows = conn.execute(
            "SELECT board_key, name, direction_method, write_policy,"
            " allow_public_submit, created_at FROM boards ORDER BY created_at"
        ).fetchall()
        self._json(200, {"boards": [{
            "key": r["board_key"], "name": r["name"],
            "direction_method": r["direction_method"],
            "write_policy": r["write_policy"],
            "allow_public_submit": bool(r["allow_public_submit"]),
            "created_at": r["created_at"],
        } for r in rows]})

    def h_admin_create_board(self, conn, match, qs):
        out = self._create_board(conn)
        if out is not None:
            self._json(201, out)

    def h_admin_update_board(self, conn, match, qs):
        # Toggle a board between cors-default (server-to-server) and cors-public
        # (browser-submittable) without losing its scores.
        body = self._read_json()
        if body is None:
            return
        if "allow_public_submit" not in body:
            return self._err(400, "allow_public_submit (bool) required")
        allow = 1 if body.get("allow_public_submit") else 0
        cur = conn.execute(
            "UPDATE boards SET allow_public_submit = ? WHERE board_key = ?",
            (allow, match.group("key")),
        )
        if cur.rowcount == 0:
            return self._err(404, "board not found")
        self._json(200, {"key": match.group("key"), "allow_public_submit": bool(allow)})

    def h_admin_delete_board(self, conn, match, qs):
        cur = conn.execute("DELETE FROM boards WHERE board_key = ?", (match.group("key"),))
        if cur.rowcount == 0:
            return self._err(404, "board not found")
        self._json(204, {})

    def h_admin_delete_member(self, conn, match, qs):
        cur = conn.execute(
            "DELETE FROM scores WHERE board_key = ? AND member_id = ?",
            (match.group("key"), match.group("member_id")),
        )
        if cur.rowcount == 0:
            return self._err(404, "member not found")
        self._json(204, {})

    def h_admin_set_member(self, conn, match, qs):
        # Operator override: set a member's score/metadata exactly, creating the
        # row if absent. Unlike submit, this bypasses keep-best — a correction
        # sets the value the operator typed, regardless of direction/overwrite.
        key = match.group("key")
        b = self._lookup_board(conn, key)
        if b is None:
            return self._err(404, "board not found")
        member_id = match.group("member_id")
        if len(member_id) > MAX_MEMBER_ID_LEN:
            return self._err(400, "member_id too long")
        body = self._read_json()
        if body is None:
            return
        if not isinstance(body.get("score"), int) or isinstance(body.get("score"), bool):
            return self._err(400, "score must be an integer")
        metadata = str(body.get("metadata", ""))
        if len(metadata.encode("utf-8")) > MAX_METADATA_BYTES:
            return self._err(400, "metadata too long")
        conn.execute(
            "INSERT INTO scores(board_key, member_id, score, metadata, updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(board_key, member_id) DO UPDATE SET"
            " score=excluded.score, metadata=excluded.metadata, updated_at=excluded.updated_at",
            (key, member_id, body["score"], metadata, now_ms()),
        )
        self._json(200, member_entry(conn, key, b["direction_method"], member_id) or {})

    # ── Admin: access keys (per-(app,board) credential + grant) ───────
    def h_admin_issue_key(self, conn, match, qs):
        app_id = match.group("app_id")
        if conn.execute(
            "SELECT 1 FROM apps WHERE app_id = ? AND revoked_at IS NULL", (app_id,)
        ).fetchone() is None:
            return self._err(404, "app not found")
        body = self._read_json()
        if body is None:
            return
        board_key = str(body.get("board_key", ""))
        if conn.execute(
            "SELECT 1 FROM boards WHERE board_key = ?", (board_key,)
        ).fetchone() is None:
            return self._err(404, "board not found")
        requested = body.get("scopes")
        if requested is None:
            scopes = list(DEFAULT_KEY_SCOPES)
        elif isinstance(requested, list):
            scopes = [str(x) for x in requested]
        else:
            return self._err(400, "scopes must be a list")
        for sc in scopes:
            if sc not in VALID_KEY_SCOPES:
                return self._err(400, "unknown scope")
        if not scopes:
            return self._err(400, "at least one scope is required")
        scopes = sorted(set(scopes))
        key_id = "key_" + new_token(8)
        secret = "sk_" + new_token(TOKEN_BYTES)
        conn.execute(
            "INSERT INTO access_keys(key_id, app_id, board_key, scopes, secret_hash, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (key_id, app_id, board_key, " ".join(scopes), hash_token(secret), now_ms()),
        )
        self._json(201, {
            "key_id": key_id, "app_id": app_id, "board_key": board_key,
            "scopes": scopes, "secret": secret,
            "note": "Store secret now — it is not retrievable later.",
        })

    def h_admin_list_keys(self, conn, match, qs):
        app_id = match.group("app_id")
        rows = conn.execute(
            "SELECT key_id, board_key, scopes, created_at, revoked_at"
            " FROM access_keys WHERE app_id = ? ORDER BY created_at", (app_id,),
        ).fetchall()
        self._json(200, {"keys": [{
            "key_id": r["key_id"], "board_key": r["board_key"],
            "scopes": r["scopes"].split(), "created_at": r["created_at"],
            "revoked": r["revoked_at"] is not None,
        } for r in rows]})

    def h_admin_revoke_key(self, conn, match, qs):
        cur = conn.execute(
            "UPDATE access_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL",
            (now_ms(), match.group("key_id")),
        )
        if cur.rowcount == 0:
            return self._err(404, "key not found or already revoked")
        self._json(204, {})

    # — dispatch —
    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        self._cors = False
        for route in Handler.ROUTES:
            m, rx, name, auth = route[0], route[1], route[2], route[3]
            scope = route[4] if len(route) > 4 else None
            if m != method:
                continue
            match = rx.match(path)
            if not match:
                continue
            # Cross-origin allowed only on routes explicitly marked "cors"
            # (the public reads and POST /submit); _json emits ACAO when set.
            self._cors = "cors" in route[4:]
            try:
                with connect() as conn:
                    if auth == "admin":
                        via_cookie = self._session_ok(conn)
                        via_header = self._admin_via_header()
                        if not (via_cookie or via_header):
                            return self._err(401, "admin authentication required")
                        if method in ("POST", "DELETE", "PATCH", "PUT") and via_cookie and not via_header:
                            if not self._csrf_ok():
                                return self._err(403, "cross-origin request rejected")
                        return getattr(self, name)(conn, match, qs)
                    if auth == "key":
                        if "cors_board" in route[4:]:
                            row = conn.execute(
                                "SELECT allow_public_submit FROM boards"
                                " WHERE board_key = ?", (match.group("key"),),
                            ).fetchone()
                            self._cors = bool(row and row["allow_public_submit"])
                        ak = self._resolve_key(conn)
                        if ak is None:
                            return self._err(401, "invalid access key")
                        # The key is bound to exactly one board; used on any other
                        # board it sees a 404 (no existence leak).
                        if match.group("key") != ak["board_key"]:
                            return self._err(404, "board not found")
                        if scope and scope not in ak["scopes"].split():
                            return self._err(403, "insufficient scope")
                        return getattr(self, name)(conn, match, qs, ak)
                    return getattr(self, name)(conn, match, qs)
            except Exception:  # noqa: BLE001 — never leak internals to the client
                return self._err(500, "internal error")
        self._err(404, "not found")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_OPTIONS(self):
        # CORS preflight, answered only for routes marked "cors" (the public
        # reads and POST /submit). Any other path falls through to 404 so the
        # browser blocks the write. No auth and no DB — a preflight carries none.
        self._cors = False
        path = urlparse(self.path).path
        methods = set()
        for route in Handler.ROUTES:
            mt = route[1].match(path)
            if not mt:
                continue
            if "cors" in route[4:]:
                methods.add(route[0])
            elif "cors_board" in route[4:]:
                with connect() as conn:
                    row = conn.execute(
                        "SELECT allow_public_submit FROM boards"
                        " WHERE board_key = ?", (mt.group("key"),),
                    ).fetchone()
                if row and row["allow_public_submit"]:
                    methods.add(route[0])
        if not methods:
            return self._err(404, "not found")
        methods = sorted(methods)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", ", ".join(methods + ["OPTIONS"]))
        self.send_header("Access-Control-Allow-Headers", "content-type, x-session-token")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        # Journald captures stderr; keep a terse access line, no bodies/secrets.
        pass

    # ── Health ──────────────────────────────────────────────────────────────
    def h_health(self, conn, match, qs):
        conn.execute("SELECT 1")
        self._json(200, {"status": "ok"})

    # ── Operator handlers ─────────────────────────────────────────────────
    def h_create_app(self, conn, match, qs):
        body = self._read_json()
        if body is None:
            return
        label = str(body.get("label", ""))
        if len(label) > MAX_APP_LABEL_LEN:
            return self._err(400, "label too long")
        app_id = "app_" + new_token(8)
        conn.execute(
            "INSERT INTO apps(app_id, label, created_at) VALUES(?,?,?)",
            (app_id, label, now_ms()),
        )
        # An app is an identity; access secrets are issued per board under it.
        self._json(201, {"app_id": app_id, "label": label})

    def h_list_apps(self, conn, match, qs):
        rows = conn.execute(
            "SELECT app_id, label, created_at, revoked_at FROM apps ORDER BY created_at"
        ).fetchall()
        apps = [{
            "app_id": r["app_id"],
            "label": r["label"],
            "created_at": r["created_at"],
            "revoked": r["revoked_at"] is not None,
        } for r in rows]
        self._json(200, {"apps": apps})

    def h_revoke_app(self, conn, match, qs):
        app_id = match.group("app_id")
        cur = conn.execute(
            "UPDATE apps SET revoked_at = ? WHERE app_id = ? AND revoked_at IS NULL",
            (now_ms(), app_id),
        )
        if cur.rowcount == 0:
            return self._err(404, "app not found or already revoked")
        self._json(204, {})

    # ── Key-authenticated writes (one board, granted scopes only) ───
    def h_submit(self, conn, match, qs, ak):
        b = self._lookup_board(conn, ak["board_key"])
        if b is None:
            return self._err(404, "board not found")
        body = self._read_json()
        if body is None:
            return
        member_id = str(body.get("member_id", "")).strip()
        if not member_id or len(member_id) > MAX_MEMBER_ID_LEN:
            return self._err(400, "member_id required, max 128 chars")
        if not isinstance(body.get("score"), int) or isinstance(body.get("score"), bool):
            return self._err(400, "score must be an integer")
        score = body["score"]
        metadata = str(body.get("metadata", ""))
        if len(metadata.encode("utf-8")) > MAX_METADATA_BYTES:
            return self._err(400, "metadata too long")

        key = b["board_key"]
        # The board's write_policy decides what a repeat submission does. The
        # conflict clause encodes it: DO NOTHING locks to the first row; an
        # unconditional DO UPDATE overwrites; a WHERE on the update keeps the best.
        insert = (
            "INSERT INTO scores(board_key, member_id, score, metadata, updated_at)"
            " VALUES(?,?,?,?,?)"
        )
        update = (
            " ON CONFLICT(board_key, member_id) DO UPDATE SET"
            " score=excluded.score, metadata=excluded.metadata, updated_at=excluded.updated_at"
        )
        policy = b["write_policy"]
        if policy == "first":
            sql = insert + " ON CONFLICT(board_key, member_id) DO NOTHING"
        elif policy == "overwrite":
            sql = insert + update
        elif b["direction_method"] == "descending":
            sql = insert + update + " WHERE excluded.score > scores.score"
        else:  # keep_best, ascending: lower is better
            sql = insert + update + " WHERE excluded.score < scores.score"
        conn.execute(sql, (key, member_id, score, metadata, now_ms()))

        # Return the member's authoritative current standing (not the value just
        # submitted, which keep-best may have rejected).
        entry = member_entry(conn, key, b["direction_method"], member_id)
        self._json(200, entry or {})

    def h_delete_member(self, conn, match, qs, ak):
        cur = conn.execute(
            "DELETE FROM scores WHERE board_key = ? AND member_id = ?",
            (ak["board_key"], match.group("member_id")),
        )
        if cur.rowcount == 0:
            return self._err(404, "member not found")
        self._json(204, {})

    # ── Public read handlers (board key is the read capability) ────────────
    def h_list(self, conn, match, qs):
        b = self._lookup_board(conn, match.group("key"))
        if b is None:
            return self._err(404, "board not found")
        count = clamp(_qint(qs, "count", DEFAULT_LIST_COUNT), 1, MAX_LIST_COUNT)
        after = _qint(qs, "after", 0)  # cursor = last seen rank; 0 = start
        direction = b["direction_method"]
        rows = conn.execute(
            ranked_cte(direction)
            + "SELECT rank, member_id, score, metadata FROM ranked"
            " WHERE rank > ? ORDER BY rank LIMIT ?",
            (b["board_key"], after, count + 1),  # one extra to detect "more"
        ).fetchall()
        items = [_entry(r) for r in rows]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM scores WHERE board_key = ?", (b["board_key"],)
        ).fetchone()["n"]
        next_cursor = None
        if len(items) > count:
            next_cursor = items[count - 1]["rank"]
            items = items[:count]
        self._json(200, {
            "items": items,
            "pagination": {
                "total": total,
                "next_cursor": next_cursor,
                "previous_cursor": after if after > 0 else None,
            },
        })

    def h_member(self, conn, match, qs):
        b = self._lookup_board(conn, match.group("key"))
        if b is None:
            return self._err(404, "board not found")
        member_id = match.group("member_id")
        around = clamp(_qint(qs, "around", 0), 0, MAX_AROUND)
        direction = b["direction_method"]
        me = member_entry(conn, b["board_key"], direction, member_id)
        if me is None:
            return self._err(404, "member not on this board")
        if around == 0:
            return self._json(200, me)
        lo, hi = me["rank"] - around, me["rank"] + around
        rows = conn.execute(
            ranked_cte(direction)
            + "SELECT rank, member_id, score, metadata FROM ranked"
            " WHERE rank BETWEEN ? AND ? ORDER BY rank",
            (b["board_key"], lo, hi),
        ).fetchall()
        self._json(200, {"member_id": member_id, "items": [_entry(r) for r in rows]})


def _entry(row):
    return {
        "rank": row["rank"],
        "member_id": row["member_id"],
        "score": row["score"],
        "metadata": row["metadata"],
    }


def _qint(qs, key, default):
    vals = qs.get(key)
    if not vals:
        return default
    try:
        return int(vals[0])
    except (ValueError, TypeError):
        return default


# Route table — compiled once. {param} segments become named groups.
Handler.ROUTES = [
    ("GET", re.compile(r"^/health$"), "h_health", "public", "cors"),
    # Admin console (HTML) + session lifecycle
    ("GET", re.compile(r"^/$"), "h_console", "public"),
    ("POST", re.compile(r"^/admin/login$"), "h_login", "public"),
    ("POST", re.compile(r"^/admin/logout$"), "h_logout", "public"),
    # Operator: apps (identity only; no secret)
    ("POST", re.compile(r"^/admin/apps$"), "h_create_app", "admin"),
    ("GET", re.compile(r"^/admin/apps$"), "h_list_apps", "admin"),
    ("DELETE", re.compile(r"^/admin/apps/(?P<app_id>[^/]+)$"), "h_revoke_app", "admin"),
    # Operator: project-wide boards (lifecycle is operator-only)
    ("GET", re.compile(r"^/admin/boards$"), "h_admin_list_boards", "admin"),
    ("POST", re.compile(r"^/admin/boards$"), "h_admin_create_board", "admin"),
    ("PATCH", re.compile(r"^/admin/boards/(?P<key>[^/]+)$"), "h_admin_update_board", "admin"),
    ("DELETE", re.compile(r"^/admin/boards/(?P<key>[^/]+)$"), "h_admin_delete_board", "admin"),
    ("DELETE", re.compile(r"^/admin/boards/(?P<key>[^/]+)/members/(?P<member_id>[^/]+)$"),
     "h_admin_delete_member", "admin"),
    ("PUT", re.compile(r"^/admin/boards/(?P<key>[^/]+)/members/(?P<member_id>[^/]+)$"),
     "h_admin_set_member", "admin"),
    # Operator: access keys issued under an app, each bound to one board
    ("GET", re.compile(r"^/admin/apps/(?P<app_id>[^/]+)/keys$"), "h_admin_list_keys", "admin"),
    ("POST", re.compile(r"^/admin/apps/(?P<app_id>[^/]+)/keys$"), "h_admin_issue_key", "admin"),
    ("DELETE", re.compile(r"^/admin/keys/(?P<key_id>[^/]+)$"), "h_admin_revoke_key", "admin"),
    # Key-authenticated writes (secret bound to one board; scope-gated)
    ("POST", re.compile(r"^/leaderboards/(?P<key>[^/]+)/submit$"), "h_submit", "key", "submit", "cors_board"),
    ("DELETE", re.compile(r"^/leaderboards/(?P<key>[^/]+)/members/(?P<member_id>[^/]+)$"),
     "h_delete_member", "key", "member:delete"),
    # Public reads (board key is the read capability)
    ("GET", re.compile(r"^/leaderboards/(?P<key>[^/]+)/list$"), "h_list", "public", "cors"),
    ("GET", re.compile(r"^/leaderboards/(?P<key>[^/]+)/member/(?P<member_id>[^/]+)$"),
     "h_member", "public", "cors"),
]


CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeCloud Leaderboard \u2014 Admin</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 15px/1.5 system-ui, -apple-system, sans-serif; margin: 0; padding: 2rem 1.2rem; max-width: 900px; margin-inline: auto; }
  h1 { font-size: 1.3rem; margin: 0; } h2 { font-size: 1.02rem; margin: 1.6rem 0 .5rem; }
  input, select, button { font: inherit; padding: .45rem .6rem; border-radius: 8px; border: 1px solid #8886; background: transparent; color: inherit; }
  button { cursor: pointer; background: #2563eb; color: #fff; border-color: transparent; }
  button.secondary { background: transparent; color: inherit; border: 1px solid #8886; }
  button.danger { background: #dc2626; color: #fff; }
  .row { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }
  .between { justify-content: space-between; }
  .card { border: 1px solid #8884; border-radius: 12px; padding: 1rem; margin: .7rem 0; }
  .muted { opacity: .65; font-size: .85rem; }
  .secret { font-family: ui-monospace, monospace; word-break: break-all; background: #8881; padding: .55rem .65rem; border-radius: 8px; margin: .4rem 0; }
  .err { color: #dc2626; min-height: 1.2em; }
  .badge { font-size: .72rem; padding: .12rem .45rem; border-radius: 999px; background: #8882; margin-left: .35rem; }
  .hidden { display: none; }
  .keys { margin-top: .6rem; border-top: 1px dashed #8884; padding-top: .6rem; }
  .brow { padding: .35rem 0; }
  summary { cursor: pointer; }
  ol { margin: .5rem 0 .3rem; padding-left: 1.2rem; }
  li { margin: .3rem 0; }
  code { font-family: ui-monospace, monospace; background: #8881; padding: 0 .25rem; border-radius: 4px; }
</style>
</head>
<body>
  <section id="login">
    <h1>Leaderboard Admin</h1>
    <p class="muted">Sign in with this VM's root (deploy) password.</p>
    <div class="row">
      <input id="pw" type="password" placeholder="Root password" autocomplete="current-password" style="min-width:18rem">
      <button id="loginBtn">Sign in</button>
    </div>
    <p id="loginErr" class="err"></p>
  </section>

  <section id="app" class="hidden">
    <div class="row between">
      <h1>Leaderboard Admin</h1>
      <button id="logoutBtn" class="secondary">Sign out</button>
    </div>

    <details class="card" open>
      <summary><strong>How to use</strong></summary>
      <ol>
        <li><strong>Create a board</strong> below. Boards are project-wide and shared; the board key is the public read capability.</li>
        <li><strong>Create an app</strong> &mdash; just a label (a game or partner). Apps hold no secret.</li>
        <li><strong>Issue an access key</strong> under the app, bound to that board, with the rights you grant: <code>submit</code> (default) and optionally <code>member:delete</code>. The secret is shown once &mdash; store it then.</li>
        <li>Your <strong>server</strong> submits scores with the key secret in the <code>x-session-token</code> header. By default a board is <strong>server-only</strong> &mdash; browsers can't submit.</li>
      </ol>
      <p class="muted">Reads are public &mdash; anyone with a board key can read rankings, no auth. A key works only on its one board and only for the rights you granted. Revoke a key, or revoke its app to disable every key under it at once.</p>
      <p class="muted">A board can be made <strong>public submit</strong> (toggle below) so a browser game with no backend can post directly. The key then lives in the client, so use a submit-only key &mdash; anyone can post scores to that board. Leave boards server-only when score integrity matters.</p>
    </details>

    <h2>Boards</h2>
    <div class="row">
      <input id="bName" placeholder="Board name" style="min-width:12rem">
      <select id="bDir">
        <option value="descending">descending (higher wins)</option>
        <option value="ascending">ascending (lower wins)</option>
      </select>
      <select id="bPolicy">
        <option value="keep_best">keep best</option>
        <option value="overwrite">overwrite (latest)</option>
        <option value="first">first only (lock)</option>
      </select>
      <label class="muted"><input id="bPublic" type="checkbox"> public (browser) submit</label>
      <button id="createBoardBtn">Add board</button>
    </div>
    <div id="boards"></div>

    <h2>Apps &amp; access keys</h2>
    <div class="row">
      <input id="appLabel" placeholder="App label (e.g. my-game-backend)" style="min-width:16rem">
      <button id="createAppBtn">Create app</button>
    </div>
    <div id="apps"></div>
  </section>

<script nonce="__NONCE__">
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var BOARDS = [];

  function el(tag, opts) {
    var n = document.createElement(tag); opts = opts || {};
    for (var k in opts) {
      if (k === 'class') n.className = opts[k];
      else if (k === 'text') n.textContent = opts[k];
      else if (k.indexOf('on') === 0) n.addEventListener(k.slice(2), opts[k]);
      else n.setAttribute(k, opts[k]);
    }
    for (var i = 2; i < arguments.length; i++) { var c = arguments[i]; if (c != null) n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); }
    return n;
  }
  function api(method, path, body) {
    var o = { method: method, credentials: 'same-origin', headers: {} };
    if (body !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
    return fetch(path, o).then(function (r) { return r.json().catch(function () { return null; }).then(function (j) { return { status: r.status, json: j }; }); });
  }
  function show(v) { $('login').classList.toggle('hidden', v !== 'login'); $('app').classList.toggle('hidden', v !== 'app'); }
  function copy(t, b) { navigator.clipboard.writeText(t).then(function () { b.textContent = 'Copied'; }, function () { b.textContent = 'Copy failed'; }); setTimeout(function () { b.textContent = 'Copy'; }, 1500); }
  function boardName(key) { for (var i = 0; i < BOARDS.length; i++) { if (BOARDS[i].key === key) return BOARDS[i].name || '(unnamed)'; } return '(board)'; }

  function doLogin() {
    $('loginErr').textContent = '';
    api('POST', '/admin/login', { password: $('pw').value }).then(function (r) {
      if (r.status === 204) { $('pw').value = ''; return loadAll(); }
      if (r.status === 429) { $('loginErr').textContent = 'Too many attempts. Wait a few minutes.'; return; }
      $('loginErr').textContent = 'Invalid password.';
    });
  }
  function doLogout() { api('POST', '/admin/logout').then(function () { show('login'); }); }

  function loadAll() {
    return api('GET', '/admin/boards').then(function (r) {
      if (r.status === 401) { return show('login'); }
      show('app'); BOARDS = (r.json && r.json.boards) || []; renderBoards(); return loadApps();
    });
  }

  function renderBoards() {
    var wrap = $('boards'); wrap.textContent = '';
    if (!BOARDS.length) { wrap.appendChild(el('p', { class: 'muted', text: 'No boards yet. Create one above.' })); return; }
    BOARDS.forEach(function (b) {
      var row = el('div', { class: 'row between brow' });
      var info = el('div', {});
      info.appendChild(el('span', { text: b.name || '(unnamed)' }));
      info.appendChild(el('span', { class: 'badge', text: b.direction_method }));
      info.appendChild(el('span', { class: 'badge', text: ({ keep_best: 'keep best', overwrite: 'overwrite', first: 'first only' })[b.write_policy] || b.write_policy }));
      info.appendChild(el('span', { class: 'badge', text: b.allow_public_submit ? 'public submit' : 'server-only' }));
      info.appendChild(el('div', { class: 'muted', text: 'key: ' + b.key }));
      row.appendChild(info);
      var entriesBox = el('div', { class: 'keys hidden' });
      var rb = el('div', { class: 'row' });
      rb.appendChild(el('button', { class: 'secondary', text: 'Entries', onclick: function () { toggleEntries(b.key, entriesBox); } }));
      rb.appendChild(el('button', { class: 'secondary', text: 'Copy key', onclick: function (e) { copy(b.key, e.target); } }));
      rb.appendChild(el('button', { class: 'secondary', text: b.allow_public_submit ? 'Make server-only' : 'Make public',
        onclick: function () { toggleBoard(b.key, !b.allow_public_submit); } }));
      rb.appendChild(el('button', { class: 'danger', text: 'Delete', onclick: function () { delBoard(b.key); } }));
      row.appendChild(rb);
      var card = el('div', { class: 'card' });
      card.appendChild(row); card.appendChild(entriesBox);
      wrap.appendChild(card);
    });
  }
  function createBoard() {
    api('POST', '/admin/boards', { name: $('bName').value, direction_method: $('bDir').value, write_policy: $('bPolicy').value, allow_public_submit: $('bPublic').checked }).then(function (r) {
      if (r.status === 201) { $('bName').value = ''; $('bPolicy').value = 'keep_best'; $('bPublic').checked = false; loadAll(); }
    });
  }
  function toggleBoard(key, makePublic) {
    if (makePublic && !confirm('Make this board browser-submittable? Anyone with a submit key can post from any web page. Use a submit-only key.')) return;
    api('PATCH', '/admin/boards/' + encodeURIComponent(key), { allow_public_submit: makePublic }).then(loadAll);
  }

  function toggleEntries(key, box) {
    if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
    box.classList.remove('hidden'); renderEntries(key, box);
  }
  function renderEntries(key, box, after) {
    if (!after) box.textContent = '';
    api('GET', '/leaderboards/' + encodeURIComponent(key) + '/list?count=100' + (after ? '&after=' + encodeURIComponent(after) : '')).then(function (r) {
      var data = r.json || {}; var items = data.items || [];
      if (!after) box.appendChild(entryAddForm(key, box));
      items.forEach(function (it) { box.appendChild(entryRow(key, box, it)); });
      if (!items.length && !after) box.appendChild(el('p', { class: 'muted', text: 'No entries yet.' }));
      var pg = data.pagination || {};
      if (pg.next_cursor) {
        var more = el('button', { class: 'secondary', text: 'Load more', onclick: function () { more.remove(); renderEntries(key, box, pg.next_cursor); } });
        box.appendChild(more);
      }
    });
  }
  function entryRow(key, box, it) {
    var row = el('div', { class: 'row between brow' });
    var info = el('div', {});
    info.appendChild(el('span', { class: 'badge', text: '#' + it.rank }));
    info.appendChild(el('span', { text: ' ' + it.member_id }));
    info.appendChild(el('span', { class: 'badge', text: 'score ' + it.score }));
    if (it.metadata) info.appendChild(el('div', { class: 'muted', text: it.metadata }));
    row.appendChild(info);
    var rb = el('div', { class: 'row' });
    rb.appendChild(el('button', { class: 'secondary', text: 'Edit', onclick: function () { editEntry(key, box, it, row); } }));
    rb.appendChild(el('button', { class: 'danger', text: 'Delete', onclick: function () { delEntry(key, box, it.member_id); } }));
    row.appendChild(rb);
    return row;
  }
  function editEntry(key, box, it, row) {
    row.textContent = '';
    var sc = el('input', { type: 'number', value: String(it.score), style: 'width:7rem' });
    var md = el('input', { type: 'text', value: it.metadata || '', placeholder: 'metadata', style: 'min-width:10rem' });
    var left = el('div', { class: 'row' });
    left.appendChild(el('span', { text: it.member_id })); left.appendChild(sc); left.appendChild(md);
    var right = el('div', { class: 'row' });
    right.appendChild(el('button', { text: 'Save', onclick: function () {
      var v = parseInt(sc.value, 10); if (isNaN(v)) return;
      api('PUT', '/admin/boards/' + encodeURIComponent(key) + '/members/' + encodeURIComponent(it.member_id), { score: v, metadata: md.value }).then(function () { renderEntries(key, box); });
    } }));
    right.appendChild(el('button', { class: 'secondary', text: 'Cancel', onclick: function () { renderEntries(key, box); } }));
    row.appendChild(left); row.appendChild(right);
  }
  function delEntry(key, box, member_id) {
    if (!confirm('Delete ' + member_id + ' from this board?')) return;
    api('DELETE', '/admin/boards/' + encodeURIComponent(key) + '/members/' + encodeURIComponent(member_id)).then(function () { renderEntries(key, box); });
  }
  function entryAddForm(key, box) {
    var f = el('div', { class: 'card' });
    f.appendChild(el('strong', { text: 'Add / set entry' }));
    var mid = el('input', { type: 'text', placeholder: 'member_id', style: 'min-width:10rem' });
    var sc = el('input', { type: 'number', placeholder: 'score', style: 'width:7rem' });
    var md = el('input', { type: 'text', placeholder: 'metadata (optional)', style: 'min-width:10rem' });
    var out = el('div', {});
    var add = el('button', { text: 'Save entry', onclick: function () {
      var v = parseInt(sc.value, 10); out.textContent = '';
      if (!mid.value || isNaN(v)) { out.appendChild(el('p', { class: 'err', text: 'member_id and integer score required.' })); return; }
      api('PUT', '/admin/boards/' + encodeURIComponent(key) + '/members/' + encodeURIComponent(mid.value), { score: v, metadata: md.value }).then(function (r) {
        if (r.status === 200) { renderEntries(key, box); } else { out.appendChild(el('p', { class: 'err', text: 'Save failed.' })); }
      });
    } });
    var rowf = el('div', { class: 'row', style: 'margin-top:.4rem' });
    rowf.appendChild(mid); rowf.appendChild(sc); rowf.appendChild(md); rowf.appendChild(add);
    f.appendChild(rowf); f.appendChild(out);
    return f;
  }
  function delBoard(key) {
    if (!confirm('Delete this board and all its scores? Access keys bound to it are removed too.')) return;
    api('DELETE', '/admin/boards/' + encodeURIComponent(key)).then(loadAll);
  }

  function loadApps() {
    return api('GET', '/admin/apps').then(function (r) {
      var wrap = $('apps'); wrap.textContent = '';
      var apps = (r.json && r.json.apps) || [];
      if (!apps.length) { wrap.appendChild(el('p', { class: 'muted', text: 'No apps yet. Create one above.' })); return; }
      apps.forEach(function (a) { wrap.appendChild(renderApp(a)); });
    });
  }
  function renderApp(a) {
    var card = el('div', { class: 'card' });
    var head = el('div', { class: 'row between' });
    var left = el('div', {});
    left.appendChild(el('strong', { text: a.label || '(no label)' }));
    if (a.revoked) left.appendChild(el('span', { class: 'badge', text: 'revoked' }));
    left.appendChild(el('div', { class: 'muted', text: a.app_id }));
    head.appendChild(left);
    var keysBox = el('div', { class: 'keys hidden' });
    var btns = el('div', { class: 'row' });
    btns.appendChild(el('button', { class: 'secondary', text: 'Keys', onclick: function () { toggleKeys(a.app_id, keysBox); } }));
    if (!a.revoked) btns.appendChild(el('button', { class: 'danger', text: 'Revoke', onclick: function () { revokeApp(a.app_id); } }));
    head.appendChild(btns); card.appendChild(head); card.appendChild(keysBox);
    return card;
  }
  function createApp() { api('POST', '/admin/apps', { label: $('appLabel').value }).then(function (r) { if (r.status === 201) { $('appLabel').value = ''; loadApps(); } }); }
  function revokeApp(id) {
    if (!confirm('Revoke this app? Every access key issued under it stops working immediately.')) return;
    api('DELETE', '/admin/apps/' + encodeURIComponent(id)).then(loadApps);
  }

  function toggleKeys(appId, box) { if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; } box.classList.remove('hidden'); renderKeys(appId, box); }
  function renderKeys(appId, box) {
    box.textContent = '';
    api('GET', '/admin/apps/' + encodeURIComponent(appId) + '/keys').then(function (r) {
      var keys = (r.json && r.json.keys) || [];
      keys.forEach(function (k) {
        var row = el('div', { class: 'row between brow' });
        var info = el('div', {});
        info.appendChild(el('span', { text: boardName(k.board_key) }));
        k.scopes.forEach(function (sc) { info.appendChild(el('span', { class: 'badge', text: sc })); });
        if (k.revoked) info.appendChild(el('span', { class: 'badge', text: 'revoked' }));
        info.appendChild(el('div', { class: 'muted', text: 'board ' + k.board_key + '  \u00b7  ' + k.key_id }));
        row.appendChild(info);
        if (!k.revoked) { var rb = el('div', { class: 'row' }); rb.appendChild(el('button', { class: 'danger', text: 'Revoke key', onclick: function () { revokeKey(k.key_id, appId, box); } })); row.appendChild(rb); }
        box.appendChild(row);
      });
      box.appendChild(issueForm(appId));
    });
  }
  function revokeKey(keyId, appId, box) {
    if (!confirm('Revoke this access key? The secret stops working immediately.')) return;
    api('DELETE', '/admin/keys/' + encodeURIComponent(keyId)).then(function () { renderKeys(appId, box); });
  }
  function issueForm(appId) {
    var f = el('div', { class: 'card' });
    f.appendChild(el('strong', { text: 'Issue access key' }));
    var sel = el('select', {});
    if (!BOARDS.length) sel.appendChild(el('option', { value: '', text: 'create a board first' }));
    BOARDS.forEach(function (b) { sel.appendChild(el('option', { value: b.key, text: (b.name || '(unnamed)') + ' \u2014 ' + b.key })); });
    var subC = el('input', { type: 'checkbox' }); subC.checked = true;
    var subL = el('label', { class: 'muted' }); subL.appendChild(subC); subL.appendChild(document.createTextNode(' submit'));
    var memC = el('input', { type: 'checkbox' });
    var memL = el('label', { class: 'muted' }); memL.appendChild(memC); memL.appendChild(document.createTextNode(' member:delete'));
    var out = el('div', {});
    var btn = el('button', { text: 'Issue key', onclick: function () {
      var scopes = []; if (subC.checked) scopes.push('submit'); if (memC.checked) scopes.push('member:delete');
      out.textContent = '';
      if (!sel.value || !scopes.length) { out.appendChild(el('p', { class: 'err', text: 'Pick a board and at least one right.' })); return; }
      api('POST', '/admin/apps/' + encodeURIComponent(appId) + '/keys', { board_key: sel.value, scopes: scopes }).then(function (r) {
        out.textContent = '';
        if (r.status !== 201) { out.appendChild(el('p', { class: 'err', text: 'Issue failed.' })); return; }
        out.appendChild(el('div', { class: 'muted', text: 'Secret for ' + boardName(r.json.board_key) + ' \u2014 copy now, shown once:' }));
        out.appendChild(el('div', { class: 'secret', text: r.json.secret }));
        out.appendChild(el('button', { text: 'Copy', onclick: function (e) { copy(r.json.secret, e.target); } }));
      });
    }});
    var rowf = el('div', { class: 'row', style: 'margin-top:.4rem' });
    rowf.appendChild(sel); rowf.appendChild(subL); rowf.appendChild(memL); rowf.appendChild(btn);
    f.appendChild(rowf); f.appendChild(out);
    return f;
  }

  $('loginBtn').addEventListener('click', doLogin);
  $('pw').addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });
  $('logoutBtn').addEventListener('click', doLogout);
  $('createBoardBtn').addEventListener('click', createBoard);
  $('createAppBtn').addEventListener('click', createApp);
  loadAll();
})();
</script>
</body>
</html>
"""


def main():
    if not ADMIN_TOKEN:
        raise SystemExit("ADMIN_TOKEN is required (set by the systemd unit from the deploy password)")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    init_db()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"leaderboard listening on :{PORT} (db={DB_PATH})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
