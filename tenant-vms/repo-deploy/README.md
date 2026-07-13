# repo-deploy — Deploy from Repository role layer

One-shot deploy from source. The user supplies a repository URL, the port
their app listens on, optional env vars, an optional database, and an
optional deploy key. The VM builds the code, runs it on an internal port,
and serves it at the VM's address on port 80 via an nginx front proxy.

**Positioning.** This template deliberately does not rebuild on push, host a
second app, or ship a dashboard. Coolify is the control plane; this is the
one-shot. The two templates name each other in their descriptions so users
self-select and neither has to grow into the other.

## How user input travels (the injection boundary)

No raw user string is ever substituted into cloud-init YAML. The Deploy from
Repo form (`repo-deploy.js`) shell-quotes and base64-encodes everything the
user types into exactly three declared variables:

| Variable          | Contents                                         | Lands at                        |
|-------------------|--------------------------------------------------|---------------------------------|
| `DEPLOY_CONF_B64` | `SOURCE_URL` / `SOURCE_REF` / `APP_PORT` / `DATABASE`, shell-quoted | `/etc/decloud/deploy.conf` (0600) |
| `APP_ENV_B64`     | `KEY=value` lines for `docker --env-file`        | `/etc/decloud/app.env` (0600)   |
| `DEPLOY_KEY_B64`  | read-only single-repo SSH key (optional)         | `/root/.ssh/decloud_deploy_key` (0600, deleted after clone) |

Each lands in a `write_files` entry with `encoding: b64`, so a newline or
quote in a user value cannot alter the composed document — TemplateComposer's
"one bad write_files entry aborts the whole list" failure mode is unreachable
from user input. The orchestrator never fetches or inspects `SOURCE_URL`;
the clone runs inside the guest (no orchestrator SSRF surface).

The rendered cloud-init is written to the hosting node's disk and is readable
by the node operator. The deploy form says so, next to both the env-var
section and the deploy-key field. That is why private repos are restricted to
deploy keys (blast radius: one repo, read-only, revocable) rather than
OAuth tokens or PATs (blast radius: the whole account).

## Provisioning (contract v1)

`provision.sh` follows base-tenant.yaml's provisioning contract: the systemd
timer is the retry boundary, every phase is stamped, no sleeps, no counters.

Phases: `docker` → `source` → `database` → `build` → `run`.

- **source**: GitHub/GitLab URLs fetch the ref tarball through the library's
  resumable `fetch()` (monotonic convergence on slow links — codeload
  archives resume; `git clone` does not). Other hosts, and all private
  clones, fall back to `git clone` into a `.part` staging dir with an atomic
  `mv` — the acknowledged lucky path. The resolved commit SHA is recorded at
  `/etc/decloud/source-sha` for reproducibility, not integrity: in-guest
  content verification is the tenant's job by design (see the ai-chatbot
  header for the recorded decision — do not "fix" this back).
- **build**: fixed precedence — `Dockerfile` → compose file → Nixpacks
  (pinned release binary via `fetch()`, no pipe-to-shell). Detection is
  delegated, never owned. The override for a wrong guess is "add a
  Dockerfile", not a form field.
- **run**: the app runs on an internal port only —
  `docker run -p 127.0.0.1:3000:$APP_PORT -e PORT=$APP_PORT --env-file /etc/decloud/app.env`.
  It never binds port 80; nginx owns 80 and proxies to `127.0.0.1:3000`
  (see "Front proxy" below). The run phase confirms the app answers on
  3000 before stamping success, and distinguishes "container exited" (fix
  your repo) from "up but silent on the port" (fix `APP_PORT`).
  **Compose apps must publish port 3000**, not 80 — nginx proxies to it.
  Port 80 is the only externally exposed port by design; the moment a
  second app or port is wanted, the answer is Coolify, not port machinery
  here.

Full build output goes to `/var/log/decloud/build.log` (SSH only). The
status page shows phase names and byte counts, never build output — build
logs routinely echo environment variables, and the page is unauthenticated
by design.

## Front proxy (why there is no port-80 handoff)

nginx owns port 80 for the VM's whole life and proxies to the app on
`127.0.0.1:3000`. While the app is down, nginx serves the build-status
page via `error_page 502 503 504 =503 /decloud-provisioning.html`; the
instant the app answers on 3000, nginx proxies straight through to it.
There is no stop, no handoff, no second binder of port 80.

This replaced a v1 design where a status server bound 80 directly and was
stopped to "hand off" the port to the app. That handoff was an unfixable
race: the status server could respawn between the port check and the app's
bind, and once any `docker run -p 80` failed mid-network-setup, dockerd
held a stale host-port reservation that `docker rm` did not release and
`ss` could not see — poisoning every retry until `systemctl restart
docker`. Because nothing but nginx now binds 80, and the app binds only
`127.0.0.1:3000`, both failure modes are structurally impossible rather
than merely handled.

- `/` → the app, with the provisioning page shown on 502/503/504.
- `/health` → **raw** passthrough to the app, no `error_page` fallback, so
  the platform's Port-80 readiness probe observes the real upstream state
  (502 until the app is up). Do not add a fallback to this location.
- `/decloud-status` → length-capped JSON from `provision-status.json`
  (guest-written, display-only; the status page escapes it client-side and
  inserts via `textContent`, never `innerHTML`).

## Updating

1. Edit this role layer.
2. Bump `RepoDeployTemplateRevision` in
   `TenantVmTemplateSeeder.RepoDeploy.cs`.
3. Commit both; the seeder picks it up on next orchestrator restart.

`NIXPACKS_VERSION` is pinned in `provision.sh` — verify the release asset
name when bumping (`nixpacks-vX.Y.Z-x86_64-unknown-linux-musl.tar.gz`).