# DeCloud.Builds

Build pipeline and source for DeCloud system VM components.

## Structure
system-vms/
dht/                  DHT node — scripts, dashboard, Go binary source
dht-node-src/       Go source (main.go, go.mod)
blockstore/           BlockStore node — scripts, dashboard, Go binary source
blockstore-node-src/ Go source
relay/                Relay node — scripts, dashboard (no compiled binary)
general/              General VM — API, index
shared/               Scripts shared across all system VMs
cloud-init/           Cloud-init YAML templates for all VM roles
.github/
workflows/
release-binaries.yml  Builds and releases dht-node and blockstore-node binaries

## Releasing binaries

```bash
git tag binaries/v1.0.0
git push origin binaries/v1.0.0
```

The workflow builds `dht-node` and `blockstore-node` for `amd64` and `arm64`,
computes SHA256 for each, and publishes a GitHub Release with all assets.
SHA256 values and asset URLs are listed in the release notes — copy them
into `TemplateSeederService` in `DeCloud.Orchestrator` for P10.
