# Generic Leaderboard — VM Template

A self-hosted, multi-tenant leaderboard backend for the DeCloud marketplace. One
VM hosts many **apps**; each app owns many **boards**; each board ranks many
**members**. The HTTP API mirrors LootLocker's server leaderboard API.

Integrated as a **compose-pipeline tenant template inside `TemplateSeederService`**,
following the exact pattern of the existing ai-chatbot / minecraft / coolify
templates — composed on `base-tenant.yaml`, with the authored service embedded
inline in the role layer's `write_files` (no DeCloud artifact pipeline, which the
compose templates don't use).

## Files

| File | What it is | Where it goes |
|---|---|---|
| `TemplateSeederService.cs` | Your uploaded file **with the four edits applied** | Replace `Orchestrator/Services/TemplateSeederService.cs` |
| `leaderboard-cloud-init.yaml` | Role layer — workload only; service embedded inline | `DeCloud.Builds/tenant-vms/leaderboard/cloud-init.yaml` |
| `leaderboard-api.py` | The service (Python **stdlib only**), readable source | Embedded into the role layer above; keep as the editable copy |
| `README.md` | This file | — |

There is **no** separate seeder class, no `*.Artifacts.cs`, and no
`InlineArtifactFactory` — those were removed. The compose-tenant templates in
`TemplateSeederService` carry zero artifacts; they embed authored scripts inline
(minecraft's `setup.sh`) and curl third-party installers from upstream. This
service is ours, so it ships inline in the layer.

## The four edits applied to TemplateSeederService.cs

1. **Role URL constant** (next to `CoolifyRoleUrl`):
   ```csharp
   private const string LeaderboardRoleUrl =
       $"{CloudInitRawBase}/tenant-vms/leaderboard/cloud-init.yaml";
   ```
2. **Revision constant** (next to `CoolifyTemplateRevision`):
   ```csharp
   private const int LeaderboardTemplateRevision = 1;
   ```
3. **Seed call** (in `SeedComposeTenantTemplatesAsync`, after coolify):
   ```csharp
   await TryUpsertComposeAsync("leaderboard",
       () => BuildLeaderboardTemplateAsync(ct), ct);
   ```
4. **`BuildLeaderboardTemplateAsync` + `BuildLeaderboardVariables`** (after the
   coolify methods), mirroring `BuildCoolifyTemplateAsync`: fetch base + role,
   `TemplateComposer.Compose`, return the `VmTemplate`, declare the statics.

No constructor, DI, or `SeedAsync` changes are needed — the compose templates all
run through the existing `SeedComposeTenantTemplatesAsync` failure domain and the
already-injected `_httpClient`.

## What base-tenant provides vs. what the role layer adds

base-tenant owns hostname, root password (`__ADMIN_PASSWORD__`), SSH password auth,
the sshd CA, `qemu-guest-agent`, curl/jq/openssl, the orchestrator-url file, and
`final_message`. The role layer adds only: `python3`, the embedded
`leaderboard-api.py`, the `EnvironmentFile` (`ADMIN_TOKEN=__ADMIN_PASSWORD__`), the
hardened systemd unit, and a `/etc/motd`. `TemplateComposer` merges them
(write_files/runcmd concatenated base-first, packages unioned, scalars role-wins).

`BuildLeaderboardVariables()` declares exactly the placeholders in the composed
document — the base-tenant set plus `__DECLOUD_DOMAIN__` (resolved by
`DeCloudDomainResolver`) — so `CloudInitValidator` sees no drift.

## Trust boundary

Authenticates the **deployer's server**, not end users. `member_id`/name are
whatever the caller passes; verifying a portal player token (CrazyGames / Poki /
Yandex) is your backend's job. Guarantees **authenticated, persisted, ranked** —
never that a score is legitimate. **Submit from your server, not a game client**
(a client that embeds the app secret leaks it).

## Auth model

| Role | Credential | Header | Can do |
|---|---|---|---|
| Operator | deploy root password | `x-admin-token` | mint / list / revoke apps |
| App | `app_secret` (shown once) | `x-session-token` | create boards, submit, manage **its own** boards |
| Public | board key only | — | read rankings |

256-bit secrets, stored only as SHA-256 hashes; admin token compared with
`hmac.compare_digest`. `UseGeneratedPassword = true` guarantees the root password
(and thus the admin token) is non-empty.

## Endpoints

```
# Operator (x-admin-token)
POST   /admin/apps          {"label":"my-game"}  -> {app_id, app_secret}
GET    /admin/apps
DELETE /admin/apps/{app_id}
# App (x-session-token: <app_secret>)
POST   /leaderboards        {"name","direction_method","overwrite_score_on_submit"}
POST   /leaderboards/{key}/submit   {"member_id","score","metadata"}
DELETE /leaderboards/{key}
DELETE /leaderboards/{key}/members/{member_id}
# Public (board key only)
GET    /leaderboards/{key}/list?count=10&after=<cursor>
GET    /leaderboards/{key}/member/{member_id}?around=3
GET    /health
```

## SDK mapping

| Concept | This service | LootLocker | Playgama Bridge |
|---|---|---|---|
| Submit | `POST /leaderboards/{key}/submit` `{member_id, score, metadata}` | `submit` `{member_id, score, metadata}` | `setScore(id, score)` |
| Page of ranks | `GET .../list?count&after` | Get Score List | `getEntries(id)` |
| Member rank | `GET .../member/{member_id}` | Get Member Rank | — |
| Around me (±N) | `GET .../member/{member_id}?around=3` | (compose list + member) | — |
| Keep best / overwrite | `overwrite_score_on_submit` | `overwrite_score_on_submit` | platform default |

## Notes

- **Validation:** `leaderboard-api.py` was run end-to-end — app minting, 401 on bad
  admin token, board creation, keep-best (lower resubmit rejected, higher
  promotes), top list + pagination, the `?around=N` window, the multi-tenant
  isolation invariant (app B → app A's board = 404), unknown-member 404. The role
  layer parses as valid cloud-init; the embedded script has no `__UPPERCASE__`
  dunders or `${...}` tokens, so the renderer leaves it untouched.
- **Editing the service:** edit `leaderboard-api.py`, then re-embed it into the
  role layer's first `write_files` block and bump `LeaderboardTemplateRevision`.
- **Size:** the embedded service is ~20 KB. That is within the compose-tenant
  norm (minecraft embeds a multi-file setup), but it is larger than the others —
  if cloud-init size ever becomes a concern, the artifact-cache path (used by
  the system VMs and the general template) is the alternative.
- **Verified flag:** ships `IsVerified = false` (like the coolify migration);
  flip to `true` after field-validation gates pass.
