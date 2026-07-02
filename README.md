# k8s-gitops

Flux v2 GitOps state for a 3-node k0s homelab cluster. Push to `main` and Flux applies it — no CI/CD pipeline, no manual `kubectl apply`.

## Architecture

```
bootstrap/          Flux bootstrapping + one Kustomization per layer
├── flux-system/    Flux controllers + GitRepository pointing at this repo
├── core.yaml       → core/
├── platform.yaml   → platform/  (dependsOn: core)
└── services-*.yaml → services/  (dependsOn: platform)
```

**Layers apply in order with explicit `dependsOn` chains:**

1. **`core/`** — Cluster infrastructure: cert-manager, Longhorn (storage), MetalLB (load balancing), Kyverno (policy), k8tz, reloader, Crossplane, external-snapshotter, plus operators (CloudNativePG, Dragonfly, Grafana operator, Strimzi Kafka) and a read-only ServiceAccount for cluster introspection.

2. **`platform/`** — Shared services: PostgreSQL 18 (CNPG), Kafka (Strimzi), Garage (S3-compatible object storage), Envoy Gateway + Gateway API, Prometheus/Grafana/Loki/Promtail monitoring stack, Pocket ID (OIDC), Velero (backup), ExternalDNS (AdGuard Home + Cloudflare), Crossplane providers.

3. **`services/<group>/`** — Application workloads, one Flux Kustomization per group:
   - `automation/` — Home Assistant, Frigate NVR, Z-Wave JS, Hermes Agent, Signal CLI
   - `development/` — Forgejo (self-hosted Git)
   - `general/` — Vaultwarden, Actual Budget
   - `immich/` — Immich photo management
   - `media/` — Plex, Sonarr, Radarr, SABnzbd, ErsatzTV

All layers use `prune: false` — deleting a file from git does **not** delete the resource from the cluster. Clean up removed resources manually.

## Secrets

Managed with [SOPS](https://github.com/getsops/sops) (PGP encryption) and Flux variable substitution:

- **`config/secrets.yaml`** — SOPS-encrypted `cluster-secrets` Secret containing `${VARIABLES}` substituted at apply time (domain names, IPs, credentials, API keys).
- **`platform/garage/s3-keys.yaml`** — SOPS-encrypted S3 credentials for Garage.
- **`scripts/cluster-secrets`** — Helper for managing `cluster-secrets` keys without hand-editing SOPS.
- **`scripts/garage-creds`** — Helper for managing Garage S3 credentials.

Manifests reference variables as literal `${CLUSTER_DOMAIN}`, `${LOAD_BALANCER_*}`, `${POSTGRES_ADDRESS}`, etc. Flux resolves them from `cluster-secrets` at apply time.

## Conventions

- **One file per app** — Everything for a workload lives in a single YAML file: PVC, HelmRelease, and any extras. Add it to the directory's `kustomization.yaml`.
- **bjw-s `app-template` chart** — Most apps use the community [app-template](https://github.com/bjw-s/helm-charts) via `chartRef` to a shared `OCIRepository`. Follow an existing service file for the shape: `controllers` → `containers` → `service` → `route` → `persistence`.
- **Gateway API for ingress** — `route:` blocks (or standalone HTTPRoutes) with Gateway API parents, not the legacy Ingress resource.
- **Image tags pinned to digests** — [Renovate](https://docs.renovatebot.com/) manages version bumps via `.github/renovate.json5`. Don't hand-edit image versions.
- **Conventional commits** — `feat(<area>): description` / `fix(<area>): description` / `chore(deps): description`.
- **YAML language server comments** — Every file starts with `# yaml-language-server: $schema=...` for editor support.

## Operations

```sh
# Validate a kustomize layer renders
kubectl kustomize core/

# Force Flux to reconcile immediately
flux reconcile kustomization <name> --with-source

# Debug stuck releases
flux get kustomizations
flux get helmreleases -A

# Edit SOPS-encrypted secrets
sops config/secrets.yaml
```

## Cluster Provisioning

Node OS configuration lives in `config/ubuntu/` (sysctl, kernel modules) and is applied with `config/ubuntu/apply.sh`. Cluster provisioning uses k0sctl with `config/cluster.yaml`.