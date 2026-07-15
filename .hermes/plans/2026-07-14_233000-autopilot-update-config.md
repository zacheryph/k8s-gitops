# k0s Autopilot UpdateConfig — Automatic Cluster Updates

> **For Hermes:** Use subagent-driven-development to implement this plan task-by-task.

**Goal:** Add a k0s autopilot `UpdateConfig` to enable automatic k0s version updates during a weekend maintenance window. k0s autopilot CRDs are already embedded in the k0s binary and applied at startup — this just provides the config.

**Safety:** All 3 nodes are control-plane nodes. Autopilot's built-in safeguards (sequential controller updates, quorum health gate, SHA256 verification, immutable plans) mean updates are safe by default. The `stable` channel + weekend 2am–6am window minimizes risk further.

**Architecture:**

```
UpdateConfig (kube-system) ──► k0s autopilot controller
  │                              │
  │ channel: stable               ├─ polls updates.k0sproject.io
  │ window: Sat/Sun 2am-6am       ├─ detects new version
  │ selector: control-plane=true  ├─ creates immutable Plan
  │                               └─ executes 1 controller at a time
  │                                    (checks /ready before each)
```

**Tech Stack:** Native k0s CRD (`autopilot.k0sproject.io/v1beta2`), no external deps.

**Assumptions:**
- k0s v1.36.2 already running (no immediate update fires)
- Autopilot CRDs are embedded in k0s (applied at startup, no extra install)
- The update server `https://updates.k0sproject.io/` is reachable from the cluster
- All 3 nodes have label `node-role.kubernetes.io/control-plane=true` (standard k0s label)

---

### Task 1: Create autopilot UpdateConfig manifest

**Objective:** Add the `UpdateConfig` resource that tells k0s autopilot to periodically check for updates and auto-apply them within the defined window.

**File:** `core/autopilot.yaml`

```yaml
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/autopilot.k0sproject.io/updateconfig_v1beta2.json
---
apiVersion: autopilot.k0sproject.io/v1beta2
kind: UpdateConfig
metadata:
  name: autopilot
  namespace: kube-system
spec:
  channel: stable
  updateServer: https://updates.k0sproject.io/
  upgradeStrategy:
    type: periodic
    periodic:
      days: [Saturday, Sunday]
      startTime: "02:00"
      length: 4h
  planSpec:
    commands:
    - k0supdate:
        targets:
          controllers:
            discovery:
              selector:
                labels: node-role.kubernetes.io/control-plane=true
```

**Design decisions:**
- `channel: stable` — not `unstable` or `edge_release`. Only production releases.
- `periodic` strategy with Sat/Sun 2am-6am window — off-hours, 4-hour window is generous for sequential updates.
- `selector` discovery (`control-plane=true`) — automatically picks up all 3 nodes, survives node additions.
- No `workers` section — all 3 nodes are controllers; no worker-only nodes exist.
- No `forceupdate: true` — autopilot compares versions and only acts when a newer one exists.
- No explicit `platforms` — the update server provides version, URL, and SHA256.

**Commit:**
```bash
git add core/autopilot.yaml
git commit -m "feat(core): add k0s autopilot UpdateConfig"
```

---

### Task 2: Wire into core kustomization

**Objective:** Add `autopilot.yaml` to `core/kustomization.yaml` so Flux includes it.

**File:** `core/kustomization.yaml` — add `- autopilot.yaml` under `resources:` (alphabetical, after `- ../config/secrets.yaml`).

**Commit:**
```bash
git add core/kustomization.yaml
git commit -m "chore(core): wire autopilot into kustomization"
```

---

### Verification

1. **kustomize build validates:**
   ```bash
   kubectl kustomize core/ | grep -A20 "kind: UpdateConfig"
   ```
   Should show the UpdateConfig with namespace `kube-system`.

2. **After Flux reconciles:**
   ```bash
   kubectl get updateconfig -n kube-system
   # Expected: autopilot   <age>
   ```

3. **Autopilot plan status (on next update check):**
   ```bash
   kubectl get plan autopilot -oyaml
   # If cluster is already at latest: status.state should be Completed or similar
   # If update is available: status.state will progress through SchedulableWait, etc.
   ```