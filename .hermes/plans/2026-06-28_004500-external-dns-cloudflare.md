# ExternalDNS Cloudflare — Public DNS Automation

> **For Hermes:** Use subagent-driven-development to implement this plan task-by-task.

**Goal:** Add a second ExternalDNS instance (`external-dns-cloudflare`) alongside the existing `external-dns-adguard` in the `dns` namespace. This instance pushes proxied A/AAAA/CNAME records to Cloudflare for the public domain, using a static NAT external IP as the target. **Opt-in only:** records are only created for resources explicitly annotated with `dns.routine.sh/external: "true"`.

**Architecture:**

```
HTTPRoute ──► ExternalDNS-cloudflare (provider=cloudflare) ──► Cloudflare API
Service LB ┘     │                                              │
                 │ --annotation-filter                          │
                 │   publish-public=true                        │
                 │ --cloudflare-proxied                         │
                 │ --target=${CLOUD_EXTERNAL_IP}                ▼
                 │                                       Public DNS (proxied)
                 │                                       ──► external IP
                 │                                    (only if annotated)
                 │
ExternalDNS-adguard (provider=webhook, AdGuard Home) ──► AdGuard Home (internal DNS)
```

**Tech Stack:** kubernetes-sigs/external-dns Helm chart (same `HelmRepository` already in the `dns` namespace), Cloudflare API token, Flux substitution.

**Assumptions:**
- Public domain is already in `cluster-secrets` as `${CLUSTER_EXTERNAL_DOMAIN}` (exists at `config/secrets.yaml:39`)
- Cloudflare API token is already in `cluster-secrets` as `${CLOUDFLARE_TOKEN}` (exists at `config/secrets.yaml:15`)
- NAT external IP is static (the user will set `${CLOUD_EXTERNAL_IP}` in cluster-secrets)
- The `dns` namespace already exists with `HelmRepository` named `external-dns` pointing at `https://kubernetes-sigs.github.io/external-dns/`

---

### Task 1: Add `${CLOUD_EXTERNAL_IP}` to cluster-secrets

**Objective:** Make the NAT external IP available to Flux substitution.

**This is a human step — the user must edit the SOPS-encrypted file.**

```bash
# User runs on their workstation:
cd /opt/data/repos/k8s-gitops
scripts/cluster-secrets set CLOUD_EXTERNAL_IP <their-static-external-ip>
```

This adds `CLOUD_EXTERNAL_IP` to `config/secrets.yaml` (SOPS-encrypted). Flux will substitute `${CLOUD_EXTERNAL_IP}` in any manifest in the repo.

**Verification:**
```bash
scripts/cluster-secrets get CLOUD_EXTERNAL_IP
# Expected: the IP address that was set
```

---

### Task 2: Create Cloudflare API token Secret

**Objective:** Make the Cloudflare API token available to the ExternalDNS pod as a Kubernetes Secret in the `dns` namespace.

**File:** Create `platform/dns/secret-cloudflare.yaml`

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: cloudflare-configuration
  namespace: dns
type: Opaque
stringData:
  token: ${CLOUDFLARE_TOKEN}
```

The `${CLOUDFLARE_TOKEN}` is substituted by Flux at apply time from `config/secrets.yaml`. The resulting Secret lives in the `dns` namespace where the pod can mount it.

**Commit:**
```bash
git add platform/dns/secret-cloudflare.yaml
git commit -m "feat(dns): add cloudflare API token secret for external-dns"
```

---

### Task 3: Create ExternalDNS Cloudflare HelmRelease

**Objective:** Deploy a second ExternalDNS instance that pushes proxied records to Cloudflare.

**File:** Create `platform/dns/external-dns-cloudflare.yaml`

```yaml
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: external-dns-cloudflare
  namespace: dns
spec:
  interval: 1h
  chart:
    spec:
      chart: external-dns
      version: 1.21.1
      sourceRef:
        kind: HelmRepository
        name: external-dns
  upgrade:
    crds: CreateReplace
  values:
    provider:
      name: cloudflare
    env:
      - name: CF_API_TOKEN
        valueFrom:
          secretKeyRef:
            name: cloudflare-configuration
            key: token
    extraArgs:
      - --annotation-filter=dns.routine.sh/external=true
      - --cloudflare-proxied
      - --target=${CLOUD_EXTERNAL_IP}
    registry: txt
    txtOwnerId: external-dns-cloudflare
    policy: sync
    interval: 1m
    logLevel: info
    sources:
      - service
      - ingress
      - gateway-httproute
    domainFilters:
      - ${CLUSTER_EXTERNAL_DOMAIN}
    resources:
      requests:
        cpu: 50m
        memory: 64Mi
      limits:
        memory: 128Mi
    podSecurityContext:
      fsGroup: 65534
      seccompProfile:
        type: RuntimeDefault
    securityContext:
      privileged: false
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      runAsNonRoot: true
      runAsUser: 65532
      runAsGroup: 65532
      capabilities:
        drop:
          - ALL
```

Key differences from `external-dns-adguard`:
- `provider.name: cloudflare` (native, no webhook sidecar needed)
- `env` sets `CF_API_TOKEN` from the `cloudflare-configuration` Secret
- `extraArgs` with `--annotation-filter` (opt-in only), `--cloudflare-proxied` (all records proxied), and `--target=${CLOUD_EXTERNAL_IP}` (NAT external IP)
- `txtOwnerId: external-dns-cloudflare` (distinct ownership from the AdGuard instance)
- `domainFilters: ${CLUSTER_EXTERNAL_DOMAIN}` (public domain, not internal)
- No `provider.webhook` block (Cloudflare is a built-in provider)
- Same security context, resource limits, and source list as the AdGuard instance

**Commit:**
```bash
git add platform/dns/external-dns-cloudflare.yaml
git commit -m "feat(dns): add external-dns cloudflare instance for public DNS"
```

---

### Task 4: Register new resources in kustomization.yaml

**Objective:** Include the new files in Flux's reconciliation.

**File:** Modify `platform/dns/kustomization.yaml`

Add two lines to the `resources:` list:

```yaml
resources:
- namespace.yaml
- secrets.yaml
- secret-cloudflare.yaml
- helm-repository.yaml
- external-dns-adguard.yaml
- external-dns-cloudflare.yaml
```

**Commit:**
```bash
git add platform/dns/kustomization.yaml
git commit -m "feat(dns): register cloudflare external-dns in kustomization"
```

---

### Task 5: Update DNS README

**Objective:** Document the new Cloudflare instance.

**File:** Modify `platform/dns/README.md`

Replace the opening heading and add a Cloudflare section after the AdGuard section:

```markdown
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
  annotated with `dns.routine.sh/external: "true"`
  get public DNS records.
```

(Keep the rest of the existing AdGuard documentation unchanged.)

Add a new section at the end of the file (before Troubleshooting, or as a new top-level section):

```markdown
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
```

**Commit:**
```bash
git add platform/dns/README.md
git commit -m "docs(dns): document cloudflare external-dns instance"
```

---

### Task 6: Push and open PR

**Objective:** Ship the changes.

```bash
git push origin feat/external-dns-cloudflare
gh pr create \
  --base main \
  --head feat/external-dns-cloudflare \
  --title "feat(dns): add external-dns cloudflare instance for public DNS" \
  --body "Adds a second ExternalDNS instance in the dns namespace that pushes proxied DNS records to Cloudflare for the public domain.

### What
- New \`external-dns-cloudflare\` HelmRelease alongside existing \`external-dns-adguard\`
- Uses \`provider: cloudflare\` (native, no sidecar needed)
- **Opt-in only** — only resources annotated with \`dns.routine.sh/external: \"true\"\` get public DNS records
- All published records proxied through Cloudflare (\`--cloudflare-proxied\`)
- Target IP set to the static NAT external IP (\`--target=\${CLOUD_EXTERNAL_IP}\`)
- API token from \`cluster-secrets\` via \`cloudflare-configuration\` Secret

### Human steps before Flux reconciles
1. Set \`CLOUD_EXTERNAL_IP\` in cluster-secrets:
   \`\`\`bash
   scripts/cluster-secrets set CLOUD_EXTERNAL_IP <static-external-ip>
   \`\`\`
2. Verify \`CLOUDFLARE_TOKEN\` and \`CLUSTER_EXTERNAL_DOMAIN\` are already set (they exist in \`config/secrets.yaml\`)

### Verification
\`\`\`bash
kubectl -n dns get pods -l app.kubernetes.io/instance=external-dns-cloudflare
kubectl -n dns logs deploy/external-dns-cloudflare
\`\`\`"
```

---

## Post-merge human steps

1. **Set the external IP:**
   ```bash
   scripts/cluster-secrets set CLOUD_EXTERNAL_IP <static-external-ip>
   ```
   This adds `CLOUD_EXTERNAL_IP` to `config/secrets.yaml`.

2. **Force reconciliation** (or wait for Flux's interval):
   ```bash
   flux reconcile kustomization platform --with-source
   ```

3. **Verify the pod is healthy:**
   ```bash
   kubectl -n dns get pods -l app.kubernetes.io/instance=external-dns-cloudflare
   kubectl -n dns logs deploy/external-dns-cloudflare
   ```

4. **Create a test record** by adding an HTTPRoute or Service annotation, then verify in Cloudflare dashboard.

---

## Risks / tradeoffs

- **Opt-in by default** — no DNS records are published unless a resource carries `dns.routine.sh/external: "true"`. This is intentional and prevents accidental exposure. The AdGuard (internal) instance still publishes everything matching the internal domain filter.
- **Both instances watch the same sources** (service, ingress, gateway-httproute). They won't conflict because they have different `domainFilters` (internal vs public) and different `txtOwnerId` values.
- **All published records are proxied** (`--cloudflare-proxied`). If the user wants some records unproxied (e.g., for non-HTTP services), they'd need per-record annotations instead of the global flag.
- **`--target` is global** — all records point to the same external IP. If the user needs different targets per service, they'd use per-source annotations instead.