# Generic Leaderboard — VM Template

A self-hosted leaderboard backend for the DeCloud marketplace. One VM is one
project: it hosts project-wide **boards** (each ranks many **members**),
label-only **apps**, and per-board **access keys**. The HTTP API mirrors
LootLocker's server leaderboard API, so a game using LootLocker or a portal SDK
(Playgama, CrazyGames, Poki) integrates with a thin adapter.

Each board's `write_policy` decides what a repeat submission does: `keep_best`
(default), `overwrite` (latest wins), or `first` (lock to the first submission and
ignore later ones — useful for daily challenges).

## Architecture

The service ships as a real **DeCloud artifact** (`leaderboard`), not embedded
inline. The role layer fetches it at boot via `${ARTIFACT_URL:leaderboard}` and
verifies it against `${ARTIFACT_SHA256:leaderboard}` before running it. The
template is composed on `base-tenant.yaml` and seeded by **`TenantVmTemplateSeeder`**
through the existing compose path (`TryUpsertComposeAsync` →
`UpsertComposeTemplateAsync`), in the same failure domain as the
ai-chatbot / minecraft / coolify templates and using the already-injected
`_httpClient`.

Upsert is revision-gated: `UpsertComposeTemplateAsync` skips when
`existing.Revision >= template.Revision`. Any change to the service or template
must bump `LeaderboardTemplateRevision` (currently **2**) or the orchestrator
keeps serving the old revision.

## Files

| File | What it is | Where it goes |
|---|---|---|
| `cloud-init.yaml` | Role layer — workload only; fetches + verifies the artifact, installs the hardened systemd unit | `DeCloud.Builds/tenant-vms/leaderboard/cloud-init.yaml` |
| `assets/leaderboard-api.py` | The service (Python **stdlib only**), the editable source that becomes the `leaderboard` artifact | built into `LeaderboardApiPy{Sha256,DataUri}` by the artifact builder |
| `README.md` | This file | — |

## What base-tenant provides vs. what the role layer adds

base-tenant owns hostname, root password (`__ADMIN_PASSWORD__`), SSH password
auth, the sshd CA, `qemu-guest-agent`, curl/jq/openssl, the orchestrator-url
file, and `final_message`. The role layer adds only: `python3`, the fetched +
sha-verified `leaderboard-api.py`, the `EnvironmentFile`
(`ADMIN_TOKEN=__ADMIN_PASSWORD__`), a hardened `systemd` unit running as an
unprivileged `leaderboard` user, and a `/etc/motd`. `TemplateComposer` merges
them (write_files/runcmd concatenated base-first, packages unioned, scalars
role-wins).

## Admin console

`GET /` serves a browser console. The operator signs in with the VM's root
(deploy) password, which is exchanged once at `POST /admin/login` for a
short-lived `HttpOnly; Secure; SameSite=Strict` session cookie — the password is
never resent and the cookie is unreadable to JS. The console manages boards,
apps, and access keys; lets the operator browse, edit, add, and delete board
entries (the edit bypasses keep-best); and includes a "How to use" panel. Admin endpoints accept
either the session cookie or the `x-admin-token` header (for curl /
server-to-server). Cookie-authenticated mutations require a same-origin
Origin/Referer (CSRF defense); header-authenticated calls skip that check.

## Trust boundary

Authenticates the **deployer's server**, not end users. `member_id`/name are
whatever the caller passes; verifying a portal player token (CrazyGames / Poki /
Yandex) is your backend's job. Guarantees **authenticated, persisted, ranked** —
never that a score is legitimate. By default **submit from your server, not a game
client** (a client that embeds an access-key secret leaks it). A board can opt
into public browser submit — see "Submit policy" below — which trades that
integrity for convenience on casual boards.

## Auth model

Boards are decoupled from apps. An **app** is just a label and holds no secret.
An **access key** is the write credential: a secret bound to exactly one board
under one app, carrying only the rights the operator grants.

| Role | Credential | Header | Can do |
|---|---|---|---|
| Operator | deploy root password (or console session) | `x-admin-token` | create/delete boards; create/revoke apps; issue/revoke access keys |
| Access key | per-board secret (shown once) | `x-session-token` | act on its **one** board: `submit` (default), optionally `member:delete` |
| Public | board key only | — | read rankings |

Grantable key scopes are `submit` and `member:delete` only; board lifecycle is
operator-only and deliberately not grantable (a writer must not be able to
delete the shared board). Enforcement, in order on every key-authenticated
write: resolve the key (it and its parent app must be un-revoked) → the board in
the URL must equal the key's board (else 404, no existence leak) → the route's
required scope must be in the key's scopes (else 403). Revoking an app disables
all keys issued under it; deleting a board cascades away its access keys and
scores.

256-bit secrets are stored only as SHA-256 hashes; the admin token is compared
with `hmac.compare_digest`. `UseGeneratedPassword = true` guarantees the root
password (and thus the admin token) is non-empty.

## Endpoints

```
# Operator (x-admin-token, or console session cookie)
POST   /admin/apps                         {"label":"my-game"}  -> {app_id, label}
GET    /admin/apps
DELETE /admin/apps/{app_id}
GET    /admin/boards
POST   /admin/boards                       {"name","direction_method","write_policy","allow_public_submit"}
PATCH  /admin/boards/{key}                  {"allow_public_submit": true|false}   (toggle CORS mode)
DELETE /admin/boards/{key}
DELETE /admin/boards/{key}/members/{member_id}
PUT    /admin/boards/{key}/members/{member_id}   {"score","metadata"}   (operator set/correct; bypasses keep-best)
GET    /admin/apps/{app_id}/keys
POST   /admin/apps/{app_id}/keys           {"board_key","scopes":["submit"]}  -> {key_id, secret}
DELETE /admin/keys/{key_id}
# Access key (x-session-token: <key secret>) — acts on the key's bound board
POST   /leaderboards/{key}/submit          {"member_id","score","metadata"}   (scope: submit)
DELETE /leaderboards/{key}/members/{member_id}                                (scope: member:delete)
# Public (board key only)
GET    /leaderboards/{key}/list?count=10&after=<cursor>
GET    /leaderboards/{key}/member/{member_id}?around=3
GET    /health
```

## Submit policy (CORS)

Each board declares whether browsers may submit, via `allow_public_submit`
(default `false`):

- **cors-default** (`false`): `POST /submit` is server-to-server only. The CORS
  preflight (`OPTIONS`) is refused and responses carry no
  `Access-Control-Allow-Origin`, so browser writes are blocked. This is the
  secure default; keep it when score integrity matters.
- **cors-public** (`true`): the preflight succeeds and submit responses carry
  `Access-Control-Allow-Origin: *`, so a browser game with no backend can post
  directly. The submit key then lives in the client — use a **submit-only** key,
  and accept that anyone can post scores to that board.

Toggle a board between modes without losing scores via
`PATCH /admin/boards/{key}`. Public reads are always cross-origin; `member:delete`,
admin, and key endpoints are never browser-accessible on either mode.

## SDK mapping

| Concept | This service | LootLocker | Playgama Bridge |
|---|---|---|---|
| Submit | `POST /leaderboards/{key}/submit` `{member_id, score, metadata}` | `submit` `{member_id, score, metadata}` | `setScore(id, score)` |
| Page of ranks | `GET .../list?count&after` | Get Score List | `getEntries(id)` |
| Member rank | `GET .../member/{member_id}` | Get Member Rank | — |
| Around me (±N) | `GET .../member/{member_id}?around=3` | (compose list + member) | — |
| Remove a member | `DELETE /leaderboards/{key}/members/{member_id}` | Delete Score | — |
| Update policy | `write_policy` (keep_best/overwrite/first) | `overwrite_score_on_submit` | platform default |

## Notes

- **Validation:** `leaderboard-api.py` is boot-tested end-to-end — login
  (good / bad / throttle-lockout), session-cookie flags, board create/list/delete,
  app create (returns no secret), key issuance (default scopes → `["submit"]`),
  submit to the bound board, the key→board binding (same key on another board →
  404), scope gating (submit-only `member:delete` → 403; two-scope key → 204),
  unknown scope → 400, key for missing board/app → 404, key + app revocation,
  board-delete cascade, public reads, and CSRF cross-origin rejection. The
  v1→v2 data migration is tested to preserve boards and scores while dropping the
  legacy `boards.app_id` / `apps.secret_hash` columns.
- **Editing the service:** edit `assets/leaderboard-api.py`, regenerate
  `LeaderboardApiPy{Sha256,DataUri}` with the artifact builder, and bump
  `LeaderboardTemplateRevision`. If the constant is not regenerated, the role
  layer's `${ARTIFACT_SHA256:leaderboard}` verification rejects the fetched file
  at boot.
- **Verified flag:** flip `IsVerified` to `true` only after field-validation
  gates pass.
