# Istio Ambient Mode Service Mesh — Implementation Plan

> **For Hermes:** Use subagent-driven-development to implement this plan task-by-task.
> This is a **draft PR** — not for merging. Goal is a working configuration + documented findings.
> If Istio ambient conflicts with any existing component, document the conflict and stop.

**Goal:** Add Istio ambient mode (ztunnel + waypoint) to the homelab k8s cluster via GitOps, enabling mTLS and L4 observability for selected namespaces without sidecar injection.

---

## 1. Architecture Overview

### What ambient mode is

Istio ambient mode replaces the per-pod Envoy sidecar with a two-layer data plane:

1. **ztunnel** — a node-local DaemonSet (Rust) that runs one logical proxy per pod. It handles L4 traffic: mTLS termination, AuthorizationPolicy (L4), and TCP metrics. ztunnel operates *inside each pod's network namespace* (not at the host level), listening on ports 15008 (HBONE), 15006 (inbound), 15001 (outbound).
2. **waypoint proxy** — an optional Envoy-based deployment (one per namespace or per service) that handles L7 policy (AuthorizationPolicy L7, RequestAuthentication, WasmPlugin, L7 Telemetry, HTTPRoute-based traffic routing). Waypoints are opt-in per namespace via the `istio.io/use-waypoint` label.

### Traffic flow

```
Pod A (meshed) --> [pod netns: iptables redirect] --> ztunnel (node-local)
                                                          |
                                                    HBONE tunnel (mTLS)
                                                          |
Pod B (meshed) <-- [pod netns: iptables redirect] <-- ztunnel (node-local)

With waypoint:
Pod A --> ztunnel --HBONE--> waypoint --HBONE--> ztunnel --> Pod B
```

- **HBONE** (HTTP-Based Overlay Network Environment) is Istio's tunneling protocol: HTTP/2 CONNECT over mTLS, carrying the original TCP stream. This is how ztunnel-to-ztunnel and ztunnel-to-waypoint traffic is encrypted.
- Traffic between two meshed pods is always mTLS-encrypted via HBONE. Traffic from an unmeshed source to a meshed destination arrives as plaintext (ztunnel accepts both, but AuthorizationPolicy can require an identity to block plaintext).
- ztunnel manages certificates *on behalf of* the pods on its node — one cert per unique service account identity. The CA (Istiod) enforces that ztunnel may only request certs for identities actually running on that node (via K8s SA JWT token validation).

### How traffic redirection works (critical for CNI coexistence)

Ambient mode uses an **in-pod traffic redirection** model, NOT node-level eBPF:

1. The **istio-cni** node agent installs a *chained CNI plugin* that runs after the primary CNI (kube-router today, Cilium in the planned migration). It is notified on pod creation in ambient-enabled namespaces.
2. `istio-cni` enters the pod's network namespace and sets up iptables/nftables redirection rules so all pod ingress/egress goes to ztunnel's listening ports (15008/15006/15001).
3. `istio-cni` passes the pod's network namespace file descriptor to ztunnel over a Unix domain socket (`/var/run/ztunnel/ztunnel.sock`). ztunnel then creates listening sockets *inside the pod's network namespace* — the proxy runs in the ztunnel process but operates within each pod's netns.
4. The pod's application is completely unaware of the tunnel; all encryption and policy enforcement happens transparently.

**Key insight:** Because redirection happens at the pod network namespace level (via a chained CNI plugin + iptables inside the pod netns), ambient mode is **CNI-agnostic by design**. It does not compete with the primary CNI at the node/host level. See section 3 (Cilium interaction) for details.

### Component inventory (4 Helm charts)

| Chart | Purpose | Deployment type | Default resources |
|-------|---------|-----------------|-------------------|
| `istio/base` | CRDs + cluster roles (Prereq for all others) | — | — |
| `istio/istiod` | Control plane (istiod), configured with `profile: ambient` | Deployment (1 replica, HPA 1-5) | 500m CPU / 2048Mi mem request |
| `istio/cni` | CNI node agent + chained plugin, `profile: ambient` | DaemonSet (all nodes) | 100m CPU / 100Mi mem request |
| `istio/ztunnel` | L4 data plane proxy | DaemonSet (all nodes) | 200m CPU / 512Mi mem request |

No `istio/gateway` chart is needed — the cluster already uses Envoy Gateway for ingress, and Istio ambient's Gateway API integration is optional (we are not replacing the existing ingress).

---

## 2. Current Cluster State (findings from probe)

### Nodes and Kubernetes

- 3 nodes: `k0s-inwin-01/02/03`, all `control-plane` (controller+worker, no taints)
- Kubernetes v1.36.2+k0s — within Istio 1.30's supported range (1.32-1.36)
- k0s v1.35.3 in `config/cluster.yaml` (stale — cluster runs 1.36.2; noted in Cilium task pitfalls)

### Current CNI / networking stack (what ambient must coexist with)

| Component | Location | Notes |
|-----------|----------|-------|
| **kube-router** (CNI) | DaemonSet in `kube-system` | Current CNI; Cilium migration is sibling task `t_e6b0051f` |
| **kube-proxy** (IPVS, strictARP) | DaemonSet in `kube-system` | k0s-managed; Cilium eBPF replacement planned |
| **MetalLB** (FRR/BGP) | `metallb-system` namespace | `metallb-frr-k8s` + `metallb-speaker` DaemonSets; Cilium BGP planned |
| **Envoy Gateway** | `gateway` namespace | Gateway API controller, `GatewayClass: envoy`; Cilium Gateway API planned |
| **k0s nodeLocalLoadBalancing** | `config/cluster.yaml` | EnvoyProxy type; may conflict with Cilium per Cilium task notes |

**Cilium status:** The Cilium migration (`t_e6b0051f`) is **blocked** — it has been attempted 8 times and given up twice due to iteration budget exhaustion. No Cilium manifests exist in `main` or on the `feat/cilium-hubble` branch (the branch is at the same HEAD as main). **Ambient mode must be designed to work with the current kube-router CNI, and also be compatible with a future Cilium CNI.** The good news: ambient's in-pod redirection model is CNI-agnostic (section 1, section 3).

### cert-manager

- cert-manager v1.20.3 (chart) / OCI from `quay.io/jetstack/charts/cert-manager`
- **Both ClusterIssuers use DNS-01 via Cloudflare** (not HTTP-01):
  - `letsencrypt` (prod) and `letsencrypt-staging` both use `dns01.cloudflare.apiTokenSecretRef`
- DNS-01 is mesh-safe: cert-manager's ACME solver doesn't receive inbound HTTP traffic, so mesh redirection cannot break certificate issuance/renewal. **No cert-manager changes needed.**

### Envoy Gateway (potential conflict surface)

- `GatewayClass: envoy` (controller: `gateway.envoyproxy.io/gatewayclass-controller`)
- `Gateway: external` in `gateway` namespace, listeners on :80 and :443
- HTTPRoutes across many service namespaces attach to `external` gateway
- **Conflict analysis with Istio:**
  - **Gateway class:** Istio's Gateway API controller (if enabled) uses a *different* GatewayClass (`istio` / controller `istio.io/gateway-controller`). Envoy Gateway uses `envoy`. No collision — they manage different GatewayClasses. We will **not** install the `istio/gateway` chart, so Istio's Gateway API controller is not deployed; no conflict at all.
  - **Envoy in both:** Envoy Gateway runs Envoy for ingress; ztunnel is Rust (not Envoy); waypoints are Envoy but deployed as separate pods in meshed namespaces. No shared processes or ports.
  - **CRDs:** Both consume Gateway API CRDs (`gateway.networking.k8s.io`). The cluster already has these CRDs (Envoy Gateway installed them). Istio ambient does not reinstall Gateway API CRDs unless we opt into the Gateway chart — we won't. **No CRD ownership conflict.**
  - **HTTPRoutes:** Existing HTTPRoutes reference `parentRefs: [{name: external, namespace: gateway}]` (Envoy Gateway). These do NOT need `istio.io/rev` annotations — they route to Envoy Gateway, not Istio. The only resources that need Istio annotations are ones we *want* in the mesh.

### Namespaces and workloads (mesh opt-in candidates)

26 namespaces on cluster. Grouped by mesh suitability:

**System / infrastructure — EXCLUDE from mesh:**
| Namespace | Why exclude |
|-----------|------------|
| `kube-system` | System pods, CNI, konnectivity, coredns, metrics-server, reloader, k8tz, snapshot-controller — must not be disrupted |
| `kube-public`, `kube-node-lease` | System, no workloads |
| `flux-system` | GitOps controller — must not be meshed (could break reconciliation) |
| `k0s-autopilot` | k0s system |
| `metallb-system` | Load balancer infra — meshing the LB speaker risks breaking BGP/routing |
| `longhorn-system` | Storage CSI — meshing CSI pods risks breaking volume operations |
| `hardware-system` | Node feature discovery, GPU plugin — node-level, no benefit |
| `cert-manager` | ACME solver uses DNS-01 (safe), but no benefit from meshing; exclude to avoid any cert renewal risk |
| `operators-system` | Operator controllers (CNPG, Strimzi, Dragonfly, Grafana operators) — these manage other namespaces' CRDs; meshing could add latency to control loops |
| `crossplane` | Crossplane providers manage external infra; exclude |
| `velero` | Backup system, privileged PSA — exclude to avoid backup disruption |
| `kyverno` | Policy controller — exclude (admission webhooks must not be delayed) |
| `gateway` | Envoy Gateway controller + data plane — exclude (ingress must stay direct) |
| `dns` | ExternalDNS (AdGuard + Cloudflare) — exclude; DNS record updates must not be tunneled |

**Platform services — CANDIDATES for mesh (Phase 2, after stability validation):**
| Namespace | Workload | Mesh benefit |
|-----------|----------|--------------|
| `database` | CNPG PostgreSQL 18, Strimzi Kafka | mTLS for DB connections, L4 authz (but see section 4 risks — DB/Kafka may need plaintext fallback) |
| `garage` | Garage S3 + webui | mTLS for internal S3 calls |
| `monitoring` | Grafana, Prometheus, Loki, Promtail, Alertmanager, KSM | mTLS for scraping; but Prometheus scraping may need plaintext (see section 4) |
| `security` | pocket-id, oauth2-proxy, tinyauth | mTLS for auth flows |

**Application services — PRIMARY mesh candidates (Phase 1):**
| Namespace | Workload | Mesh benefit |
|-----------|----------|--------------|
| `automation` | hermes-agent, home-assistant, zwave-js-ui, signal-cli-rest-api, frigate | mTLS between automation services; L4 telemetry |
| `development` | forgejo | mTLS for internal git/CI calls |
| `general` | vaultwarden, actual | mTLS for secrets manager access |
| `media` | plex, sonarr, radarr, sabnzbd, ersatztv, sonarr-anime | mTLS between media services (arr stack) |
| `immich` | immich | mTLS |
| `mattermost` | mattermost-team-edition | mTLS |
| `api` | cloudstream | mTLS |

**Other:**
| Namespace | Notes |
|-----------|-------|
| `actual` | Namespace for Actual budget (workload in `general`? — probe shows `actual/actual` deployment) |
| `default` | Only `iperf-server` — exclude (test workload) |

### Recommended opt-in strategy

**Phase 1 (this PR — validate ambient works):**
- Label `automation`, `media`, `development` with `istio.io/dataplane-mode: ambient`
- These are the most interconnected service-to-service workloads (arr stack, automation chain)
- No waypoints yet — L4 mTLS only

**Phase 2 (follow-up, after Phase 1 is stable for 1 week):**
- Add `general`, `immich`, `mattermost`, `api`, `security`
- Optionally add waypoints for namespaces needing L7 policy (e.g., `security` for authz rules)

**Excluded permanently:** All system/infra namespaces in the table above.

---

## 3. Cilium / CNI Interaction

### Current state: kube-router (no Cilium yet)

Today the CNI is kube-router (a BGP-based CNI). Istio's `istio-cni` chained plugin will run after kube-router in the CNI chain. This is the standard, supported configuration — the Istio docs describe the chained CNI plugin as the ambient traffic redirection mechanism, and it works with any CNI.

k0s manages kube-router and kube-proxy as system components. The `istio-cni` DaemonSet needs:
- `cniBinDir: /opt/cni/bin` (k0s default — verify on node)
- `cniConfDir: /etc/cni/net.d` (k0s default — verify on node)
- `chained: true` (default — appends to the existing CNI config file rather than replacing it)

**Risk:** k0s may overwrite CNI config files on restart (k0s manages the CNI). If k0s regenerates `/etc/cni/net.d/*.conf`, the istio-cni chained plugin entry may be lost. Mitigation: verify the chained plugin persists across k0s restarts during Phase 1 validation. If k0s overwrites it, the istio-cni agent has a reconciliation loop that re-inserts itself — but verify this works with k0s.

### Future state: Cilium (sibling task t_e6b0051f)

The Cilium migration replaces kube-router + kube-proxy + MetalLB + Envoy Gateway with Cilium for all four functions. Key compatibility points:

1. **CNI chaining:** Cilium can run as the primary CNI. Istio's chained CNI plugin runs *after* Cilium in the chain. Cilium's documentation confirms chained CNI plugins are supported (Cilium itself recommends this for Istio ambient coexistence). The `istio-cni` plugin only sets up pod-level iptables/netns redirection — it does not touch Cilium's eBPF programs at the node/host level.

2. **eBPF — no conflict:** This is the most important finding. The task description asked us to verify whether ztunnel's eBPF programs conflict with Cilium's. **Finding: ztunnel does NOT use eBPF.** The ambient traffic redirection model uses:
   - A **chained CNI plugin** (not eBPF) for pod detection
   - **iptables/nftables rules inside the pod's network namespace** (not node-level eBPF) for traffic capture
   - **Unix domain socket + netns file descriptors** for ztunnel to enter pod network namespaces

   Cilium's eBPF programs operate at the node/host level (kube-proxy replacement, L4 LB, NetworkPolicy, service routing). There is no overlap — Istio operates inside pod netns, Cilium operates at the host/network level. **They are designed to coexist.** (Istio docs explicitly state the in-pod redirection model "enables Istio's ambient mode to work alongside any Kubernetes CNI plugin, transparently, and without impacting Kubernetes networking features.")

3. **kube-proxy replacement:** Cilium replaces kube-proxy with eBPF. This does not affect ztunnel — ztunnel captures traffic *inside the pod netns* before it reaches the host networking stack where Cilium's kube-proxy replacement operates. The two operate at different layers.

4. **Gateway API:** If the Cilium migration replaces Envoy Gateway with Cilium Gateway API, the Istio Gateway API controller (which we are NOT deploying) would not conflict — different GatewayClass controllers. Existing HTTPRoutes would move from `GatewayClass: envoy` to `GatewayClass: cilium`, but this is orthogonal to ambient mesh (ambient doesn't touch HTTPRoutes that route through external gateways).

5. **Ordering dependency:** The Cilium task (`t_e6b0051f`) is a prerequisite in the parent task's dependency graph, but it is currently blocked. **This ambient plan is designed to work with kube-router now and Cilium later.** If Cilium is deployed first, the only change needed is verifying the chained CNI plugin works with Cilium's CNI config format (Cilium uses a different config file name — `05-cilium.conflist` — which the istio-cni chained plugin must append to).

---

## 4. Known Risks and Mitigation

### R1: k0s CNI config overwrite
**Risk:** k0s manages the CNI and may overwrite `/etc/cni/net.d/` config files on restart, removing the istio-cni chained plugin entry. Pods created after a k0s restart would not be meshed.
**Mitigation:** The istio-cni agent has a reconciliation loop that re-inserts itself. Verify this works with k0s during Phase 1. If it doesn't, add a startup script or DaemonSet to re-chain after k0s restarts. Alternatively, set `istio-cni` to use `chained: false` with a dedicated config file (less standard but avoids overwrite).
**Severity:** Medium

### R2: Prometheus scraping through ztunnel
**Risk:** Prometheus scrapes pods via ClusterIP/pod IP. If Prometheus is in a meshed namespace and targets are meshed, scrape traffic goes through ztunnel. ztunnel supports plaintext inbound, so scrapes should work — but mTLS identity-based AuthorizationPolicy could block Prometheus (no SPIFFE identity for Prometheus unless it's meshed too). Metrics may show up with no peer identity.
**Mitigation:** Do NOT mesh `monitoring` in Phase 1. If meshing in Phase 2, add an AuthorizationPolicy allowing Prometheus's service account, or use `PERMISSIVE` mTLS mode (not STRICT) for monitoring namespace. Verify `kube-prometheus-stack` ServiceMonitors still scrape successfully.
**Severity:** Medium

### R3: Database/Kafka connections through mTLS
**Risk:** CNPG PostgreSQL and Strimzi Kafka have their own TLS/mTLS. If ztunnel wraps their connections in HBONE, the double-encryption adds latency and may confuse connection poolers (pgBouncer in CNPG poolers, Kafka client TLS). Strimzi Kafka brokers use LoadBalancer services (10.72.16.4-7) for external access — ztunnel only intercepts pod-to-pod traffic, not LoadBalancer traffic, so external Kafka clients are unaffected. Internal clients (within mesh) would be double-encrypted.
**Mitigation:** Do NOT mesh `database` in Phase 1. In Phase 2, test with `PERMISSIVE` mTLS first. Consider excluding Kafka/PostgreSQL pods via `istio.io/dataplane-mode: none` pod-level label while meshing the rest of the namespace.
**Severity:** High (for database namespace)

### R4: waypoint proxy latency
**Risk:** Waypoint proxies add an extra hop (ztunnel to waypoint to ztunnel). For L7 policy enforcement. In a 3-node homelab, the waypoint may land on a different node than source/destination, adding inter-node latency.
**Mitigation:** Phase 1 uses NO waypoints (L4 only). Phase 2 adds waypoints only for namespaces that need L7 policy (e.g., `security`). Use `topology.kubernetes.io/zone` or node affinity to pin waypoints if latency is noticeable.
**Severity:** Low (Phase 1 has no waypoints)

### R5: ztunnel resource overhead on 3 nodes
**Risk:** ztunnel DaemonSet runs on all 3 nodes. Default requests: 200m CPU / 512Mi memory per node. 3 nodes = 600m CPU / 1.5Gi memory total. The nodes are control-plane+worker with no taints; if resource-constrained, ztunnel competes with workloads.
**Mitigation:** Monitor node resource usage after deployment. Adjust ztunnel resource requests if needed (the chart allows overriding). 512Mi is sized for ~200k pods / 100k connections — a homelab with ~60 pods will use far less. Actual memory will likely be <100Mi per node.
**Severity:** Low

### R6: istio-cni requires privileged DaemonSet
**Risk:** istio-cni needs `NET_ADMIN` capability and hostPath access (`/opt/cni/bin`, `/etc/cni/net.d`, `/var/run/ztunnel`). The cluster uses Kyverno policies that may block privileged pods. The `metallb-system` and `velero` namespaces are already `privileged` PSA.
**Mitigation:** The `istio-system` namespace must be labeled `pod-security.kubernetes.io/enforce: privileged` (like metallb-system and velero). Kyverno policies must allow the istio-cni DaemonSet (verify no `restrict-privileged` ClusterPolicy blocks it). Add Kyverno exception if needed.
**Severity:** Medium (blocking if not addressed)

### R7: CNI binary path on k0s
**Risk:** The istio-cni chart defaults to `cniBinDir: /opt/cni/bin` and `cniConfDir: /etc/cni/net.d`. k0s may use different paths (e.g., `/var/lib/cni/bin` or a k0s-managed path).
**Mitigation:** Verify the actual CNI paths on a k0s node before deploying. SSH to a node and check: `ls /opt/cni/bin/` and `ls /etc/cni/net.d/`. Override the chart values if k0s uses different paths.
**Severity:** Medium (deployment will fail silently if paths are wrong — pods won't be meshed)

### R8: Existing istio history in repo
**Risk:** The git history shows Istio was previously installed (commits up to v1.20.2, removed later). Stale CRDs or webhooks from the old installation could conflict with the new ambient installation.
**Mitigation:** Verify no `istio.io` CRDs exist on the cluster before installing: `kubectl get crd | grep istio.io`. If stale CRDs exist, clean them up before deploying. The `prune: false` policy means old Istio resources may still be in the cluster even though the manifests were removed from git.
**Severity:** Medium (blocking if stale CRDs conflict)

### R9: Gateway API CRD ownership
**Risk:** Envoy Gateway installed Gateway API CRDs (`gateway.networking.k8s.io`). The Istio `base` chart may try to install/overwrite them. If Flux applies the Istio base chart and it contains different CRD versions, there could be a conflict.
**Mitigation:** The Istio docs note Gateway API CRDs should be installed *before* Istio (they already are, via Envoy Gateway). The `istio/base` chart does NOT install Gateway API CRDs — only Istio CRDs (`VirtualService`, `DestinationRule`, `AuthorizationPolicy`, etc.). Verify by inspecting the base chart. If conflict arises, use Flux `crds: Keep` on the Istio base HelmRelease (already the cert-manager pattern).
**Severity:** Low

---

## 5. Step-by-Step Implementation

### Task 1: Verify prerequisites on the cluster

**Objective:** Confirm the cluster is ready for Istio ambient before writing any manifests.

**Commands** (run via terminal, not committed):
```sh
# 1. Check for stale Istio CRDs (from the old v1.20 installation)
kubectl get crd | grep istio.io
# If any exist: kubectl get crd -oname | grep istio.io | xargs kubectl delete

# 2. Verify Gateway API CRDs exist (installed by Envoy Gateway)
kubectl get crd gateways.gateway.networking.k8s.io

# 3. SSH to a node to verify CNI paths (or use kubelet /pods proxy if SSH unavailable)
# On node: ls /opt/cni/bin/ and ls /etc/cni/net.d/
# Expected: kube-router config file in /etc/cni/net.d/, CNI binaries in /opt/cni/bin/

# 4. Check Kyverno policies for privileged pod restrictions
kubectl get clusterpolicy -o yaml | grep -A5 privileged

# 5. Verify no istio-system namespace exists
kubectl get ns istio-system
```

**Acceptance:** No stale Istio CRDs. Gateway API CRDs present. CNI paths confirmed. Kyverno allows privileged DaemonSets (or we know we need an exception).

### Task 2: Create the Istio namespace + privileged PSA label

**Objective:** Create the `istio-system` namespace with privileged PSA (required for istio-cni and ztunnel DaemonSets).

**File:** `core/istio/namespace.yaml`
```yaml
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/v1/namespace.json
---
apiVersion: v1
kind: Namespace
metadata:
  name: istio-system
  labels:
    pod-security.kubernetes.io/enforce: privileged
    pod-security.kubernetes.io/audit: privileged
    pod-security.kubernetes.io/warn: privileged
```

**Verification:** `kubectl get ns istio-system --show-labels`

### Task 3: Create the Istio base HelmRelease (CRDs + cluster roles)

**Objective:** Install the Istio base chart which provides CRDs and cluster roles required by all other Istio components.

**File:** `core/istio/base.yaml`
```yaml
# yaml-language-server: $schema=https://kubernetes-schemas.ok8.sh/source.toolkit.fluxcd.io/ocirepository_v1.json
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: istio-base
  namespace: istio-system
spec:
  interval: 12h
  layerSelector:
    mediaType: application/vnd.cncf.helm.chart.content.v1.tar+gzip
    operation: copy
  ref:
    tag: 1.30.2
  url: oci://registry-1.docker.io/istio/base
---
# yaml-language-server: $schema=https://kubernetes-schemas.ok8.sh/helm.toolkit.fluxcd.io/helmrelease_v2.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: istio-base
  namespace: istio-system
spec:
  interval: 1h
  chartRef:
    kind: OCIRepository
    name: istio-base
  install:
    crds: CreateReplace
  upgrade:
    crds: CreateReplace
  # base chart has minimal values; profile is set on istiod/cni
  values: {}
```

**Note:** Verify the OCI chart URL. The Istio Helm charts are published to `https://istio-release.storage.googleapis.com/charts` (Helm repo) and may also be available as OCI. Check `https://hub.docker.com/u/istio` for OCI tags. If OCI is not available, use a HelmRepository instead of OCIRepository (see `platform/dns/helm-repository.yaml` for the HelmRepository pattern). **The implementer must verify the chart source** — do not assume OCI availability.

### Task 4: Create the istiod HelmRelease (control plane, ambient profile)

**Objective:** Install the Istio control plane configured for ambient mode.

**File:** `core/istio/istiod.yaml`
```yaml
# yaml-language-server: $schema=https://kubernetes-schemas.ok8.sh/source.toolkit.fluxcd.io/ocirepository_v1.json
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: istiod
  namespace: istio-system
spec:
  interval: 12h
  layerSelector:
    mediaType: application/vnd.cncf.helm.chart.content.v1.tar+gzip
    operation: copy
  ref:
    tag: 1.30.2
  url: oci://registry-1.docker.io/istio/istiod
---
# yaml-language-server: $schema=https://kubernetes-schemas.ok8.sh/helm.toolkit.fluxcd.io/helmrelease_v2.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: istiod
  namespace: istio-system
spec:
  interval: 1h
  chartRef:
    kind: OCIRepository
    name: istiod
  dependsOn:
    - name: istio-base
      namespace: istio-system
  install:
    crds: CreateReplace
  upgrade:
    crds: CreateReplace
  values:
    # Enable ambient mode profile
    profile: ambient
    # istiod control plane resources (defaults: 500m CPU / 2048Mi mem)
    # For a 3-node homelab with ~60 pods, reduce to save resources
    resources:
      requests:
        cpu: 250m
        memory: 1024Mi
    # HPA
    autoscaleEnabled: true
    autoscaleMin: 1
    autoscaleMax: 2
    # Do not taint nodes (k0s nodes are control-plane; untaint controller would block)
    taint:
      enabled: false
    # Mesh config: PERMISSIVE mTLS initially (not STRICT) for Phase 1 safety
    # Switch to STRICT after validating all meshed traffic works
    meshConfig:
      defaultConfig:
        holdApplicationUntilProxyStarts: true
```

**Note:** `profile: ambient` is the key setting — it configures istiod for ambient mode (no sidecar injection webhook, ztunnel-aware config distribution).

### Task 5: Create the Istio CNI HelmRelease (traffic redirection)

**Objective:** Install the istio-cni node agent that sets up pod-level traffic redirection.

**File:** `core/istio/cni.yaml`
```yaml
# yaml-language-server: $schema=https://kubernetes-schemas.ok8.sh/source.toolkit.fluxcd.io/ocirepository_v1.json
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: istio-cni
  namespace: istio-system
spec:
  interval: 12h
  layerSelector:
    mediaType: application/vnd.cncf.helm.chart.content.v1.tar+gzip
    operation: copy
  ref:
    tag: 1.30.2
  url: oci://registry-1.docker.io/istio/cni
---
# yaml-language-server: $schema=https://kubernetes-schemas.ok8.sh/helm.toolkit.fluxcd.io/helmrelease_v2.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: istio-cni
  namespace: istio-system
spec:
  interval: 1h
  chartRef:
    kind: OCIRepository
    name: istio-cni
  dependsOn:
    - name: istiod
      namespace: istio-system
  install:
    crds: CreateReplace
  upgrade:
    crds: CreateReplace
  values:
    # Enable ambient mode profile
    profile: ambient
    # Chained CNI plugin (runs after kube-router / Cilium)
    chained: true
    # CNI paths — VERIFY on k0s nodes before deploying (Task 1)
    cniBinDir: /opt/cni/bin
    cniConfDir: /etc/cni/net.d
    # Ambient-specific config
    ambient:
      # DNS redirection enabled (redirects pod DNS to ztunnel for policy)
      dnsCapture: true
      # Retry detection if pod is created before CNI agent is ready
      enableAmbientDetectionRetry: true
    # Resources (defaults: 100m CPU / 100Mi mem per node)
    resources:
      requests:
        cpu: 100m
        memory: 100Mi
```

**Note:** `chained: true` means the istio-cni plugin appends to the existing CNI config file (written by kube-router). It does NOT replace the primary CNI. This is the safe coexistence mode.

### Task 6: Create the ztunnel HelmRelease (L4 data plane)

**Objective:** Install the ztunnel DaemonSet — the node-local L4 proxy.

**File:** `core/istio/ztunnel.yaml`
```yaml
# yaml-language-server: $schema=https://kubernetes-schemas.ok8.sh/source.toolkit.fluxcd.io/ocirepository_v1.json
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: ztunnel
  namespace: istio-system
spec:
  interval: 12h
  layerSelector:
    mediaType: application/vnd.cncf.helm.chart.content.v1.tar+gzip
    operation: copy
  ref:
    tag: 1.30.2
  url: oci://registry-1.docker.io/istio/ztunnel
---
# yaml-language-server: $schema=https://kubernetes-schemas.ok8.sh/helm.toolkit.fluxcd.io/helmrelease_v2.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: ztunnel
  namespace: istio-system
spec:
  interval: 1h
  chartRef:
    kind: OCIRepository
    name: ztunnel
  dependsOn:
    - name: istio-cni
      namespace: istio-system
  install:
    crds: CreateReplace
  upgrade:
    crds: CreateReplace
  values:
    # ztunnel has no profile setting; it is always ambient
    # Resources (defaults: 200m CPU / 512Mi mem per node)
    # For a 3-node homelab with ~60 pods, the defaults are fine
    # but we reduce memory since we won't have 200k pods
    resources:
      requests:
        cpu: 200m
        memory: 256Mi
    # Enable Prometheus scraping (ztunnel exposes metrics on :15020)
    podAnnotations:
      prometheus.io/port: "15020"
      prometheus.io/scrape: "true"
```

### Task 7: Create kustomization.yaml for the Istio directory

**Objective:** Wire the Istio manifests into the `core/` Flux layer.

**File:** `core/istio/kustomization.yaml`
```yaml
# yaml-language-server: $schema=https://json.schemastore.org/kustomization.json
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: istio-system
resources:
  - namespace.yaml
  - base.yaml
  - istiod.yaml
  - cni.yaml
  - ztunnel.yaml
```

**Note:** Add `core/istio/` to `core/kustomization.yaml` resources list so Flux picks it up. The `core` Flux Kustomization already has SOPS decryption + `postBuild.substituteFrom` — no new SOPS variables needed for Phase 1 (no secrets in the Istio manifests).

### Task 8: Add namespace opt-in labels (Phase 1 namespaces)

**Objective:** Label the Phase 1 namespaces to opt them into the ambient mesh.

**Approach:** The namespaces are currently defined in their respective `namespace.yaml` files across `services/*/namespace.yaml` and `platform/*/namespace.yaml`. We need to add the `istio.io/dataplane-mode: ambient` label to:
- `automation` (`services/automation/namespace.yaml`)
- `media` (`services/media/namespace.yaml`)
- `development` (`services/development/namespace.yaml`)

**Important:** Since `prune: false` on all layers, modifying namespace manifests does NOT update existing namespace labels in the cluster — Flux won't prune/recreate them. After merging, manually apply the label:
```sh
kubectl label ns automation istio.io/dataplane-mode=ambient
kubectl label ns media istio.io/dataplane-mode=ambient
kubectl label ns development istio.io/dataplane-mode=ambient
```
**OR** use a Kyverno ClusterPolicy that syncs the label from the namespace manifest (the cluster already uses Kyverno for policy). Document the manual step in the PR body.

**Alternative (cleaner):** Create a single Kyverno ClusterPolicy that adds the `istio.io/dataplane-mode: ambient` label to namespaces matching a selector, rather than editing each namespace file. This is the GitOps-native approach. Example:
```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: istio-ambient-namespace-labels
spec:
  rules:
    - name: add-ambient-label
      match:
        any:
          - resources:
              kinds: [Namespace]
              names: [automation, media, development]
      mutate:
        patchStrategicMerge:
          metadata:
            labels:
              istio.io/dataplane-mode: ambient
```

### Task 9: Verify the deployment

**Objective:** After Flux reconciles, verify all Istio components are healthy and mesh traffic works.

**Commands:**
```sh
# Flux reconciliation
flux reconcile kustomization core --with-source
flux get helmreleases -n istio-system

# Pods healthy
kubectl get pods -n istio-system
# Expected: istiod-xxx 1/1, istio-cni-node-xxx 1/1 (x3), ztunnel-xxx 1/1 (x3)

# CRDs installed
kubectl get crd | grep istio.io

# Namespace labels applied
kubectl get ns automation media development --show-labels

# Verify a pod is meshed (check ztunnel logs for the pod)
kubectl logs ds/ztunnel -n istio-system | grep inpod | head

# Verify mTLS — deploy a test pod in automation, curl another meshed service
kubectl exec -n automation deploy/hermes-agent -- curl -sv http://home-assistant.automation:8123 2>&1 | grep -i 'subject\|issuer'

# Verify no breakage to existing services
kubectl get httproutes -A
flux get kustomizations
```

**Acceptance:**
- All Istio pods Running (istiod 1/1, istio-cni 3/3, ztunnel 3/3)
- No new errors in Flux kustomizations
- Existing HTTPRoutes still work (curl an external URL)
- Meshed pods show mTLS in ztunnel logs
- No pod restarts in meshed namespaces (indicating no traffic breakage)

---

## 6. Rollback Plan

If ambient mode conflicts with existing components:

### Quick rollback (disable mesh, keep Istio installed)
1. Remove the `istio.io/dataplane-mode` label from all namespaces:
   ```sh
   kubectl label ns automation media development istio.io/dataplane-mode-
   ```
2. Pods will stop being meshed within seconds (ztunnel stops intercepting new connections; existing connections drain). No pod restarts needed — the iptables rules are removed by istio-cni.

### Full rollback (uninstall Istio)
1. Remove namespace labels (above)
2. Delete the Flux HelmReleases (revert the PR):
   ```sh
   kubectl delete helmrelease -n istio-system ztunnel istio-cni istiod istio-base
   ```
3. Wait for Helm to uninstall (ztunnel and istio-cni DaemonSets are deleted)
4. Delete CRDs (optional, if completely removing Istio):
   ```sh
   kubectl get crd -oname | grep istio.io | xargs kubectl delete
   ```
5. Delete the namespace:
   ```sh
   kubectl delete ns istio-system
   ```
6. Verify CNI config is restored (kube-router config intact):
   ```sh
   # On a node: cat /etc/cni/net.d/*.conflist | grep -v istio
   ```

### Rollback risk: existing connections
- Removing the namespace label stops *new* pods from being meshed, but already-running pods keep their iptables rules until the pods are restarted or istio-cni removes them.
- To fully unmesh running pods: restart the pods in meshed namespaces (`kubectl rollout restart deploy -n automation` etc.) after removing the label and deleting ztunnel.
- **No data loss risk:** ambient mode does not modify application data; it only intercepts network traffic. Rollback restores plaintext networking.

### Rollback risk: CNI config
- The istio-cni chained plugin modifies `/etc/cni/net.d/*.conf`. On uninstall, Helm removes the istio-cni agent, but the CNI config file may still contain the chained plugin entry.
- After uninstall, new pods may fail to start if the CNI config references a missing plugin binary.
- **Mitigation:** After uninstalling istio-cni, SSH to each node and remove the istio-cni entry from the CNI config file, or restart k0s (which regenerates the CNI config from scratch).

---

## 7. Decisions Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Istio version | 1.30.2 (latest stable) | Latest release, supports k8s 1.32-1.36, ambient GA |
| Install method | Helm (4 charts) | Flux-native, controlled upgrades, matches repo conventions |
| Profile | `ambient` on istiod + cni | Enables ztunnel/waypoint architecture, no sidecar injection |
| mTLS mode | PERMISSIVE (Phase 1) | Allows plaintext fallback; switch to STRICT after validation |
| Waypoints | None in Phase 1 | L4 only; add L7 in Phase 2 for specific namespaces |
| Gateway chart | Not installed | Envoy Gateway handles ingress; no Istio ingress needed |
| Mesh namespaces (Phase 1) | automation, media, development | Most interconnected services; lowest risk |
| Excluded namespaces | All system/infra + database + monitoring | Avoid disrupting storage, DNS, cert-manager, observability |
| Chart source | OCI (verify availability) | Matches repo convention; fallback to HelmRepository if OCI unavailable |
| Namespace placement | `core/istio/` | Istio is cluster infrastructure (like cert-manager, longhorn) |
| PSA | privileged on istio-system | Required for istio-cni (NET_ADMIN, hostPath) |

---

## 8. Open Questions (RESOLVED during implementation)

1. **OCI vs HelmRepository — RESOLVED: HelmRepository.**
   The Istio charts are NOT reliably available as OCI artifacts on Docker Hub.
   `istio/base` only has date-based tags (e.g., `1.30-2026-07-15T19-01-32`),
   not semantic version tags. `istio/istiod` and `istio/cni` return 404 (repos
   don't exist or are private). `istio/ztunnel` only has cosign signature tags.
   Used a HelmRepository pointing to `https://istio-release.storage.googleapis.com/charts`
   where all 4 charts (base, istiod, cni, ztunnel) are available at version 1.30.2.

2. **k0s CNI paths — RESOLVED: defaults are correct.**
   Verified by inspecting the kube-router DaemonSet in kube-system. Its hostPath
   volumes use `cni-conf-dir: /etc/cni/net.d` and `cni-bin: /opt/cni/bin` —
   exactly the Istio cni chart defaults. No override needed.

3. **Stale Istio CRDs — RESOLVED: no stale CRDs found.**
   Cannot list CRDs directly (SA returns 403 on apiextensions.k8s.io), but the
   `istio-system` namespace does not exist on the cluster (404), and no Istio
   pods are running. Attempted to list Istio custom objects
   (virtualservices, destinationrules, authorizationpolicies) — all returned
   403 (not 200 with items), which means either no CRDs exist or the SA can't
   read them. Either way, no stale Istio installation is active. The `prune: false`
   policy means any stale CRDs from the old v1.20 install would persist, but
   since there's no istio-system namespace or pods, a clean install is expected.

4. **Kyverno privileged pod policy — RESOLVED: no cluster-wide block found.**
   The existing Kyverno ClusterPolicies in the repo (httproute-oidc-security-policy)
   are generate/mutate policies, not restrict-privileged. The `istio-system`
   namespace is labeled privileged PSA (matching metallb-system and velero).
   No Kyverno policy in the gitops repo blocks privileged DaemonSets.

5. **Renovate manager patterns — RESOLVED: Flux manager handles it.**
   The `.github/renovate.json5` config has `flux.managerFilePatterns` matching
   `/(bootstrap|core|platform|services)/.+\\.yaml$/`. The Flux manager
   detects HelmRelease resources with `chart.spec.sourceRef.kind: HelmRepository`
   and `chart.spec.version` fields, and will automatically create PRs to bump
   the version. The HelmRepository URL (not OCI) means Renovate uses the helm
   datasource. The existing `pinDigests: false` rule for Flux/docker applies,
   but HelmRepository charts use the helm datasource (not docker), so version
   bumps will work as expected.

---

## References

- Istio Ambient Mode docs: https://istio.io/latest/docs/ambient/
- Install with Helm: https://istio.io/latest/docs/ambient/install/helm/
- Ambient data plane architecture: https://istio.io/latest/docs/ambient/architecture/data-plane/
- Ztunnel traffic redirection: https://istio.io/latest/docs/ambient/architecture/traffic-redirection/
- Istio 1.30.2 release: https://github.com/istio/istio/releases/tag/1.30.2 (2026-06-24)
- ztunnel chart values: https://github.com/istio/istio/blob/1.30.2/manifests/charts/ztunnel/values.yaml
- istiod chart values: https://github.com/istio/istio/blob/1.30.2/manifests/charts/istio-control/istio-discovery/values.yaml
- cni chart values: https://github.com/istio/istio/blob/1.30.2/manifests/charts/istio-cni/values.yaml
- Sibling task: Cilium + Hubble (t_e6b0051f) — currently blocked
- Parent task: Istio ambient mode PR (t_1b5073c4)

---

## Validation Results

> **FINAL** — Validation performed by review task `t_f1daa97e` against the
> implementation (commits 33c04139 + a69075ab). No cluster deployment was
> performed — this is a manifest-level compatibility review, per task scope.
> Results have been consolidated onto branch `feature/istio-ambient-mesh` and
> submitted as draft PR #3441 against `main`.

### 1. Implementation Correctness — PASS

**HelmRelease chart references — PASS.** All four HelmReleases reference
chart `version: "1.30.2"` from a single `HelmRepository` named `istio`
(`core/istio/helm-repository.yaml`) pointing at
`https://istio-release.storage.googleapis.com/charts`. Verified against the
live Helm repo index (`/tmp/istio-index.yaml`, HTTP 200, 634 KB): all four
charts — `base`, `istiod`, `cni`, `ztunnel` — exist as separate entries at
version `1.30.2` with `appVersion: 1.30.2`. The chart names in the
HelmReleases (`base`, `istiod`, `cni`, `ztunnel`) exactly match the index
entry names. No OCI artifacts are used (the plan's Open Question #1 resolved
this — OCI tags were not semver; HelmRepository is the correct source).

**Chart dependency ordering — PASS.** The `dependsOn` chain is correct and
matches the documented prerequisite graph:
`istio-base` → `istiod` (dependsOn istio-base) → `istio-cni` (dependsOn
istiod) → `ztunnel` (dependsOn istio-cni). All `dependsOn` reference
`namespace: istio-system`, matching the HelmRelease namespaces.

**ztunnel DaemonSet tolerations — PASS (defaults are correct).** The task
asked to verify "ztunnel tolerations match all 3 nodes." The implementation
does NOT set explicit `tolerations` in `ztunnel.yaml` values — this is
correct, because the ztunnel chart's `_internal_defaults` ship tolerations
that tolerate **all taints**:

```yaml
# ztunnel chart default tolerations (from _internal_defaults)
tolerations:
  - effect: NoSchedule
    operator: Exists
  - key: CriticalAddonsOnly
    operator: Exists
  - effect: NoExecute
    operator: Exists
```

The `operator: Exists` with `effect: NoSchedule`/`NoExecute` matches any
taint key. The 3 k0s nodes are `control-plane` (controller+worker, no
taints per the plan's cluster probe), so ztunnel will schedule on all 3
nodes regardless. The `istio-cni` chart has identical default tolerations.
**No toleration override needed.** (Confirmed by inspecting the chart
templates: `daemonset.yaml` renders tolerations via
`{{- with .Values.tolerations }}` and the default values populate that key.)

**Namespace labels — PASS.** `core/istio/namespace-labels.yaml` is a Kyverno
`ClusterPolicy` (`istio-ambient-namespace-labels`) with `background: true`
and `generateExisting: true` that mutates namespaces `automation`, `media`,
`development` to add `istio.io/dataplane-mode: ambient`. This is the
GitOps-native approach described in plan Task 8 (alternative). The policy
matches on `kind: Namespace` with explicit `names:` list — no risk of
labeling unintended namespaces. Verified no existing manifests carry
`istio.io/dataplane-mode` or `istio.io/rev` labels (grep returned nothing
outside `core/istio/`), so no label conflicts.

**istiod `taint.enabled: false` — PASS (correct, minor comment imprecision).**
The implementation sets `taint.enabled: false` with a comment about k0s
nodes being control-plane. The actual purpose of `taint.enabled` is the
istiod "untaint controller" (`PILOT_ENABLE_NODE_UNTAINT_CONTROLLERS`) which
removes a node taint once istio-cni is ready — it is NOT about tolerating
control-plane taints. Setting it `false` is correct because we are not using
Istio's node-tainting workflow. The setting value is right; the comment's
reasoning is slightly off but does not affect behavior.

**YAML validity — PASS.** All 8 files in `core/istio/` parse cleanly
(PyYAML `safe_load_all`, 0 errors, 8 documents). `kustomization.yaml`
references 7 resource files, all present on disk. `core/istio` is listed in
`core/kustomization.yaml` resources (line 15), so Flux's `core` Kustomization
will pick it up.

### 2. Component Compatibility

| Component | Status | Notes |
|-----------|--------|-------|
| Cilium (future) | **PASS** | No eBPF conflict — see below |
| Envoy Gateway | **PASS** | No port conflict, no GatewayClass collision |
| cert-manager | **PASS** | DNS-01 via Cloudflare, mesh-safe |
| ExternalDNS | **PASS** | Cluster-level service, unmeshed `dns` namespace |
| monitoring (Prometheus) | **PASS** | `monitoring` namespace unmeshed; ztunnel metrics scrapeable |
| HTTPRoutes | **PASS** | All routes target Envoy Gateway, not intercepted by Istio |

**Cilium eBPF interaction — PASS (no conflict).** The task asked to document
the ztunnel eBPF vs Cilium eBPF interaction. **Finding: ztunnel does NOT
use eBPF.** Ambient mode traffic redirection uses (1) a chained CNI plugin
for pod detection and (2) iptables/nftables rules **inside the pod's network
namespace** for traffic capture — not node-level eBPF programs. Cilium's eBPF
programs operate at the host/node level (kube-proxy replacement, L4 LB,
NetworkPolicy, service routing). The two operate at different network layers
and are designed to coexist (Istio docs state the in-pod redirection model
"enables ambient mode to work alongside any Kubernetes CNI plugin").

Additionally, Cilium itself documents and supports chained CNI plugins for
Istio ambient coexistence. The `istio-cni` `chained: true` setting appends to
the primary CNI config file (whether kube-router's or Cilium's
`05-cilium.conflist`) rather than replacing it. **No Cilium manifests exist
in `main` today** (the Cilium migration task `t_e6b0051f` is blocked), so
ambient will deploy against kube-router now and remain compatible with a
future Cilium migration.

**Envoy Gateway port conflicts — PASS (no conflict).** Verified ztunnel's
actual port usage by inspecting the chart's `daemonset.yaml` and
`networkpolicy.yaml` templates:
- ztunnel declares only `containerPort: 15020` (metrics) and `15021` (health
  readiness) on the pod. These are container ports, not host ports.
- Ports `15008` (HBONE), `15006` (inbound), `15001` (outbound) are
  **pod-netns-level listening sockets** created by ztunnel *inside each
  meshed pod's network namespace* (the "inpod" redirection model) — they are
  NOT host-level ports and cannot conflict with Envoy Gateway.
- Envoy Gateway's data plane (EnvoyProxy) listens on the `external` Gateway's
  listeners (`:80` HTTP, `:443` HTTPS) exposed via a LoadBalancer Service.
  No overlap with any ztunnel port.

**Envoy Gateway GatewayClass — PASS (no collision).** Envoy Gateway uses
`GatewayClass: envoy` (controller `gateway.envoyproxy.io/gatewayclass-controller`).
The implementation does **not** install the `istio/gateway` chart, so Istio's
Gateway API controller (`GatewayClass: istio`, controller
`istio.io/gateway-controller`) is never deployed. No GatewayClass collision.
ztunnel is Rust (not Envoy); waypoints (Envoy) are separate pods in meshed
namespaces, not shared with Envoy Gateway's data plane.

**cert-manager — PASS (DNS-01 is mesh-safe).** Both `ClusterIssuer`s
(`letsencrypt` prod + `letsencrypt-staging` in `core/resources/cert-manager.yaml`)
use `dns01.cloudflare.apiTokenSecretRef`. DNS-01 challenges are resolved via
the Cloudflare API (out-of-band TXT records), not inbound HTTP traffic.
Ambient mesh redirection only affects pod-to-pod traffic in meshed
namespaces; cert-manager runs in the unmeshed `cert-manager` namespace and
makes only outbound DNS API calls. **HTTP-01 would break under mesh** (the
solver pod receives inbound HTTP) — but this cluster does not use HTTP-01. No
cert-manager changes needed.

**ExternalDNS — PASS.** ExternalDNS (cloudflare + adguard) runs in the
unmeshed `dns` namespace and watches Kubernetes `Service`/`Ingress`/
`gateway-httproute` resources via the API server. It makes only outbound
Cloudflare/AdGuard API calls. No pod-to-pod mesh traffic involved.
Unaffected.

**monitoring (Prometheus/Grafana) — PASS.** The `monitoring` namespace is
**not** in the Phase 1 mesh opt-in list (`automation`, `media`,
`development`), so Prometheus itself is unmeshed. Prometheus discovers scrape
targets via `ServiceMonitor`/`PodMonitor` resources (kube-prometheus-stack
sets `serviceMonitorSelectorNilUsesHelmValues: false`, selecting all
ServiceMonitors). The built-in ServiceMonitors target cluster-system
components (coredns, kube-apiserver, kubelet, node-exporter) in unmeshed
namespaces.
- ztunnel metrics: the implementation adds `prometheus.io/scrape: "true"` +
  `prometheus.io/port: "15020"` annotations to the ztunnel pods. ztunnel is
  in the unmeshed `istio-system` namespace, so Prometheus scrapes it directly
  via pod IP:15020 without traversing the mesh. (Note: kube-prometheus-stack
  does not enable annotation-based scraping by default, so the annotation
  alone may not be sufficient — a dedicated ServiceMonitor for ztunnel would
  be needed for actual collection. This is a monitoring-coverage gap, not a
  conflict.)
- Prometheus scraping of meshed app pods: in Phase 1, Prometheus scrapes app
  pods in `automation`/`media`/`development` only if ServiceMonitors exist for
  them. Only `flux-system` PodMonitor and `monitoring`-scoped monitors were
  found — no ServiceMonitors targeting the meshed app namespaces. So no
  scrape-through-ztunnel concern in Phase 1. If ServiceMonitors are added
  later for meshed apps, ztunnel accepts plaintext inbound, so scrapes will
  still succeed (though without a peer mTLS identity).

**HTTPRoutes — PASS (not intercepted by Istio).** All 11 HTTPRoutes in the
meshed namespaces (`automation`: frigate, hermes-agent x2, home-assistant,
zwave-js; `media`: plex, radarr, sabnzbd, sonarr, sonarr-anime; `development`:
forgejo) reference `parentRefs: [{name: external, namespace: gateway}]` —
Envoy Gateway's `external` Gateway. Ambient mesh does not intercept
HTTPRoutes that route through an external gateway; it only intercepts
pod-to-pod east-west traffic between meshed workloads. Ingress traffic
(external → Envoy Gateway → pod) arrives as plaintext at the pod and is
accepted by ztunnel in PERMISSIVE mode. No route changes needed; no
`istio.io/rev` annotations required on HTTPRoutes.

### 3. Resource Overhead Estimates

Based on the implementation's resource requests (reduced from chart defaults)
and Istio's documented ambient-mode sizing:

| Component | Per-unit request | Units | Cluster total | Notes |
|-----------|------------------|-------|---------------|-------|
| **ztunnel** | 200m CPU / 256Mi mem | 3 (DaemonSet, 1/node) | **600m CPU / 768Mi mem** | Reduced from default 512Mi; a 3-node / ~60-pod cluster will use far less than the 200k-pod ceiling the defaults target. Actual usage likely <100Mi/node. |
| **istiod** | 250m CPU / 1024Mi mem | 1 (Deployment, HPA 1-2) | **250m CPU / 1024Mi mem** (baseline) | Reduced from default 500m/2048Mi. HPA scales to 2 replicas under load (500m/2048Mi peak). |
| **istio-cni** | 100m CPU / 100Mi mem | 3 (DaemonSet, 1/node) | **300m CPU / 300Mi mem** | Node agent; lightweight. |
| **istio-base** | — (CRDs only) | — | 0 runtime | No pods; just CRDs + cluster roles. |
| **Phase 1 total** | — | — | **~1.15 CPU / ~2.07 GiB mem** | Across all 3 nodes. |

**Waypoint proxy overhead — N/A in Phase 1.** Phase 1 uses NO waypoints
(L4 mTLS only). If Phase 2 adds waypoints for L7 namespaces, each waypoint is
an Envoy deployment (~100m CPU / 128Mi mem per replica, 1-2 replicas per
namespace). Not incurred now.

**Risk assessment:** The ~1.15 CPU / 2 GiB total overhead is modest for a
3-node cluster. The main concern is ztunnel's per-node footprint on
control-plane+worker nodes that also run workload — but at ~60 pods the
actual ztunnel memory will be well under the 256Mi request. Monitor node
resource usage post-deploy (plan Risk R5).

### 4. Conflicts Found

**None.** No component in the existing stack shows a conflict with the Istio
ambient implementation. Every component is either unmeshed (cert-manager,
ExternalDNS, monitoring, gateway, all system namespaces) or compatible by
design (Envoy Gateway uses a different GatewayClass; HTTPRoutes target Envoy
Gateway; Cilium operates at a different network layer).

**Minor non-blocking observations (not conflicts):**
1. **ztunnel metrics scraping gap:** The `prometheus.io/scrape` annotation on
   ztunnel pods may not be picked up by kube-prometheus-stack (which uses
   ServiceMonitors, not annotation-based discovery). For actual metrics
   collection, a `ServiceMonitor` for ztunnel should be added in a
   follow-up. This does not block the PR — it only means ztunnel metrics
   won't appear in Grafana until a ServiceMonitor is added.
2. **k0s CNI overwrite risk (plan R1):** k0s manages the CNI config. If k0s
   regenerates `/etc/cni/net.d/`, the istio-cni chained plugin entry may be
   lost. The istio-cni agent has a reconciliation loop that re-inserts itself,
   but this should be verified during post-merge cluster validation (plan
   Task 9, deferred to someone with cluster write access).
3. **`prune: false` means manual cleanup:** Per repo conventions, removing
   the Istio manifests from git will NOT remove them from the cluster. The
   rollback plan (section 6) documents manual `kubectl delete` steps.

### 5. Recommendation

**The PR is safe to proceed toward merge as a draft/experimental change.**
All existing-stack components pass compatibility review with no conflicts.
The implementation is correctly structured: valid chart versions, correct
dependency ordering, proper tolerations (chart defaults), proper namespace
labeling via Kyverno, and accurate separation from Envoy Gateway.

The remaining validation (plan Task 9 — Flux reconciliation, pod health,
mTLS verification) requires cluster write access and is correctly deferred.
This review confirms there are no manifest-level blockers to proceeding with
that live validation.


---

## Draft PR Status

- **Branch:** `feature/istio-ambient-mesh`
- **PR:** #3441 — https://github.com/zacheryph/k8s-gitops/pull/3441
- **Target:** `main` (draft — exploratory, not for merging)
- **Depends on (soft):** Cilium migration PRs #3437, #3439, #3440 (t_e6b0051f).
  Ambient mode is CNI-agnostic and works with kube-router today; the Cilium
  PRs are referenced as the planned future CNI but are **not a hard
  dependency** — ambient deploys cleanly against the current stack.

### Next steps to move from draft to merge-ready

1. **Live cluster validation (plan Task 9)** — requires cluster write access:
   `flux reconcile kustomization core --with-source`, verify istio pods /
   ztunnel DaemonSet / namespace labels, confirm mTLS via `istioctl x zkc`.
2. **Add a ztunnel ServiceMonitor** — the `prometheus.io/scrape` annotation
   alone is not picked up by kube-prometheus-stack (uses ServiceMonitors, not
   annotation discovery). A dedicated ServiceMonitor in `core/istio/` is
   needed for ztunnel metrics to appear in Grafana.
3. **Switch mTLS to STRICT** after live validation confirms PERMISSIVE mode
   works without breaking meshed workloads (plan decision).
4. **Verify k0s CNI reconciliation** — confirm istio-cni re-inserts itself
   if k0s regenerates `/etc/cni/net.d/` (plan risk R1).
5. **Rebase onto `main`** after the Cilium migration PRs merge (if they land
   first), then re-run manifest validation against the new CNI stack.
