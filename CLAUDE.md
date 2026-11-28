# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Flux v2 GitOps state for a 3-node k0s homelab cluster (`k0s-inwin`, nodes at 10.72.13.11–13). There is no build or test suite — Flux watches `main` and applies whatever merges. Changes take effect by committing and pushing, then waiting for (or forcing) reconciliation.

Note: README.md's "Cluster" section is outdated (describes an old k3s/rook-ceph layout). The actual structure is below.

## Common commands

```sh
# Validate a kustomize layer renders before committing
kubectl kustomize core/        # or platform/, services/media/, etc.

# Force Flux to pick up a pushed change immediately
flux reconcile kustomization <core|platform|services-media|...> --with-source

# Debug a stuck release
flux get kustomizations
flux get helmreleases -A

# Edit the (only) SOPS-encrypted file; PGP keys are in .sops.yaml
sops config/secrets.yaml

# Manage cluster-secrets keys without hand-editing SOPS (value from stdin if omitted)
scripts/cluster-secrets set|get|list|remove <KEY>

# Manage garage S3 credentials in platform/garage/s3-keys.yaml
scripts/garage-creds add|list|show|remove <name>

# Cluster provisioning / upgrades (k0sctl config)
k0sctl apply --config config/cluster.yaml
```

## Architecture: Flux layering

`bootstrap/flux-system/gotk-sync.yaml` points Flux at `./bootstrap`, which holds one Flux `Kustomization` per layer with explicit ordering via `dependsOn`:

1. **`core/`** — cluster infrastructure: cert-manager, Longhorn (storage), MetalLB, k8tz, reloader, external-snapshotter, plus `core/operators/` (CloudNativePG, Dragonfly, Grafana operator, Strimzi) and `core/repositories/` (the shared `app-template` OCIRepository in `flux-system`). Also `core/hermes-viewer/` (read-only ServiceAccount/RBAC for cluster introspection).
2. **`platform/`** (dependsOn core) — shared services: `database/` (CNPG PostgreSQL 18, Strimzi Kafka + topics), `garage/`, `minio.yaml`, `gateway/` (Envoy Gateway + Gateway API), `monitoring/` (Grafana, Prometheus stack, Loki, Promtail), `security/` (pocket-id, OIDC SecurityPolicy controller — a Metacontroller DecoratorController whose webhook source lives in `controllers/oidc-policy/`).
3. **`services/<group>/`** (each dependsOn platform; one Flux Kustomization per group in `bootstrap/`) — workloads: `automation/` (frigate, home-assistant, zwave-js, hermes-agent), `development/` (forgejo, bb — a coding-agent web UI on the `images/devbox` image), `general/` (vaultwarden, unifi, actual), `immich/`, `media/` (plex, sonarr, radarr, sabnzbd).

All layers decrypt with SOPS (`sops-gpg` secret) and run `postBuild.substituteFrom` the `cluster-secrets` Secret — manifests reference `${CLUSTER_DOMAIN}`, `${LOAD_BALANCER_*}`, `${POSTGRES_ADDRESS}`, credentials, etc. as literal `${VAR}` strings that Flux substitutes at apply time. New variables go in `config/secrets.yaml` (SOPS-encrypted, included by `core/kustomization.yaml`).

**`prune: false` on shared layers**: deleting a file from git does NOT delete the resource from the cluster. The root `flux-system` scope uses pruning. Final retirement removes the five temporary scopes through their existing live `WaitForTermination` deletion policies; Crossplane's final namespace/source are collected from its saved inventory.

`bootstrap/service-retirement.yaml` is a one-time Purko namespace deletion Job with permissions limited to that namespace and reading Purko's old HelmRelease. Keep its completed Job as a receipt until cleanup is verified; do not add a TTL that would cause Flux to recreate it every sync. It is excluded from Renovate because Job pod templates are immutable. Follow `docs/service-retirement.md` for verification and removal of the Job/RBAC afterward. Retained CRDs, Longhorn data, and manually managed database/credentials need explicit cleanup. Preserve the shared `operators-system`, `automation`, and `media` namespaces and shared storage.

## Conventions

- **One file per app**, containing everything for that app: PVC(s), OCIRepository (if not app-template), HelmRelease, and any extras. Add the file to the directory's `kustomization.yaml`.
- Most apps use the **bjw-s `app-template`** chart via `chartRef: {kind: OCIRepository, name: app-template, namespace: flux-system}`. Follow an existing service file (e.g. `services/general/vaultwarden.yaml`) for the shape: `controllers` → `containers` → `service` → `route` → `persistence`.
- **Ingress is Gateway API**, not Ingress: app-template `route:` blocks (or standalone HTTPRoutes) with `parentRefs: [{name: external, namespace: gateway}]` and hostnames under `*.${CLUSTER_DOMAIN}`.
- Every YAML file starts with a `# yaml-language-server: $schema=...` comment (kubernetes-schemas.pages.dev for Flux/k8s kinds, the bjw-s schema for app-template HelmReleases).
- Image tags are pinned to digests; **Renovate** (`.github/renovate.json5`) manages bumps. Don't hand-edit versions unless asked.
- Commit messages are conventional commits scoped to the area: `feat(core): ...`, `fix(frigate): ...`, `chore(deps): ...`.
- Postgres roles/databases are created manually (not GitOps) — see `platform/database/README.md` for the SQL.
- `images/<name>/` holds Dockerfiles for images built in this repo (like `controllers/`, each has a workflow in `.github/workflows/` that pushes to GHCR and opens a digest-bump PR). Tool versions are `ARG`s with `# renovate:` comments.
- `config/ubuntu/` is host-level OS config (sysctl, kernel modules) applied to nodes via `config/ubuntu/apply.sh`, outside Kubernetes.
