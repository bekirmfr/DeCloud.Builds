# repo-deploy — Deploy from Repository role layer

One-shot deploy from source. The user supplies a repository URL, the port
their app listens on, optional env vars, an optional database, and an
optional deploy key. The VM builds the code and serves it on port 80.

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
- **run**: the status server releases port 80, then
  `docker run -p 80:$APP_PORT -e PORT=$APP_PORT --env-file /etc/decloud/app.env`.
  Compose apps map their own ports and must publish 80 (documented on the
  form). Port 80 is the only exposed port by design — the moment a second
  port is wanted, the answer is Coolify, not port machinery here.

Full build output goes to `/var/log/decloud/build.log` (SSH only). The
status page shows phase names and byte counts, never build output — build
logs routinely echo environment variables, and the page is unauthenticated
by design.

## Status page

`repo-status.service` serves the progress page on port 80 **as HTTP 503**
(honest "not ready" to the platform's port-80 readiness probe; browsers
render it fine) and `/status` as length-capped JSON from
`provision-status.json` (guest-written, display-only, escaped client-side).
`provision.sh` stops the service when the app takes port 80;
`ConditionPathExists=!/var/lib/decloud/provisioned` keeps it from ever
coming back. On a failed `run` phase the ERR trap restarts it so the user
is never staring at connection-refused.

## Updating

1. Edit this role layer.
2. Bump `RepoDeployTemplateRevision` in
   `TenantVmTemplateSeeder.RepoDeploy.cs`.
3. Commit both; the seeder picks it up on next orchestrator restart.

`NIXPACKS_VERSION` is pinned in `provision.sh` — verify the release asset
name when bumping (`nixpacks-vX.Y.Z-x86_64-unknown-linux-musl.tar.gz`).
