# ExternalDNS — AdGuard Home (internal) + Cloudflare (public)

Two ExternalDNS instances share the same namespace and chart source,
each targeting a different DNS provider.

## Components

- **external-dns-adguard** — kubernetes-sigs/external-dns HelmRelease with
  `provider=webhook` and the AdGuard Home provider sidecar. Pushes DNS
  rewrite rules to AdGuard Home via its API for `${CLUSTER_DOMAIN}` (internal).

- **external-dns-cloudflare** — kubernetes-sigs/external-dns HelmRelease with
  `provider=cloudflare`. Pushes proxied DNS records to Cloudflare for
  `${CLUSTER_EXTERNAL_DOMAIN}` (public). **Opt-in only** — only resources
  annotated with `dns.routine.sh/external: "true"` get public DNS records.

## AdGuard Home (internal DNS)

### Architecture

```
HTTPRoute ──► ExternalDNS-adguard ──► AdGuard Home API
Service LB ┘                              │
                                          │ OPNSense/LAN
                                          │
                                          ▼
                                    LAN clients (resolving via
                                    AdGuard Home DNS)
```

### Source of truth

ExternalDNS creates records when:

- An HTTPRoute has a `spec.hostnames` entry, OR
- A Service has the annotation
  `external-dns.alpha.kubernetes.io/hostname: <name>.<zone>`, OR
- An Ingress has `spec.rules[].host` entries.

To exclude a resource, set the annotation
`external-dns.alpha.kubernetes.io/enabled: "false"`.

### Secrets (one-time out-of-band)

Create the AdGuard Home configuration Secret before Flux reconciles:

```bash
kubectl -n dns create secret generic adguard-configuration \
  --from-literal=url='https://<adguard-home-ip-or-hostname>' \
  --from-literal=user='<adguard-username>' \
  --from-literal=password='<adguard-password>'
```

The webhook provider communicates with AdGuard Home's API to manage DNS
rewrite rules using the Adblock-style filtering syntax:

```
|host.example.com^dnsrewrite=NOERROR;A;10.72.16.X
```

### AdGuard Home provider limitations

> [!IMPORTANT]
> The provider takes **ownership** of **all rules** matching the
> `dnsrewrite` format (`|name^dnsrewrite=NOERROR;TYPE;TARGET`). Rules
> not matching this format (e.g., blocklist rules) are left untouched.
>
> If you need manually-set DNS rules alongside ExternalDNS-managed ones,
> define them as `DNSEndpoint` CRD objects and enable the `crd` source.

### Verification

Run after merge and secret creation:

```bash
# 1. Confirm the pod is healthy
kubectl -n dns get pods
# Expected: external-dns-adguard-* (1/1) — Running

# 2. Check webhook sidecar logs
kubectl -n dns logs deploy/external-dns-adguard -c webhook
# Expected: "starting server on :8888"

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

# 6. From a LAN client using AdGuard Home as resolver:
dig +short smoketest.example.com
# Expected: the gateway LB IP

# 7. Cleanup
kubectl delete -n default httproute extdns-smoketest
sleep 90
# Rule disappears from AdGuard Home.
```

### Troubleshooting

- **Webhook can't reach AdGuard Home** — verify connectivity:
  ```bash
  kubectl -n dns run curltest --rm -it --image=curlimages/curl:latest -- \
    curl -s -o /dev/null -w "%{http_code}" '<adguard-url>'
  ```
  Expected: 200. If not, check firewall rules / OPNSense ACLs.

- **Webhook fails readiness probe** — check logs:
  ```bash
  kubectl -n dns logs deploy/external-dns-adguard -c webhook
  ```
  Common causes: wrong ADGUARD_URL, wrong credentials, or TLS certificate
  issues if using HTTPS.

- **ExternalDNS logs show "context deadline exceeded"** — the webhook
  sidecar isn't responding. Verify both containers are running and the
  webhook is listening on port 8888.

- **AdGuard Home API is HTTP only** — set `url` with `http://`. The
  provider does not skip TLS verification; use a valid certificate or
  HTTP for local-only instances.

## Cloudflare (public DNS)

The Cloudflare instance creates **proxied** A/AAAA/CNAME records for the
public domain (`${CLUSTER_EXTERNAL_DOMAIN}`). All records are proxied
through Cloudflare's edge (`--cloudflare-proxied`). The target IP is the
static NAT external IP set in `cluster-secrets` as `CLOUD_EXTERNAL_IP`.

### Opt-in annotation

By default, ExternalDNS publishes **nothing** to Cloudflare. To publish
a resource's DNS records, add the annotation:

```yaml
metadata:
  annotations:
    dns.routine.sh/external: "true"
```

Without this annotation, the resource is ignored by the Cloudflare
instance. The AdGuard (internal) instance is unaffected — it continues
to publish all matching resources to the internal domain.

### Credentials

The Cloudflare API token is stored in the `cloudflare-configuration`
Secret (created from `${CLOUDFLARE_TOKEN}` in `cluster-secrets`).
The token needs `Zone:Read` and `DNS:Edit` permissions on the target zone.

### Verification

```bash
# 1. Confirm the pod is healthy
kubectl -n dns get pods -l app.kubernetes.io/instance=external-dns-cloudflare
# Expected: external-dns-cloudflare-* (1/1) — Running

# 2. Check logs for successful Cloudflare API calls
kubectl -n dns logs deploy/external-dns-cloudflare
# Expected: "All records are already up to date" or records created

# 3. Verify in Cloudflare dashboard
# Check the DNS tab for the zone — records should appear with
# the orange cloud (proxied) icon and the external IP as the target.

# 4. From an external client:
dig +short <hostname>.<public-domain>
# Expected: Cloudflare proxy IPs (not the NAT IP directly, since proxied)
```

### Troubleshooting

- **"Invalid request headers" (6003)** — the API token is invalid or
  doesn't have permission on the zone. Recreate the token with
  `Zone:Read` + `DNS:Edit` on the target zone.

- **Records show internal IPs instead of external** — verify
  `--target=${CLOUD_EXTERNAL_IP}` is set and `${CLOUD_EXTERNAL_IP}`
  is correctly set in `cluster-secrets`.

- **Records not proxied** — verify `--cloudflare-proxied` is in
  `extraArgs`.