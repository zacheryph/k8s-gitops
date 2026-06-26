# ExternalDNS with AdGuard Home

ExternalDNS pushes DNS records to an AdGuard Home instance on the LAN
via the [AdGuard Home webhook provider](https://github.com/muhlba91/external-dns-provider-adguard).

## Components

- **external-dns-adguard** — kubernetes-sigs/external-dns HelmRelease with
  `provider=webhook` and the AdGuard Home provider sidecar. Pushes DNS
  rewrite rules to AdGuard Home via its API.

## Architecture

```
HTTPRoute ──► ExternalDNS-adguard ──► AdGuard Home API
Service LB ┘                              │
                                          │ OPNSense/LAN
                                          │
                                          ▼
                                    LAN clients (resolving via
                                    AdGuard Home DNS)
```

## Source of truth

ExternalDNS creates records when:

- An HTTPRoute has a `spec.hostnames` entry, OR
- A Service has the annotation
  `external-dns.alpha.kubernetes.io/hostname: <name>.<zone>`, OR
- An Ingress has `spec.rules[].host` entries.

To exclude a resource, set the annotation
`external-dns.alpha.kubernetes.io/enabled: "false"`.

## Secrets (one-time out-of-band)

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

## AdGuard Home provider limitations

> [!IMPORTANT]
> The provider takes **ownership** of **all rules** matching the
> `dnsrewrite` format (`|name^dnsrewrite=NOERROR;TYPE;TARGET`). Rules
> not matching this format (e.g., blocklist rules) are left untouched.
>
> If you need manually-set DNS rules alongside ExternalDNS-managed ones,
> define them as `DNSEndpoint` CRD objects and enable the `crd` source.

## Verification

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

## Troubleshooting

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