# Split-DNS Automation (AdGuard Home + ExternalDNS)

Two ExternalDNS deployments push records to Cloudflare (public) and an
AdGuard Home instance on the LAN (internal). OPNSense forwards the zone to
AdGuard Home so LAN clients get local answers while the internet sees
Cloudflare.

## Components

- **external-dns-cloudflare** — bitnami/external-dns HelmRelease with
  `provider=cloudflare`. Pushes records to the Cloudflare API.
- **external-dns-adguard** — kubernetes-sigs/external-dns HelmRelease with
  `provider=webhook` and the AdGuard Home provider sidecar. Pushes records
  to an AdGuard Home instance on the LAN via its API.

## Architecture

```
HTTPRoute ──► ExternalDNS-cloudflare ──► Cloudflare API       (public)
Service LB ┘
HTTPRoute ──► ExternalDNS-adguard   ──► AdGuard Home API     (internal)
Service LB ┘                              │
                                          │ OPNSense/LAN
                                          │
                                    OPNSense Unbound (forward-zone or
                                    AdGuard Home as upstream)
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
   curl -s -H "Authorization: Bearer ***" \
     https://api.cloudflare.com/client/v4/zones | jq '.result[] | select(.name=="<zone>") | .id'
   ```
3. Replace `REPLACE_WITH_CLOUDFLARE_ZONE_ID` in
   `platform/dns/external-dns-cloudflare.yaml`.
4. Create the Secret in-cluster:
   ```bash
   kubectl -n dns create secret generic cloudflare-api-token \
     --from-literal=api-token='<token from step 1>'
   ```

### AdGuard Home configuration

Create the Secret with your AdGuard Home instance details:

```bash
kubectl -n dns create secret generic adguard-configuration \
  --from-literal=url='https://<adguard-home-ip-or-hostname>' \
  --from-literal=user='<adguard-username>' \
  --from-literal=password='<adguard-password>'
```

The AdGuard Home instance runs on OPNSense (or another LAN host). The
webhook provider communicates with AdGuard Home's API to manage DNS
rewrite rules (Adblock-style filtering syntax).

### AdGuard Home provider limitations

> [!IMPORTANT]
> The provider takes **ownership** of **all rules** matching the
> `dnsrewrite` format (`|name^dnsrewrite=NOERROR;TYPE;TARGET`). Rules
> not matching this format (e.g., blocklist rules) are left untouched.
>
> If you need manually-set DNS rules alongside ExternalDNS-managed ones,
> define them as `DNSEndpoint` CRD objects and enable the `crd` source.

## OPNSense configuration (follow-up, not in this PR)

AdGuard Home handles the split-DNS directly — no additional OPNSense
configuration is needed beyond what's already set up for AdGuard Home
to serve DNS. If Unbound is the primary resolver, configure it to
forward the zone to AdGuard Home:

1. **Services → Unbound DNS → Overrides → Domain Overrides**
2. Click **Add**
3. **Domain:** `<public zone>`
4. **Type:** "Forward"
5. **IP Address:** `<AdGuard Home IP>`
6. Click **Save**, then **Apply**.

## Verification (end-to-end)

Run after merge:

```bash
# 1. Confirm everything in the dns namespace is healthy
kubectl -n dns get pods
# Expected: external-dns-adguard-* (1/1),
#           external-dns-cloudflare-* (1/1) — all Running

# 2. Confirm AdGuard provider is healthy
kubectl -n dns logs deploy/external-dns-adguard -c webhook | head -20
# Expected: "starting server on :8888" or similar startup log

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

# 5. Verify in AdGuard Home
# Check the AdGuard Home UI → Filters → Custom filtering rules.
# Look for:
#   |smoketest.example.com^dnsrewrite=NOERROR;A;<gateway IP>

# 6. Verify in Cloudflare
# (replace <zone id> with the actual zone ID)
curl -s -H "Authorization: Bearer ***" \
  "https://api.cloudflare.com/client/v4/zones/<zone id>/dns_records" | \
  jq '.result[] | select(.name=="smoketest.example.com")'
# Expected: a record with name "smoketest.example.com"

# 7. From a LAN client:
dig +short smoketest.example.com
# Expected: the gateway LB IP (via AdGuard Home)

# 8. Cleanup
kubectl delete -n default httproute extdns-smoketest
sleep 90
# Records disappear from AdGuard Home and Cloudflare.
```

## Troubleshooting

- **AdGuard webhook can't reach AdGuard Home** — verify network connectivity
  from the cluster to the AdGuard Home instance:
  ```bash
  kubectl -n dns run curltest --rm -it --image=curlimages/curl:latest -- \
    curl -s -o /dev/null -w "%{http_code}" '<adguard-url>'
  ```
  Expected: 200. If not, check firewall rules / OPNSense ACLs.

- **webhook container fails readiness probe** — check logs:
  ```bash
  kubectl -n dns logs deploy/external-dns-adguard -c webhook
  ```
  Common causes: wrong ADGUARD_URL (must be reachable from cluster),
  wrong credentials, or certificate issues if using HTTPS.

- **ExternalDNS logs show "context deadline exceeded"** — the webhook
  sidecar isn't responding. Check the webhook container is running and
  the SERVER_PORT matches what external-dns expects (`localhost:8888`).

- **Rules show up in AdGuard but DNS doesn't resolve** — AdGuard Home
  must be configured as the DNS resolver for your LAN clients (either
  directly or as OPNSense Unbound's upstream). Verify with
  `dig +short <hostname> @<adguard-ip>`.

- **AdGuard Home API is HTTP only** — set ADGUARD_URL with `http://`.
  The provider does not skip TLS verification; use a valid certificate
  or HTTP for local-only instances.