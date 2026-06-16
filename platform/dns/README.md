# Split-DNS Automation (BIND + ExternalDNS)

Two ExternalDNS deployments push records to Cloudflare (public) and an
in-cluster BIND server (internal). OPNSense forwards the zone to BIND so
LAN clients get local answers while the internet sees Cloudflare.

## Components

- **bind** — BIND 9 Deployment (hand-rolled, single replica). Acts as an
  authoritative nameserver for the public zone on the internal side.
  ExternalDNS writes A/AAAA/CNAME records via RFC 2136 (TSIG-signed).
  Backed by a Longhorn PVC for zone journal persistence. Exposed via
  MetalLB LoadBalancer at `10.72.16.51`.
- **external-dns-cloudflare** — bitnami/external-dns HelmRelease with
  `provider=cloudflare`. Pushes records to the Cloudflare API.
- **external-dns-rfc2136** — bitnami/external-dns HelmRelease with
  `provider=rfc2136`. Pushes records to the in-cluster BIND server via
  TSIG-signed dynamic DNS updates.

## Architecture

```
HTTPRoute ──► ExternalDNS-cloudflare ──► Cloudflare API     (public)
Service LB ┘
HTTPRoute ──► ExternalDNS-rfc2136   ──► BIND (RFC 2136)    (internal)
Service LB ┘                              │
                                          │ MetalLB LB IP 10.72.16.51
                                          │
                                    OPNSense Unbound (forward-zone)
                                          ▲
                                    LAN clients
```

## Source of truth

A record appears in both providers when:

- An HTTPRoute has a `spec.hostnames` entry under the public zone, OR
- A Service of type LoadBalancer has the annotation
  `external-dns.alpha.kubernetes.io/hostname: <name>.<zone>`.

To exclude a resource, set the annotation
`external-dns.alpha.kubernetes.io/enabled: "false"`.

## Secrets (one-time out-of-band)

### Cloudflare API token

1. In the Cloudflare dashboard, create an API token with:
   - **Permissions:** `Zone:DNS:Edit`
   - **Zone Resources:** Include → Specific zone → `<zone>`
2. Find the zone ID:
   ```bash
   curl -s -H "Authorization: Bearer *** \
     https://api.cloudflare.com/client/v4/zones | jq '.result[] | select(.name=="<zone>") | .id'
   ```
3. Replace `REPLACE_WITH_CLOUDFLARE_ZONE_ID` in
   `platform/dns/external-dns-cloudflare.yaml`.
4. Create the Secret in-cluster:
   ```bash
   kubectl -n dns create secret generic cloudflare-api-token \
     --from-literal=api-token='<token from step 1>'
   ```

### RFC 2136 TSIG key for BIND

The TSIG key hardcoded in `bind.yaml` (ConfigMap `bind-config`) and in the
Secret `rfc2136-tsig` must match. If you want a new key:

```bash
# Generate a fresh TSIG key
tsig-keygen -a hmac-sha256 external-dns.
# Output looks like:
# key "external-dns." {
#     algorithm hmac-sha256;
#     secret "BASE64_SECRET_HERE";
# };

# Update the key in bind.yaml (ConfigMap -> bind-config -> named.conf)
# AND update the Secret:
kubectl -n dns create secret generic rfc2136-tsig \
  --from-literal=tsig-secret='BASE64_SECRET_HERE' \
  --dry-run=client -o yaml | kubectl apply -f -
```

The `external-dns-rfc2136` HelmRelease references this secret by name
(`rfc2136.existingSecret: rfc2136-tsig`).

## OPNSense configuration (follow-up, not in this PR)

In the OPNSense UI:

1. **Services → Unbound DNS → Overrides → Domain Overrides**
2. Click **Add**
3. **Domain:** `<public zone>`
4. **Type:** "Forward"
5. **IP Address:** `10.72.16.51` (the MetalLB IP of the `bind` service)
6. Click **Save**, then **Apply**.

After this, queries for `<public zone>` from LAN clients go
OPNSense → in-cluster BIND → answer. External queries hit Cloudflare.

## Verification (end-to-end)

Run after merge:

```bash
# 1. Confirm everything in the dns namespace is healthy
kubectl -n dns get pods
# Expected: bind-* (1/1), external-dns-rfc2136-* (1/1),
#           external-dns-cloudflare-* (1/1) — all Running

# 2. Confirm BIND is reachable via the MetalLB IP
kubectl -n dns run digtest --rm -it --image=mirror.gcr.io/busybox:1.36 -- \
  dig +short ns1.example.com @10.72.16.51
# Expected: 10.72.16.51 (the seed NS record)

# 3. Create a test HTTPRoute
kubectl apply -f - <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: extdns-smoketest
  namespace: default
spec:
  parentRefs:
    - name: external
      namespace: gateway
  hostnames: ["smoketest.example.com"]
  rules:
    - backendRefs:
        - name: kube-dns
          port: 53
EOF

# 4. Wait ~90s for ExternalDNS to reconcile
sleep 90

# 5. Verify in BIND (via RFC 2136)
kubectl -n dns run digtest2 --rm -it --image=mirror.gcr.io/busybox:1.36 -- \
  dig +short smoketest.example.com @10.72.16.51
# Expected: the gateway LB IP

kubectl -n dns exec deploy/bind -- named-checkzone example.com /var/bind/data/example.com.zone
# Should show the zone with the new smoketest record

# 6. Verify in Cloudflare
# (replace <zone id> with the actual zone ID)
curl -s -H "Authorization: Bearer *** \
  "https://api.cloudflare.com/client/v4/zones/<zone id>/dns_records" | \
  jq '.result[] | select(.name=="smoketest.example.com")'
# Expected: a record with name "smoketest.example.com"

# 7. From a LAN client (after OPNSense config):
dig +short smoketest.example.com
# Expected: the gateway LB IP (via OPNSense → BIND)

# 8. Cleanup
kubectl delete -n default httproute extdns-smoketest
sleep 90
# Records disappear from BIND and Cloudflare.
```

## Troubleshooting

- **BIND pod won't start** — check the init container logs:
  `kubectl -n dns logs bind-* -c seed-zone`. If the seed zone copy fails,
  the PVC may not be writable. Verify Longhorn is healthy.
- **ExternalDNS can't reach BIND** — verify the service resolves from
  inside the cluster: `kubectl -n dns run digtest --rm -it --image=busybox -- nslookup bind.dns.svc.cluster.local`
- **TSIG signature errors in ExternalDNS logs** — the TSIG key in the
  Secret (`rfc2136-tsig`) doesn't match the key in BIND's ConfigMap
  (`bind-config`). Regenerate both and re-deploy.
- **OPNSense forward-zone queries fail** — confirm the MetalLB IP is
  correct: `kubectl -n dns get svc bind`. If MetalLB assigned a different
  IP, update the OPNSense Domain Override.
- **Zone file is stale after BIND restart** — BIND writes updates to a
  journal file (`.jnl`) on the PVC. The PVC persists across restarts.
  To force a fresh zone transfer, delete the PVC and let it reprovision:
  `kubectl -n dns delete pvc bind-data && kubectl -n dns rollout restart deploy bind`.
- **Too many NOTIFY messages** — BIND may try to NOTIFY its secondaries
  on every update. The `allow-transfer { none; }` directive in
  `named.conf` suppresses zone transfers and should suppress NOTIFY for
  zones with no configured secondaries. If NOTIFY still appears in logs,
  add `also-notify { };` to the zone block.
