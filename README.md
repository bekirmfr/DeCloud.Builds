# DeCloud.Builds

Build pipeline and deployment artifacts for DeCloud system VM components.

## Structure

```
system-vms/
  compute-artifact-constants.sh   # Generates C# constants for TemplateSeederService
  dht/
    src/                          # Go source → compiled dht-node binary
      main.go
      go.mod
      go.sum
    assets/                       # Deployed as-is → inline data: URI artifacts
      dht-health-check.sh
      dht-notify-ready.sh
      dht-bootstrap-poll.sh
      dht-dashboard.py
      dashboard.html
      dashboard.css
      dashboard.js
    cloud-init.yaml               # Cloud-init template for DHT VMs
  blockstore/
    src/                          # Go source → compiled blockstore-node binary
      main.go
      go.mod
      go.sum
    assets/
      blockstore-health-check.sh
      blockstore-notify-ready.sh
      blockstore-bootstrap-poll.sh
      blockstore-dashboard.py
      dashboard.html
      dashboard.css
      dashboard.js
    cloud-init.yaml
  relay/
    assets/                       # No compiled binary — WireGuard is a kernel module
      relay-api.py
      relay-http-proxy.py
      notify-nat-ready.sh
      dashboard.html
      dashboard.css
      dashboard.js
    cloud-init.yaml
  shared/
    assets/                       # Scripts shared across multiple roles
      wg-mesh-enroll.sh
      wg-config-fetch.sh
.github/
  workflows/
    release-binaries.yml          # Builds and releases dht-node and blockstore-node
```

**`src/`** — Go source compiled by CI into raw ELF binaries. Published to GitHub
Releases as HTTPS artifacts. The node agent fetches these at deploy time via
`ArtifactCacheService`.

**`assets/`** — Scripts, dashboards, and other files deployed as-is. Registered
in `TemplateSeederService` as inline `data:` URI artifacts (base64-encoded,
stored in the template MongoDB document). The node agent serves these to VMs
from its local artifact cache.

**`cloud-init.yaml`** — The cloud-init template for each role. References both
binary and script artifacts via `${ARTIFACT_URL:name}` substitution variables.
The static content (systemd units, nginx config, env files) is embedded inline.

---

## Releasing binaries

Tag format: `binaries/vMAJOR.MINOR.PATCH`

```bash
git tag binaries/v1.0.0
git push origin binaries/v1.0.0
```

The workflow builds `dht-node` and `blockstore-node` for `amd64` and `arm64`,
computes SHA256 for each, and publishes a GitHub Release with eight assets.
The release notes table contains the SHA256 values and asset URLs — copy them
into `SystemVmTemplateSeeder.cs` in `DeCloud.Orchestrator` and bump the
affected `TemplateRevision` constant.

---

## Updating script artifacts

When a script or dashboard file in `assets/` changes:

```bash
cd system-vms
bash compute-artifact-constants.sh
```

This generates `artifact-constants.cs` containing updated SHA256 and
`data:` URI constants for every file under every `assets/` directory.
Copy the changed constants into `SystemVmTemplateSeeder.cs`, bump the
affected `TemplateRevision`, and commit to `DeCloud.Orchestrator`.

---

## Artifact pipeline

All system VM artifacts flow through the same unified pipeline used by
community marketplace templates:

| Artifact type | Storage | Delivery |
|---------------|---------|----------|
| Compiled binary (`dht-node`, `blockstore-node`) | GitHub Release asset (HTTPS URL) | `ArtifactCacheService` fetches from URL, caches locally, serves to VM over virbr0 |
| Script / dashboard (`assets/`) | Inline `data:` URI in template document | `ArtifactCacheService` decodes inline, caches locally, serves to VM over virbr0 |
| Static config (systemd units, nginx, env files) | Embedded in `cloud-init.yaml` | Written by cloud-init at boot via `write_files` |

The platform stores only metadata for external artifacts (URL + SHA256) and
inline bytes for `data:` artifacts — it never serves binary content directly.

## Placeholder syntax conventions

- `__VARNAME__` — substituted at orchestrator render time inside cloud-init
  bodies (the orchestrator's CloudInitRenderer is the authority).
- `{{VARNAME}}` — substituted at consumer render time by the serving process
  inside the VM. Used for per-VM content in artifact files (HTML, JS, etc.)
  that ship as content-addressed bytes through the artifact pipeline.

The two layers are independent and never overlap on the same token.