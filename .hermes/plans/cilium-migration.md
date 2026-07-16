# Cilium Migration Implementation Plan

> **Reference document for the Cilium + Hubble migration.**
> This plan is the authoritative guide for the entire change: component
> replacement, migration order, k0sctl steps, BGP remapping, risk areas,
> rollback, and verification. The task-by-task implementation plan lives at
> `.hermes/plans/2026-07-15_091306-cilium-hubble.md`.

**Goal:** Replace four networking stack components (kube-router, kube-proxy,
Envoy Gateway, MetalLB) with Cilium's eBPF data plane, BGP control plane,
Gateway API implementation, and Hubble observability — across a 3-node k0s
homelab cluster.

**Cluster:** `k0s-inwin` — 3 controller+worker nodes (k0s-inwin-01/02/03 at
10.72.13.11–13), k0s v1.35.3+k0s.0, Kubernetes v1.36.2. All nodes are
control plane + worker (`noTaints: true`).

**Branch:** `feat/cilium-hubble`

---

## 1. Component Replacement Matrix

| Layer | Before (current) | After (Cilium) | Mechanism |
|-------|-----------------|----------------|-----------|
| CNI | kube-router (k0s default, built on GoBGP) | Cilium eBPF CNI | `spec.network.provider: custom` in k0s config disables k0s-managed CNI; Cilium DaemonSet installs CNI binary + config on each node |
| kube-proxy | k0s kube-proxy in IPVS mode (`mode: ipvs`, `strictARP: true`) | Cilium eBPF kube-proxy replacement | `kubeProxyReplacement: true` in Cilium Helm values; `spec.network.kubeProxy.disabled: true` in k0s config removes kube-proxy system component |
| LoadBalancer IP advertisement | MetalLB BGP (FRR-based, `BGPPeer` + `BGPAdvertisement` + `IPAddressPool`) | Cilium BGP control plane + `CiliumLoadBalancerIPPool` | `bgpControlPlane.enabled: true`; CRDs: `CiliumBGPClusterConfig`, `CiliumBGPPeerConfig`, `CiliumBGPAdvertisement`, `CiliumLoadBalancerIPPool` |
| Ingress / Gateway API | Envoy Gateway (GatewayClass `envoy`, controller `gateway.envoyproxy.io/gatewayclass-controller`) | Cilium Gateway API (GatewayClass `cilium`, controller `io.cilium/gateway-controller`) | `gatewayAPI.enabled: true` + `gatewayClass.create: true`; Gateway `external` keeps same name/namespace so HTTPRoutes need no changes |
| L2 load balancing | k0s `nodeLocalLoadBalancing` (EnvoyProxy type) | Removed — Cilium handles service load balancing via eBPF | `nodeLocalLoadBalancing` block removed from k0s config; Cilium's eBPF data plane handles all service routing |
| Observability | Envoy Proxy metrics (PodMonitor) + MetalLB metrics | Hubble (Relay + UI) + Cilium agent/operator Prometheus | `hubble.enabled: true`, `relay.enabled: true`, `ui.enabled: true`; ServiceMonitors for Cilium agent, operator, and Hubble Relay |

**What does NOT change:**
- CoreDNS in `kube-system` — k0s-managed, untouched
- cert-manager ClusterIssuer `letsencrypt` (DNS-01 Cloudflare) — unchanged
- ExternalDNS (AdGuard + Cloudflare instances) — consuming `gateway-httproute` sources, no changes needed
- All HTTPRoute manifests — they reference `parentRefs: [{name: external, namespace: gateway}]`, which remains valid because the Gateway resource keeps the same name and namespace
- `config/secrets.yaml` SOPS-encrypted variables — same `${VAR}` substitution, no new variables needed

---

## 2. Migration Order and Rationale

The migration follows a strict order. Each phase has a specific reason for its
position — violating this order causes traffic loss or split-brain networking.

### Phase 1: Merge the GitOps branch (manifests only, no cluster changes)

**Action:** Merge `feat/cilium-hubble` to `main`. Flux reconciles.

**What Flux applies:**
1. Cilium HelmRelease + OCIRepository (`core/cilium.yaml`)
2. Cilium BGP CRDs + CiliumLoadBalancerIPPool (`core/resources/cilium-bgp.yaml`)
3. Updated Gateway with `gatewayClassName: cilium` (`platform/gateway/gateway.yaml`)
4. Hubble UI HTTPRoute (if added)

**What Flux does NOT apply yet (blocked by ordering):**
- The k0s config change (`config/cluster.yaml`) is NOT a Kubernetes manifest — it's a k0sctl inventory file. Flux does not apply it. It takes effect only when an operator runs `k0sctl apply`.

**Why the Cilium HelmRelease must be applied before the k0s spec change:**
Cilium's DaemonSet must be running and Ready on all nodes BEFORE k0s stops
providing kube-router and kube-proxy. If you change `spec.network.provider` to
`custom` first (via `k0sctl apply`), k0s removes kube-router and kube-proxy
immediately. Without Cilium already running, the cluster has no CNI and no
service routing — pods lose network, `kubectl exec` fails, and the cluster
becomes unreachable until Cilium starts (which itself needs the CNI to
function in some configurations). By applying the Cilium HelmRelease first,
the Cilium DaemonSet starts alongside the existing kube-router. Both run
simultaneously during the transition window, and Cilium is ready to take over
the moment kube-router is removed.

**Why old HelmReleases (MetalLB, Envoy Gateway) are removed last:**
During the transition, both the old and new data planes coexist. Removing
MetalLB or Envoy Gateway before Cilium is fully operational would leave
LoadBalancer services without IP advertisement and HTTPRoutes without a
Gateway implementation. The removal happens in Phase 5, after all nodes have
been migrated and Cilium is verified. Because `prune: false` is set on every
layer, removing a file from git does NOT delete the resource from the cluster
— the HelmReleases must be manually uninstalled with `helm uninstall` or
`kubectl delete`.

### Phase 2: Verify Cilium is running alongside the old stack

**Action:** Confirm Cilium agents are Ready on all nodes.

```sh
kubectl -n kube-system get pods -l k8s-app=cilium -o wide
kubectl -n kube-system get pods -l app.kubernetes.io/name=cilium-operator
```

**Expected:** All Cilium agents and the cilium-operator are Ready. At this
point Cilium is running but NOT handling traffic — kube-router is still the
active CNI, kube-proxy is still handling service routing, MetalLB is still
advertising BGP, and Envoy Gateway is still serving HTTPRoutes.

### Phase 3: Node-by-node migration (drain → k0sctl apply → uncordon)

**Action:** For each node, one at a time:
1. Drain the node
2. Apply the k0s config change (disables kube-router + kube-proxy on that node)
3. Uncordon and verify Cilium takes over

See Section 3 for the exact commands and drain order.

### Phase 4: Verify Cilium data plane

**Action:** After all 3 nodes are migrated:
- Cilium BGP is peered with OPNsense (Section 7 checklist)
- Cilium Gateway API is serving HTTPRoutes
- Hubble UI is accessible
- All LoadBalancer services have external IPs

### Phase 5: Remove old components

**Action:** Manually remove the old data plane components (because `prune:
false`):

```sh
# Remove kube-router DaemonSet (k0s no longer manages it)
kubectl delete daemonset -n kube-system kube-router

# Remove MetalLB
kubectl delete helmrelease -n metallb-system metallb
kubectl delete ocirepository -n metallb-system metallb
kubectl delete namespace metallb-system

# Remove Envoy Gateway
kubectl delete helmrelease -n gateway envoy-gateway
kubectl delete ocirepository -n gateway envoy-gateway
kubectl delete envoyproxy -n gateway envoy
kubectl delete gatewayclass envoy
```

**Why last:** These components were still serving traffic during the migration.
Only after Cilium is verified as the active data plane is it safe to remove
them. Removing them earlier creates a gap with no traffic handling.

---

## 3. k0sctl Apply Steps

### Config change

`config/cluster.yaml` changes from:

```yaml
spec:
  network:
    kubeProxy:
      mode: ipvs
      ipvs:
        strictARP: true
    nodeLocalLoadBalancing:
      enabled: true
      type: EnvoyProxy
```

to:

```yaml
spec:
  network:
    # Custom CNI provider — Cilium is installed via GitOps (core/cilium.yaml)
    # and manages the CNI, kube-proxy replacement, and service load balancing.
    provider: custom
    kubeProxy:
      disabled: true
```

**What this does:**
- `provider: custom` — k0s stops deploying and managing the kube-router CNI. It does not install any CNI plugin; the Cilium DaemonSet (already running from Phase 1) takes over as the active CNI.
- `kubeProxy.disabled: true` — k0s removes the kube-proxy system component. Cilium's `kubeProxyReplacement: true` (configured in the HelmRelease) takes over service routing via eBPF.
- `nodeLocalLoadBalancing` block removed — this was an EnvoyProxy-based NLLB that conflicts with Cilium's eBPF kube-proxy replacement. Cilium handles all service load balancing internally.
- `strictARP: true` removed — this was MetalLB-specific (needed for FRR's ARP behavior). Cilium BGP does not use ARP for LB IP advertisement; it uses eBPF and BGP.

### Node drain order

The cluster has 3 controller+worker nodes. Since all nodes are both control
plane and worker, the drain order matters for API server availability. Drain
one node at a time, always keeping at least 2 nodes running (etcd quorum).

**Drain order: k0s-inwin-03 → k0s-inwin-02 → k0s-inwin-01**

Rationale: drain the last node first, the middle node second, and the first
node (the initial bootstrap controller, likely the etcd leader) last. This
minimizes etcd leader changes — the leader is the last to be drained. If
k0s-inwin-01 is the etcd leader, draining it last avoids a leader election
during the migration. If it is not the leader, the order is less critical but
still safe.

**Per-node procedure (repeat for each node):**

```sh
# 1. Drain the node — ignore daemonsets (Cilium, Longhorn, etc.), delete emptyDir data
kubectl drain k0s-inwin-03 --ignore-daemonsets --delete-emptydir-data

# 2. Apply the k0s config change to this node
#    k0sctl reads config/cluster.yaml, connects via SSH, and reconfigures the node.
#    The provider:custom + kubeProxy.disabled:true takes effect on this node only.
k0sctl apply --config config/cluster.yaml

# 3. Wait for the node to come back online
kubectl uncordon k0s-inwin-03
kubectl wait --for=condition=Ready node/k0s-inwin-03 --timeout=120s

# 4. Verify Cilium is the active CNI on this node
kubectl -n kube-system get pods -l k8s-app=cilium --field-selector spec.nodeName=k0s-inwin-03
# All pods should be Running

# 5. Verify pods on this node have IPs (Cilium IPAM is working)
kubectl get pods -A --field-selector spec.nodeName=k0s-inwin-03 -o wide | grep -v 'NAME'

# 6. Before draining the next node, verify core services are still reachable
#    (API server, DNS, at least one HTTPRoute)
kubectl get nodes
kubectl -n kube-system get pods -l k8s-app=kube-dns
```

**Critical: do not drain the next node until the current node is fully
operational with Cilium.** If a node fails to come back with Cilium, stop the
migration and troubleshoot before proceeding. See Section 6 (Rollback).

### Why drain is necessary

`k0sctl apply` reconfigures the kubelet on the target node. The kubelet
restarts, and the CNI plugin switches from kube-router to Cilium. Existing
pods on the node lose their network interfaces briefly. Draining ensures pods
are safely rescheduled to other nodes (where Cilium is already running or
kube-router is still active) before the reconfiguration. Without draining,
pods would be abruptly disconnected and may not recover.

---

## 4. BGP Peer Remapping

### MetalLB FRR config (before)

The MetalLB BGP configuration used three CRs in `metallb-system` namespace:

**`core/resources/metallb.yaml` (deleted):**
```yaml
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: services-block
  namespace: metallb-system
spec:
  addresses:
  - ${LOAD_BALANCER_POOL}       # e.g., 10.72.16.0/24
  autoAssign: true
  avoidBuggyIPs: true
---
apiVersion: metallb.io/v1beta2
kind: BGPPeer
metadata:
  name: router
  namespace: metallb-system
spec:
  peerAddress: ${ROUTER_IP}     # OPNsense router IP (from SOPS)
  peerASN: ${ROUTER_ASN}        # OPNsense ASN (from SOPS)
  peerPort: 179
  myASN: ${CLUSTER_ASN}         # cluster's ASN (from SOPS)
  holdTime: 1m30s
---
apiVersion: metallb.io/v1beta1
kind: BGPAdvertisement
metadata:
  name: router-bgp
  namespace: metallb-system
spec:
  ipAddressPools:
  - services-block
  peers:
  - router
```

**Variable mapping (SOPS → Cilium):**

| MetalLB field | SOPS variable | Cilium field |
|---------------|---------------|--------------|
| `BGPPeer.peerAddress` | `${ROUTER_IP}` | `CiliumBGPClusterConfig.bgpInstances[0].peers[0].peerAddress` |
| `BGPPeer.peerASN` | `${ROUTER_ASN}` | `CiliumBGPClusterConfig.bgpInstances[0].peers[0].peerASN` |
| `BGPPeer.myASN` | `${CLUSTER_ASN}` | `CiliumBGPClusterConfig.bgpInstances[0].localASN` |
| `BGPPeer.peerPort` | 179 (literal) | `CiliumBGPPeerConfig.transport.peerPort` |
| `BGPPeer.holdTime` | 1m30s (literal) | `CiliumBGPPeerConfig.timers.holdTimeSeconds: 90` |
| `IPAddressPool.addresses` | `${LOAD_BALANCER_POOL}` | `CiliumLoadBalancerIPPool.blocks[0].cidr` |

### Cilium BGP config (after)

The equivalent Cilium configuration uses four CRs (all in `core/resources/cilium-bgp.yaml`):

```yaml
apiVersion: cilium.io/v2
kind: CiliumBGPClusterConfig
metadata:
  name: router
spec:
  nodeSelector:
    matchLabels:
      kubernetes.io/os: linux       # All nodes run BGP (was: MetalLB speaker on all nodes)
  bgpInstances:
    - name: router
      localASN: ${CLUSTER_ASN}       # Was: BGPPeer.myASN
      peers:
        - name: router
          peerAddress: ${ROUTER_IP}  # Was: BGPPeer.peerAddress
          peerASN: ${ROUTER_ASN}     # Was: BGPPeer.peerASN
          peerConfigRef:
            name: router             # References CiliumBGPPeerConfig below
---
apiVersion: cilium.io/v2
kind: CiliumBGPPeerConfig
metadata:
  name: router
spec:
  transport:
    peerPort: 179                   # Was: BGPPeer.peerPort
  timers:
    holdTimeSeconds: 90             # Was: BGPPeer.holdTime (1m30s = 90s)
    keepAliveTimeSeconds: 30        # Cilium default; MetalLB used FRR default (23s)
  families:
    - afi: ipv4
      safi: unicast
      advertisements:
        matchLabels:
          bgp.routine.sh/advertise: "true"  # Links to CiliumBGPAdvertisement below
---
apiVersion: cilium.io/v2
kind: CiliumBGPAdvertisement
metadata:
  name: services
  labels:
    bgp.routine.sh/advertise: "true"  # Must match the matchLabels above
spec:
  advertisements:
    - advertisementType: Service
      service:
        addresses:
          - LoadBalancerIP           # Advertise the LoadBalancer IP (was: ipAddressPools reference)
        aggregationLengthIPv4: 32    # One route per LB IP (no route aggregation)
---
apiVersion: cilium.io/v2
kind: CiliumLoadBalancerIPPool
metadata:
  name: services-block               # Same name as old IPAddressPool (for clarity)
spec:
  allowFirstLastIPs: "No"            # Was: avoidBuggyIPs: true (equivalent)
  blocks:
    - cidr: ${LOAD_BALANCER_POOL}    # Was: IPAddressPool.addresses[0]
```

### Key differences from MetalLB

1. **BGP speaker location:** MetalLB ran a dedicated speaker pod in
   `metallb-system`. Cilium's BGP control plane runs inside the Cilium agent
   DaemonSet on every node (selected by `nodeSelector`). Every node
   establishes a BGP session to OPNsense — same as MetalLB's default
   `externalTrafficPolicy: Local` behavior where each node advertises only the
   LB IPs it's running a service pod for.

2. **Advertisement linking:** MetalLB linked advertisements to peers via
   `ipAddressPools` + `peers` fields on the `BGPAdvertisement` CR. Cilium
   links via labels — `CiliumBGPPeerConfig.families[0].advertisements.matchLabels`
   must match the `CiliumBGPAdvertisement` metadata labels. The label key
   `bgp.routine.sh/advertise` follows the repo's `routine.sh` annotation
   convention (same namespace as `backup.routine.sh/`, `dns.routine.sh/`).

3. **Hold time:** MetalLB's `holdTime: 1m30s` translates to
   `holdTimeSeconds: 90`. Cilium also requires `keepAliveTimeSeconds`; 30 is
   the recommended default (one-third of hold time).

4. **IP pool:** `CiliumLoadBalancerIPPool` replaces `IPAddressPool`. The
   `allowFirstLastIPs: "No"` field is the equivalent of MetalLB's
   `avoidBuggyIPs: true` — both avoid assigning the network and broadcast
   addresses of the pool CIDR.

5. **Aggregation:** `aggregationLengthIPv4: 32` means each LoadBalancer IP
   gets its own /32 route advertised. MetalLB's default was also per-IP. Do
   not increase aggregation unless OPNsense is configured to handle
   aggregated routes.

---

## 5. Risk Areas

### 5.1 Cilium Gateway API maturity: TLS passthrough and header modification

**Risk:** Cilium's Gateway API implementation is a Gateway Implementation
(GEEP) at the "Extended" conformance level for core HTTP routing. Two areas
have maturity concerns:

- **TLS passthrough:** Cilium supports `TLSRoute` (passthrough mode) but the
  implementation is newer than Envoy Gateway's. If any service uses TLS
  passthrough (the `TLSRoute` kind rather than HTTPS with termination on the
  Gateway), test it explicitly. The current cluster's Gateway `external`
  terminates TLS with `certificateRefs` (two wildcard certs: `default-wildcard-tls`
  and `web-domain-tls`), so TLS passthrough is not in use. Risk is low for the
  current config but should be verified post-migration.

- **Header modification:** Cilium Gateway API supports `RequestHeaderModifier`
  and `ResponseHeaderModifier` filters. The current cluster has only one
  HTTPRoute filter in use: `https-redirect` (a `RequestRedirect` filter, not
  header modification). Header modification routes, if added later, should be
  tested against Cilium's implementation — there have been historical bugs
  with `X-Forwarded-*` header handling. Envoy Gateway's header manipulation is
  more mature.

**Mitigation:** Before merge, audit all HTTPRoutes for `RequestHeaderModifier`,
`ResponseHeaderModifier`, `RequestMirror`, and `URLRewrite` filters. The
current cluster has only `RequestRedirect` (the HTTPS redirect), which is
core conformance and well-supported. Document any filter usage that exceeds
core conformance as a post-migration test item.

### 5.2 strictARP behavior differences

**Risk:** The old config had `kubeProxy.ipvs.strictARP: true`, which was
required for MetalLB + IPVS to work correctly — without it, IPVS's ARP
behavior caused issues with LoadBalancer IP reachability. Cilium does not use
IPVS or ARP for load balancing; it uses eBPF for service translation and BGP
for external advertisement. There is no `strictARP` equivalent in Cilium.

**What to watch:** After migration, LoadBalancer IPs are advertised via BGP to
OPNsense. OPNsense routes traffic to the node(s) running the service pod.
Cilium's eBPF handles the rest. If any external client or on-prem device has
stale ARP entries for the old MetalLB LB IPs, they need to be cleared (or
wait for ARP cache timeout). This is a brief disruption, not a persistent
issue.

**Mitigation:** The k0s config change removes `strictARP` entirely. No
equivalent setting is needed in Cilium. If LB IPs are unreachable after
migration, check: (a) BGP session to OPNsense is established, (b)
CiliumLoadBalancerIPPool has available IPs, (c) the service has an assigned
`LoadBalancerIP` or is set to auto-allocate from the pool.

### 5.3 k0s v1.35.3+k0s.0 / Kubernetes v1.36.2 compatibility with Cilium

**Risk:** The cluster runs k0s v1.35.3+k0s.0, which ships Kubernetes v1.36.2.
Cilium 1.19.5's compatibility with Kubernetes v1.36 needs verification. Cilium
officially supports up to the latest two Kubernetes minor versions at
release time. Cilium 1.19.x targets Kubernetes 1.30–1.33; v1.36 is ahead of
the tested range.

**What to watch:** The main risk areas are:
- API deprecation: v1.36 may deprecate APIs that Cilium 1.19.5 uses.
  Cilium typically catches up within one release cycle.
- eBPF program compatibility: newer kernels may have BPF helper changes.
  The nodes run kernel 6.17.0-40-generic, which is very recent and should
  have good BPF support, but Cilium's BPF programs are compiled against
  specific kernel versions.

**Mitigation:** Before the migration window:
1. Check the Cilium compatibility matrix for the exact Cilium version vs.
   Kubernetes v1.36 support level.
2. If Cilium 1.19.5 does not officially support v1.36, consider using the
   latest Cilium patch release or the Cilium 1.20.x line if available.
3. Test in a staging environment if possible. If not possible, have the
   rollback plan (Section 6) ready and practice it on one node first.

**Note:** The existing plan pins Cilium chart tag `1.19.5`. Verify this
version against the Cilium/Kubernetes compatibility matrix before merge.
If a newer version is needed, update the `OCIRepository.spec.ref.tag` in
`core/cilium.yaml`.

### 5.4 nodeLocalLoadBalancing conflict

**Risk:** The old k0s config had `nodeLocalLoadBalancing.enabled: true` with
`type: EnvoyProxy`. This deployed an Envoy proxy on each node that handled
service load balancing locally (avoiding a second hop for traffic to
node-local services). Cilium's eBPF kube-proxy replacement also handles
service load balancing at the node level, but via eBPF instead of a
userspace Envoy proxy.

**Conflict:** If both are enabled, there are two competing load balancers:
Envoy (NLLB) and Cilium (eBPF). Traffic would be double-processed, causing
incorrect routing, connection resets, or performance degradation.

**Resolution:** The k0s config change removes the `nodeLocalLoadBalancing`
block entirely. Cilium's eBPF data plane replaces this functionality. The
removal happens atomically with the `k0sctl apply` on each node — when the
node switches to `provider: custom`, k0s stops deploying the NLLB Envoy
proxy, and Cilium's eBPF takes over.

**What to watch:** After migration, verify that inter-pod service traffic
does not have a double-hop. Cilium's default behavior routes traffic
directly to the service endpoint via eBPF, which is equivalent to or better
than NLLB. Check with Hubble flows:
```sh
kubectl -n kube-system exec ds/cilium -- hubble observe --type flow --port 80
```

### 5.5 Dual-CNI coexistence during transition

**Risk:** During Phase 1–3, both kube-router and Cilium run on the same
nodes. Two CNIs managing the same network can cause conflicts: both try to
program network interfaces, iptables rules, and routing tables.

**Mitigation:** Cilium is designed to coexist with other CNIs during
migration. When `kubeProxyReplacement: true` is set but kube-proxy is still
running (Phase 1–2), Cilium detects the existing kube-proxy and operates in
a compatibility mode. Once kube-proxy is removed (Phase 3 per-node), Cilium
takes full control. The key is that only one CNI should be actively
assigning IPs to pods — during the transition, kube-router assigns IPs to
existing pods, and Cilium only assigns IPs to new pods on nodes where
kube-router has been removed. The per-node drain/uncordon sequence ensures
this clean handoff.

**What to watch:** If pods on a non-migrated node get Cilium-assigned IPs
instead of kube-router IPs, there may be a CIDR conflict. Verify that
Cilium's `ipam.mode: cluster-pool` with `clusterPoolIPv4PodCIDRList:
10.244.0.0/16` matches k0s's `podCIDR`. The existing k0s config does not
explicitly set `podCIDR` (k0s default is 10.244.0.0/16), so Cilium's pool
should match. If k0s uses a different CIDR, update Cilium's IPAM config to
match.

### 5.6 BGP duplicate advertisement during transition

**Risk:** During Phase 1–4, both MetalLB and Cilium BGP are running. Both
advertise the same LoadBalancer IPs to OPNsense. This causes duplicate BGP
routes — OPNsense sees two sources for the same /32, which can cause
asymmetric routing or route flapping.

**Mitigation:** This is acceptable during the brief transition window
because:
1. MetalLB and Cilium advertise the same IPs, so the route is the same
   destination.
2. OPNsense uses BGP multipath or picks one route based on AS path / local
   preference. Both routes point to the same cluster nodes.
3. The transition window is short (minutes per node, ~15 min total for 3
   nodes).

If route flapping is observed, the safest approach is to remove MetalLB's
BGPAdvertisement before starting the node migration (Phase 3), accepting a
brief gap where Cilium BGP is the only advertiser. This is a tradeoff between
overlap safety (both running) and gap safety (brief no-advertisement).

---

## 6. Rollback Plan

If the migration fails at any phase, rollback follows the reverse order.

### Rollback from Phase 3 (node migration in progress)

If a node fails to come back with Cilium after `k0sctl apply`:

1. **Revert the k0s config:**
   ```sh
   git revert <merge-commit>   # or git checkout main~1 -- config/cluster.yaml
   ```
   Restore the original `config/cluster.yaml`:
   ```yaml
   spec:
     network:
       kubeProxy:
         mode: ipvs
         ipvs:
           strictARP: true
       nodeLocalLoadBalancing:
         enabled: true
         type: EnvoyProxy
   ```

2. **Re-apply k0s to the affected node:**
   ```sh
   k0sctl apply --config config/cluster.yaml
   ```
   This re-enables kube-router and kube-proxy on the node. The node comes back
   with the old CNI.

3. **Uncordon the node:**
   ```sh
   kubectl uncordon k0s-inwin-03
   ```

4. **Verify the node is healthy with the old stack:**
   ```sh
   kubectl get nodes
   kubectl -n kube-system get pods --field-selector spec.nodeName=k0s-inwin-03
   ```

5. **If only 1–2 nodes were migrated, re-apply the old config to those nodes
   too** (same `k0sctl apply` with the reverted config). All nodes should be
   back on kube-router + kube-proxy before declaring rollback complete.

### Rollback from Phase 5 (old components removed)

If the old HelmReleases have been manually removed but Cilium is not working:

1. **Reinstall MetalLB:**
   The MetalLB manifests are still in git (on `main` before the merge, or
   revert the merge). Flux will re-apply them:
   ```sh
   git revert <merge-commit>
   flux reconcile kustomization core --with-source
   ```

2. **Reinstall Envoy Gateway:**
   Same — revert the merge and Flux re-applies the Envoy Gateway HelmRelease:
   ```sh
   flux reconcile kustomization platform --with-source
   ```

3. **Re-enable kube-router + kube-proxy via k0s:**
   ```sh
   k0sctl apply --config config/cluster.yaml  # with reverted config
   ```

4. **Remove Cilium:**
   Since `prune: false`, removing Cilium from git does not delete it from
   the cluster. Manually remove:
   ```sh
   helm uninstall cilium -n kube-system
   kubectl delete crd ciliumbgpclusterconfigs.cilium.io ciliumbgppeerconfigs.cilium.io ciliumbgpadvertisements.cilium.io ciliumloadbalancerippools.cilium.io ciliumbgppeerconfigs.cilium.io 2>/dev/null
   kubectl delete -n kube-system ciliumnetworkpolicies.cilium.io --all 2>/dev/null
   # Remove Cilium NetworkPolicy CRDs if installed
   kubectl delete crd ciliumnetworkpolicies.cilium.io ciliumclusterwidenetworkpolicies.cilium.io 2>/dev/null
   ```

### Rollback decision criteria

- **Cilium agents not Ready after 10 minutes on a migrated node:** rollback
  that node.
- **BGP session to OPNsense not established after 5 minutes:** check
  `cilium bgp peers` output; if misconfigured, rollback.
- **HTTPRoutes returning 503/502 after all nodes migrated:** Cilium Gateway
  API is not serving traffic. Rollback to Envoy Gateway.
- **Pods not getting IPs on a migrated node:** Cilium IPAM is not working.
  Rollback that node.

---

## 7. Verification Checklist

This checklist maps to the acceptance criteria for the migration. All items
must pass before declaring the migration complete.

### 7.1 Cilium CNI (replaces kube-router)

- [ ] `kubectl -n kube-system get ds cilium` shows 3/3 pods Ready
- [ ] `kubectl -n kube-system get deploy cilium-operator` shows 1/1 Ready
- [ ] `kubectl get nodes -o jsonpath='{.items[*].status.conditions[?(@.type=="Ready")].status}'` — all nodes `True`
- [ ] `kubectl -n kube-system exec ds/cilium -- cilium status` shows "Node health: OK" on each node
- [ ] New pods get IPs from the 10.244.0.0/16 pool: `kubectl run test --image=nginx --restart=Never && kubectl get pod test -o wide` shows an IP in the pod CIDR
- [ ] kube-router DaemonSet is removed: `kubectl get ds -n kube-system | grep kube-router` returns nothing
- [ ] No kube-router pods in `kube-system`: `kubectl get pods -n kube-system | grep kube-router` returns nothing

### 7.2 kube-proxy replacement

- [ ] kube-proxy is not running: `kubectl get pods -n kube-system | grep kube-proxy` returns nothing
- [ ] `kubectl -n kube-system exec ds/cilium -- cilium status | grep "kube-proxy"` shows "Kube-proxy replacement: true"
- [ ] Service routing works: `kubectl run test-client --image=nginx --restart=Never && kubectl exec test-client -- curl -s <any-cluster-service>` returns a response
- [ ] `kubectl -n kube-system exec ds/cilium -- cilium service list` shows all Kubernetes services

### 7.3 Cilium BGP (replaces MetalLB)

- [ ] `kubectl -n kube-system exec ds/cilium -- cilium bgp peers` shows a BGP session to `${ROUTER_IP}` in "Established" state on all 3 nodes
- [ ] `kubectl get ciliumbgpclusterconfig router` exists and shows the configured ASNs
- [ ] `kubectl get ciliumloadbalancerippool services-block` exists with the `${LOAD_BALANCER_POOL}` CIDR
- [ ] All LoadBalancer services have external IPs: `kubectl get svc -A | grep LoadBalancer` shows IPs in the 10.72.16.x pool
- [ ] OPNsense shows BGP routes for the LB IPs (check OPNsense BGP neighbors / routing table)
- [ ] MetalLB is fully removed: `kubectl get ns metallb-system` returns "NotFound"
- [ ] No MetalLB pods: `kubectl get pods -A | grep metallb` returns nothing

### 7.4 Cilium Gateway API (replaces Envoy Gateway)

- [ ] `kubectl get gatewayclass` shows `cilium` with `Accepted=True`
- [ ] `kubectl get gateway -n gateway external` shows `Programmed=True`
- [ ] `kubectl get httproute -A` shows all routes with `Accepted=True`
- [ ] The `https-redirect` HTTPRoute works: `curl -I http://<any-domain>` returns `301` redirect to HTTPS
- [ ] HTTPS routes work: `curl -sk https://<any-domain>` returns a response (TLS terminated by Cilium Gateway with the wildcard cert)
- [ ] TLS certificate refs are valid: `kubectl get gateway -n gateway external -o yaml | grep certificateRefs` shows `default-wildcard-tls` and `web-domain-tls`
- [ ] Envoy Gateway is fully removed: `kubectl get deploy -n gateway | grep envoy` returns nothing
- [ ] No Envoy Gateway pods: `kubectl get pods -A | grep envoy` returns nothing (excluding any app named envoy)

### 7.5 Hubble observability

- [ ] `kubectl -n kube-system get deploy hubble-relay` shows 1/1 Ready
- [ ] `kubectl -n kube-system get svc hubble-ui` exists with a ClusterIP
- [ ] `kubectl -n kube-system exec ds/cilium -- hubble observe --type flow -n 10` returns flow events
- [ ] Hubble UI is accessible (port-forward or HTTPRoute): `kubectl -n kube-system port-forward svc/hubble-ui 8080:80` and `curl http://localhost:8080` returns the UI
- [ ] Prometheus is scraping Cilium metrics: `kubectl -n monitoring exec prometheus-kube-prometheus-stack-prometheus-0 -- wget -qO- http://cilium-metrics.kube-system:9962/metrics | head` returns metrics
- [ ] Grafana has Cilium dashboards (if imported): check Grafana dashboard list for Cilium

### 7.6 No regressions

- [ ] `kubectl get nodes` — all 3 nodes Ready
- [ ] `kubectl get pods -A | grep -v Running | grep -v Completed` — no unexpected NotReady pods
- [ ] `kubectl get svc -A | grep LoadBalancer` — all LB services have external IPs in 10.72.16.x
- [ ] `flux get kustomizations` — all kustomizations are Ready
- [ ] `flux get helmreleases -A` — all helmreleases are Ready (including cilium)
- [ ] ExternalDNS is still creating records: `kubectl logs -n external-dns <external-dns-pod> | tail` shows successful updates
- [ ] cert-manager is still issuing certificates: `kubectl get certificates -A | grep -v Ready` returns nothing pending
- [ ] CoreDNS is serving: `kubectl -n kube-system get pods -l k8s-app=kube-dns` — all Running
- [ ] Longhorn is healthy: `kubectl -n longhorn-system get pods` — all Running
- [ ] Backup schedules are intact: `kubectl get schedules -n velero` — schedules present (check Velero excludedNamespaces no longer references `metallb-system`)

---

## Appendix: Files Changed in This Migration

| File | Action | Purpose |
|------|--------|---------|
| `core/cilium.yaml` | Created | Cilium HelmRelease + OCIRepository + Namespace |
| `core/resources/cilium-bgp.yaml` | Created | BGP CRs + CiliumLoadBalancerIPPool |
| `core/kustomization.yaml` | Modified | Add `cilium.yaml`, remove `metallb.yaml` |
| `core/resources/kustomization.yaml` | Modified | Add `cilium-bgp.yaml`, remove `metallb.yaml` |
| `core/metallb.yaml` | Deleted | MetalLB HelmRelease + Namespace |
| `core/resources/metallb.yaml` | Deleted | MetalLB IPAddressPool + BGPPeer + BGPAdvertisement |
| `config/cluster.yaml` | Modified | Switch to `provider: custom`, disable kube-proxy, remove NLLB |
| `platform/gateway/gateway.yaml` | Modified | Replace Envoy GatewayClass + EnvoyProxy with Cilium GatewayClass |
| `platform/gateway/envoy-gateway.yaml` | Deleted | Envoy Gateway HelmRelease + OCIRepository |
| `platform/gateway/monitors.yaml` | Deleted | Envoy PodMonitor + ServiceMonitor |
| `platform/gateway/dashboards.yaml` | Deleted | Envoy Grafana dashboards |
| `platform/gateway/kustomization.yaml` | Modified | Remove deleted files from resources |
| `platform/monitoring/dashboards/metallb.yaml` | Deleted | MetalLB Grafana dashboard |
| `platform/monitoring/dashboards/kustomization.yaml` | Modified | Remove `metallb.yaml` reference |
| `platform/velero/helmrelease.yaml` | Modified | Remove `metallb-system` from excludedNamespaces |
