# Coolify tenant VM template

Self-hosted PaaS deployed as a single-tenant VM. Browser traffic enters
through the platform's CentralIngress (Caddy on the orchestrator), tunnels
to the node's NodeAgent, and is routed by Coolify's in-VM Traefik to the
right app container.

This document records *why* the cloud-init looks the way it does. It is the
companion to `cloud-init.yaml` — read it first when something in there seems
arbitrary.

## Files in this directory

- `cloud-init.yaml` — the composed template applied to fresh VMs. Read by
  `TemplateComposer` which prepends `base-tenant.yaml`. Do not edit the
  composed copy in MongoDB; edit this file and the seeder will upsert on
  the next orchestrator restart (controlled by `CoolifyTemplateRevision`
  in `TemplateSeederService`).
- `README.md` — this file.

## What the template actually does

Beyond running Coolify's stock installer, the cloud-init carries five
non-obvious pieces of glue. Each exists to bridge a specific gap between
how Coolify expects to be deployed (own public IP, own TLS, own port
control) and how DeCloud actually delivers traffic (CGNAT VM, TLS
terminated at the orchestrator, only ports 22/80/443 reachable).

### 1. `coolify-realtime.conf` — nginx WebSocket split

Coolify exposes three host ports natively: `:8000` (dashboard nginx +
Laravel), `:6001` (Reverb realtime), `:6002` (terminal). The browser
connects to all three directly in stock deployments. When CentralIngress
fronts Coolify on a single subdomain everything must arrive on `:8000`,
so nginx needs location blocks that split `/app/*` to `:6001` and
`/terminal/ws` to `:6002`. Without this the dashboard shows
"realtime service connection error" and the in-browser terminal never
connects. Dropped into `/etc/nginx/server-opts.d/` inside the coolify
container on every container start by `coolify-fix-hosts.sh`.

### 2. `decloud-traefik-proxy.py` — file-provider router generator

Coolify's Traefik auto-generates a `redirect-to-https` middleware that
fires unconditionally on its `http` entrypoint, ignoring
`X-Forwarded-Proto`. Many app frameworks (PHP, Rails) also redirect
internally when `$_SERVER['HTTPS']` is unset. In our topology TLS is
already terminated at the orchestrator, so we cannot let Traefik or the
app redirect again.

The generator scans all running app containers for Traefik docker-provider
labels (`traefik.http.routers.http-X.rule`), and for each one emits a
**file-provider** router with `priority = 2000 + len(rule)` and a
`decloud-https-headers` middleware that injects `X-Forwarded-Proto: https`
and `X-Forwarded-Ssl: on`. Traefik provider isolation prevents us from
overriding the `redirect-to-https@docker` middleware from the file
provider; a higher-priority router that bypasses it is the cleanest
workaround.

Three subtleties baked into the generator:

- **Upstream port resolution** has three sources, in this order:
  (a) `traefik.http.services.<router>.loadbalancer.server.port` label —
  what Coolify emits for plain domains; (b) the container image's single
  `Config.ExposedPorts` entry — Coolify *drops* the port label when a
  domain has a path suffix (`https://host/api`); (c) `80` as last resort.
- **Rule-length priority** (`2000 + len(rule)`): two requirements at
  once — beat all docker-provider routers (their default priority is the
  bare rule length, well under 2000) and make more specific rules win
  among ours so `PathPrefix(/api)` outranks `PathPrefix(/)` deterministically.
- **Pure Python, no shell heredocs.** The script is embedded in
  `cloud-init.yaml` under `write_files: content: |`. cloud-init's YAML
  parser treats a column-0 heredoc body inside a block scalar as a new
  YAML mapping key and rejects the entire user-data. Keeping the
  generator in Python lets every line stay properly indented under the
  block scalar.

Triggered on every Docker container start/die event by
`decloud-traefik-proxy.service`, so new Coolify deployments are covered
automatically without operator action.

### 3. `coolify-fix-hosts.sh` — in-container patches

Two patches applied on every Coolify container start event:

- **`host.docker.internal` repoint** — Coolify's internal SSH-to-self
  flow needs the host's sshd reachable from inside containers. Re-points
  to the Coolify Docker network gateway, which Coolify itself doesn't do.
- **nginx `server-opts.d` include** — Coolify's `http.conf.template`
  carries an `include /etc/nginx/server-opts.d/*.conf;` directive, but
  the rendered `http.conf` shipped in the image is missing it, so the
  `coolify-realtime.conf` from §1 wouldn't load. The patch appends the
  include line (idempotent) and reloads nginx.

Neither patch survives Coolify upgrades — `upgrade.sh` redownloads
compose files and recreates containers — so the patch is event-driven,
not one-shot.

### 4. `coolify-setup.sh` — first-boot orchestration

Beyond invoking Coolify's installer, this script does three things the
installer doesn't:

- **APP_URL + Reverb env override.** Coolify's installer writes
  `/data/coolify/source/.env` with empty `APP_URL`/`PUSHER_HOST`/
  `PUSHER_PORT`/`PUSHER_SCHEME`. With those empty, Laravel falls back to
  `http://localhost` and the browser-side Pusher client tries
  `wss://localhost:6001` — i.e. the user's own machine. Livewire waits
  forever for the handshake and the dashboard renders as a perpetual
  black loading screen. We write the right values and restart Coolify.
- **Seed-wait gate.** Coolify's installer kicks off migrations and the
  `ProductionSeeder` asynchronously after the coolify container starts.
  Restarting the container mid-seed aborts it; the
  `instance_settings` row is never created; every subsequent route
  including `/login` 500s with `No query results for model
  [InstanceSettings]`, surfaced through nginx as a 404 error page. The
  gate polls `InstanceSettings::count()` until ≥1 before restarting.
  Bounded to ~5 minutes with an explicit `migrate --force` +
  `db:seed --class=ProductionSeeder --force` fallback (idempotent — the
  seeders use `firstOrCreate`).
- **`docker restart` over `docker compose restart`.** Coolify's compose
  file carries a legacy `soketi` service block as a backward-compatibility
  alias (`container_name: coolify-realtime`) with no `image`, `build`, or
  `profile`. Docker Compose v2 strict validation rejects it; `docker
  restart` operates by container name without touching the compose file
  so it's unaffected.

### 5. sshd drop-in for Docker bridge subnets

The base tenant template sets `TrustedUserCAKeys` + `AuthorizedPrincipalsFile`
(the DeCloud SSH CA), which blocks plain pubkey auth — any key not signed
by the CA is rejected. Coolify generates its own SSH keypair to manage
"this server" and that key is not CA-signed. The drop-in (in
`/etc/ssh/sshd_config.d/98-coolify-localhost.conf`) re-allows plain
pubkey auth for connections originating from `10.0.0.0/8` (any Docker
bridge subnet); the CA-based auth applies to everything else.

## Canonical deployment flow for multi-service apps

The template's `final_message` and the marketplace `LongDescription`
already cover this for end users. Restated here for maintainers, because
several of the workarounds above only make sense in context.

Hostnames are user-chosen and appear in three places (DNS, DeCloud,
Coolify). Ports are app-chosen and appear *nowhere* in user input — the
generator derives them from container labels or `ExposedPorts` and the
file-provider router does the translation invisibly.

1. DNS: `*.myapp.example.com  CNAME  vms.stackfi.tech` plus
   `myapp.example.com  CNAME  vms.stackfi.tech` (the wildcard does not
   match the bare apex).
2. DeCloud → Custom Domains: add each hostname a browser will visit,
   target port **80** (Coolify's Traefik entrypoint), Verify DNS.
3. Coolify → each service's domain field, no port suffix:
   `https://myapp.example.com` for frontend,
   `https://api.myapp.example.com` (or `https://myapp.example.com/api`
   if the app's routes are path-namespaced) for backend.
4. Internal services (databases, caches, server-side object storage):
   nothing — containers reach them by compose service name on the
   internal Docker network.

Coolify's auto-generated `*.sslip.io` URLs for compose services point at
the raw node IP and are never reachable on DeCloud. Users must ignore
them. This is the largest UX friction in the integration; see Roadmap.

## Known caveats

These are intentional trade-offs in the current implementation, not bugs.
Each will go away when the corresponding roadmap item lands.

### Orchestrator-terminated TLS doesn't match Coolify's mental model

Coolify's UI was designed assuming Coolify itself is the public TLS edge.
That assumption surfaces as several pieces of UX friction:

- The "Required Port: 8201" warning when assigning a domain to a service.
  Coolify wants to publish that port on its Traefik entrypoint. On DeCloud
  it can't — only 80/443 arrive at the VM. Users have to read the
  parenthetical "or any other port if you know what you're doing" as
  permission to ignore the warning.
- Auto-generated `*.sslip.io` URLs for compose services. They look real
  and clickable but reach nothing.
- The `redirect-to-https` middleware fights `X-Forwarded-Proto`. We work
  around this with the file-provider router (§2 above), but the existence
  of that workaround is itself friction.
- `loadbalancer.server.port` is dropped from labels when a domain has a
  path suffix. The generator has to fall back to `ExposedPorts`.
- Coolify's compose templates sometimes interpolate
  `NUXT_PUBLIC_BASE_API: ${SERVICE_URL_BACKEND}` directly, which works
  if Coolify is the edge (no path prefix) but doubles `/api` when a
  service's domain includes one. Users have to override via "Edit
  Compose File."

None of these break the integration — every browser-facing service can
be made to work — but the user has to learn the platform's edge model
before deploying any non-trivial app. The platform's `final_message` and
marketplace docs teach this, but it's still attention tax.

### Per-VM cert sharing

All Coolify VMs on the platform sit behind one wildcard cert
(`*.vms.stackfi.tech`) plus per-custom-domain on-demand certs issued by
the orchestrator's Caddy. For tenants who need separate cert ownership
(compliance, audit trails), this is the wrong shape — they'd want their
own cert per VM. Today there's no way to opt out.

### Coolify upgrade resilience

`coolify-fix-hosts.sh` and `decloud-traefik-proxy.py` are armed by
systemd unit files watching `docker events`. This survives Coolify
upgrades that recreate containers but does *not* survive Coolify
upgrades that change the **shape** of what we patch. If Coolify reworks
its nginx config layout, or renames its proxy container, the patch
scripts silently no-op and the user gets a broken dashboard with no
DeCloud-side error. There's no version pinning on Coolify itself —
`install.sh` pulls latest.

Mitigation today is monitoring; the template revision (`TemplateSeederService.CoolifyTemplateRevision`) is bumped whenever the
patch needs updating for a new Coolify release.

## Roadmap

### Template-declared TLS posture (resolves §"Orchestrator-terminated TLS")

The current platform terminates TLS at CentralIngress for every template
unconditionally. A planned `ingress.mode` field on the template manifest
will let templates declare which side handles TLS:

```yaml
ingress:
  mode: http        # orchestrator terminates TLS (current default)
  # OR
  mode: passthrough # VM terminates TLS; orchestrator SNI-routes TCP on :443
```

`mode: passthrough` drives three changes automatically:

- Caddy uses its `layer4` app to SNI-route TCP on :443 to the VM
  unchanged, no cert at the edge.
- NodeAgent's port allowlist permits 443 for this VM.
- No cert automation runs at the orchestrator for this VM's hostnames.

Under this model the Coolify template would switch to `passthrough` and
all current workarounds dissolve in one stroke:

- The "Required Port: 8201" warning becomes correct (Coolify *does*
  publish on 443).
- `sslip.io` URLs become reachable (Coolify's Traefik directly fronts
  the public IP).
- The `redirect-to-https` middleware is right — no file-provider router
  needed.
- `loadbalancer.server.port` is right — no `ExposedPorts` fallback
  needed.
- Per-VM cert ownership is automatic — each VM provisions its own
  Let's Encrypt cert through Coolify's existing flow.

The bridge code in this template (`decloud-traefik-proxy.py`, the file-
provider router middleware) stays useful for `mode: http` templates and
goes unused for Coolify under `mode: passthrough`.

### Coolify-API integration (smaller win, useful before passthrough lands)

While we still run Coolify in `mode: http`, a DeCloud→Coolify API
integration could push the wildcard domain setting and a few sensible
env defaults to Coolify at install time. This would eliminate the
sslip.io URL confusion without changing routing. It is a near-term
mitigation, not a substitute for the passthrough mode.

### Version pinning for Coolify (resolves §"Coolify upgrade resilience")

`coolify-setup.sh` currently does `curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash` — latest. The shape we patch could change
between runs. Pinning to a known-good Coolify release (with the
patches validated against it) and bumping the pin deliberately when we
re-validate is the cleanest fix. Implementation is one extra env var
passed to the upstream installer.

## Changing this template

When you edit `cloud-init.yaml`:

1. Validate the YAML parses (cloud-init silently discards user-data with
   a single bad line). The embedded Python script also has to compile —
   if you put a heredoc inside a `content: |` block scalar, cloud-init
   will reject the whole file. There is a section comment at the top of
   `decloud-traefik-proxy.py` repeating this rule.
2. Bump `TemplateSeederService.CoolifyTemplateRevision` so the seeder
   upserts the new template on the next orchestrator restart. Existing
   VMs are not retroactively patched; the change only affects newly
   provisioned VMs.
3. If the change touches the patch surface (Coolify's nginx config
   layout, Traefik label format, container names), re-test against a
   fresh VM end-to-end: first-login, multi-service compose deploy with
   path-routed backend, custom domain verify, in-browser terminal,
   realtime dashboard updates, file upload through an object store
   service if the app has one. The full test matrix is in
   `PROJECT_MEMORY.md`.

## Related files in the broader codebase

- `DeCloud.Orchestrator/src/Orchestrator/Services/TemplateSeederService.cs` —
  `BuildCoolifyTemplateAsync`. Marketplace metadata, `ExposedPorts`
  declarations (what NodeAgent permits per-VM), revision constant.
- `DeCloud.Orchestrator/src/Orchestrator/Services/CentralCaddyManager.cs` —
  the edge that terminates TLS for hostnames routed here. On-demand
  TLS permission endpoint reads the orchestrator's `_customDomains`
  cache.
- `DeCloud.Orchestrator/src/Orchestrator/Services/CentralIngressService.cs` —
  domain registration, DNS proof-of-control verification, lifecycle
  hooks that keep Caddy routes installed across transient VM/node
  outages.
- `DeCloud.NodeAgent/.../GenericProxyController.cs` — the per-VM port
  allowlist (`IsPortAllowed`) the orchestrator's Caddy routes terminate
  against.
