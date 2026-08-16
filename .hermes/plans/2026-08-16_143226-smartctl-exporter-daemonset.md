# Smartctl Exporter DaemonSet Implementation Plan

> **For Hermes:** Implemented in `platform/monitoring` as a Flux OCI HelmRelease.

**Goal:** Deploy Prometheus smartctl-exporter as a privileged DaemonSet so every cluster node exposes local disk SMART metrics to Prometheus.

**Architecture:** Use the upstream `prometheus-community/charts/prometheus-smartctl-exporter` OCI chart. The chart renders one privileged DaemonSet pod per node, mounts `/dev`, scans devices automatically, exposes port 9633, and is discovered by the existing Prometheus Operator through a ServiceMonitor. The chart's bundled PrometheusRule is enabled for common SMART failure conditions.

**Tech Stack:** Flux HelmRelease, Flux OCIRepository, Prometheus Operator ServiceMonitor/PrometheusRule, smartctl-exporter v0.14.0.

---

### Task 1: Register the upstream OCI chart

**Files:**
- Modify: `platform/monitoring/oci-repositories.yaml`

Add an OCIRepository named `prometheus-smartctl-exporter` pointing to `oci://ghcr.io/prometheus-community/charts/prometheus-smartctl-exporter`, pinned to chart version `0.17.1`.

### Task 2: Add the monitoring HelmRelease

**Files:**
- Create: `platform/monitoring/smartctl-exporter.yaml`

Configure:
- image `quay.io/prometheuscommunity/smartctl-exporter:v0.14.0@sha256:cfe22c36d7d2fac48ebf619707305acb65eb0fb670656eb80f356e606d782bc1`
- automatic device discovery using the chart defaults
- privileged exporter pods, as required by smartctl
- 120-second collection interval
- ServiceMonitor at 60 seconds with node metadata attached
- bundled SMART alert rules
- CPU and memory requests/limits
- rolling DaemonSet update strategy with one unavailable pod maximum

### Task 3: Wire the manifest into the monitoring Kustomization

**Files:**
- Modify: `platform/monitoring/kustomization.yaml`

Add `smartctl-exporter.yaml` to the resources list.

### Task 4: Validate and inspect

Run:
```bash
python3 - <<'PY'
import yaml
from pathlib import Path
for p in [
    Path('platform/monitoring/oci-repositories.yaml'),
    Path('platform/monitoring/smartctl-exporter.yaml'),
    Path('platform/monitoring/kustomization.yaml'),
]:
    list(yaml.safe_load_all(p.read_text()))
    print(f'{p}: valid YAML')
PY
git diff --check
git diff --stat
git diff
```

The upstream chart template was inspected to verify that it renders a privileged DaemonSet with `/dev` mounted at `/hostdev`, automatic device discovery when no explicit devices are configured, a ClusterIP Service, and optional ServiceMonitor/PrometheusRule resources.

### Task 5: Commit and open the PR

Use the repository's global Git identity, commit with:
```bash
git add platform/monitoring/oci-repositories.yaml platform/monitoring/smartctl-exporter.yaml platform/monitoring/kustomization.yaml
git commit -m "feat(monitoring): deploy smartctl exporter daemonset"
git push -u origin feat/scrutiny-daemonset
gh pr create --base main --head feat/scrutiny-daemonset --title "feat(monitoring): deploy smartctl exporter daemonset" --body-file <body>
```

Do not include public domain names or public IPs in the PR metadata.

### Risks and tradeoffs

- SMART collection requires privileged pods and host `/dev` access; this is inherent to querying disk devices and is not avoidable through normal container hardening.
- The chart creates a Service selecting all exporter DaemonSet pods. Prometheus therefore scrapes each node-local exporter through the Service endpoints.
- The repository does not currently deploy a second disk dashboard; the exporter metrics and alert rules are immediately available through Prometheus/Grafana.
- The image digest is the multi-architecture manifest-list digest; the registry resolves the node-specific image manifest at pull time.

### Verification result

Pending execution of YAML parsing, diff checks, commit, push, and PR creation.

---

## Commits

- `feat(monitoring): deploy smartctl exporter daemonset`

## Review

Before opening the PR, confirm the final diff contains only the OCIRepository, HelmRelease, Kustomization wiring, and this plan. Confirm no real homelab identifiers appear in tracked files or PR metadata.

## Rollback

Remove the HelmRelease and its Kustomization entry, then reconcile the monitoring Flux Kustomization. Because the layer uses `prune: false`, removal from Git does not delete the release automatically; cleanup would require an explicit cluster-side deletion after merge if rollback is needed.

## Notes

The old `tools/smarttools.yaml` historical manifest was not reused: it was a one-off Pod and did not represent the upstream smartctl-exporter chart deployment.