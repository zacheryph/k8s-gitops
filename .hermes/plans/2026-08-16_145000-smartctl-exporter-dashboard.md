# Smartctl Exporter Grafana Dashboard Implementation Plan

> **For Hermes:** Implement the dashboard through the existing Grafana Operator and monitoring Kustomization.

**Goal:** Import Grafana dashboard 22604 revision 3 for the already-deployed smartctl-exporter metrics.

**Architecture:** Add a `GrafanaDashboard` custom resource in the existing monitoring dashboards collection. Use `grafanaCom` with dashboard ID `22604` and revision `3`, map its Prometheus datasource to the existing Grafana datasource, and select the existing Grafana instance. No dashboard JSON is vendored into Git; Grafana Operator retrieves the pinned Grafana.com revision.

**Tech Stack:** Grafana Operator `GrafanaDashboard`, Grafana.com dashboard 22604 revision 3, Prometheus.

---

### Task 1: Add the GrafanaDashboard resource

**Files:**
- Create: `platform/monitoring/dashboards/smartctl-exporter.yaml`

Configure:
- `metadata.name: smartctl-exporter`
- namespace `monitoring`
- the existing `grafana.internal/instance: grafana` selector
- datasource mapping `prometheus` → `DS_PROMETHEUS`
- Grafana.com dashboard ID `22604`, revision `3`

### Task 2: Wire the dashboard into Kustomize

**Files:**
- Modify: `platform/monitoring/dashboards/kustomization.yaml`

Add `smartctl-exporter.yaml` to the dashboard resources.

### Task 3: Validate

Run:
```bash
uv run --with pyyaml python - <<'PY'
import yaml
from pathlib import Path
for p in [
    Path('platform/monitoring/dashboards/smartctl-exporter.yaml'),
    Path('platform/monitoring/dashboards/kustomization.yaml'),
]:
    list(yaml.safe_load_all(p.read_text()))
    print(f'{p}: valid YAML')
PY
git diff --check
git diff --stat
git diff
```

Before implementation, verify the Grafana.com page reports dashboard ID `22604`, revision `3`, Prometheus as its datasource, and Grafana 11.4.0 as a dependency. Verify the current repository uses the same `GrafanaDashboard`/`grafanaCom` pattern and Grafana instance selector.

### Task 4: Commit and open the PR

```bash
git add platform/monitoring/dashboards/smartctl-exporter.yaml platform/monitoring/dashboards/kustomization.yaml .hermes/plans/2026-08-16_145000-smartctl-exporter-dashboard.md
git commit -m "feat(monitoring): add smartctl exporter dashboard"
git push -u origin feat/smartctl-exporter-dashboard
gh pr create --base main --head feat/smartctl-exporter-dashboard --title "feat(monitoring): add smartctl exporter dashboard" --body '<summary and verification>'
```

Do not include private homelab domains or public IPs in the PR metadata.

### Risks and tradeoffs

- The dashboard is imported by Grafana Operator from Grafana.com rather than vendored, matching existing repository conventions and keeping the GitOps diff small.
- The dashboard requires Prometheus metrics from smartctl-exporter and Grafana features compatible with its stated Grafana 11.4.0 dependency.
- Dashboard variables include node, disk, interface, model, and serial filters; the upstream dashboard queries the smartctl-exporter metric labels directly.

### Rollback

Remove the dashboard resource and its Kustomize entry. Since the platform Flux layer uses `prune: false`, deletion from Git may require explicit cleanup of the GrafanaDashboard resource after merge.

## Verification result

Pending YAML validation, diff checks, commit, push, and PR creation.
