# Promtail Syslog Receiver for OPNsense Remote Logging

> **For Hermes:** Use subagent-driven-development to implement this plan task-by-task.

**Goal:** Add a Promtail syslog listener on port 1514 exposed via MetalLB so OPNsense can ship
its logs to Loki in real-time. Logs survive OPNsense reboots because Loki is the source of truth.

**Context:** OPNsense went down ~July 16 and the logs were lost on reboot (July 19). OPNsense has
no `os-loki` plugin — its syslog-ng only supports `udp4/tcp4/tls4` transports (no HTTP push).
The correct path is: OPNsense → syslog → Promtail (syslog listener) → Loki.

**Architecture:**
```
OPNsense (syslog-ng)
    │ syslog (DNS: syslog.<internal>) → MetalLB LB → Promtail
    ▼
Promtail DaemonSet (syslog scrape config)
    │ HTTP push
    ▼
Loki :3100 (monitoring namespace)
```

**DNS approach:** Instead of hardcoding a MetalLB IP, the extraPorts service is annotated with
`external-dns.alpha.kubernetes.io/hostname: syslog.${CLUSTER_DOMAIN}`. ExternalDNS (AdGuard Home)
picks this up and creates a DNS rewrite rule. OPNsense uses the DNS name — no IP to hardcode
in cluster-secrets or OPNsense config.

**Existing stack:** Promtail chart 6.17.1 via OCIRepository, DaemonSet with 3 pods, pushing to
`http://loki:3100/loki/api/v1/push`. No extra ports or syslog scrapes currently configured.

**ExternalDNS:** Already running in `dns` namespace with `source: service` and `${CLUSTER_DOMAIN}`
in domainFilters. The annotation on the extraPorts service is all that's needed.

---

### Task 1: Add syslog extra port and scrape config to Promtail

**Objective:** Add `extraPorts` for syslog (port 1514 TCP) and a `syslog` scrape job to the
Promtail HelmRelease values.

**File:** `platform/monitoring/promtail.yaml`

Changes to the HelmRelease `.spec.values`:
1. Add `extraPorts.syslog` block — creates a dedicated LoadBalancer Service on port 1514
2. Add `config.snippets.extraScrapeConfigs` — syslog job with relabel_configs for hostname/app/severity

```yaml
### YAML diff (HelmRelease values only) ###

  values:
    serviceMonitor:
      enabled: true
    extraPorts:
      syslog:
        name: tcp-syslog
        containerPort: 1514
        annotations:
          external-dns.alpha.kubernetes.io/hostname: syslog.${CLUSTER_DOMAIN}
        service:
          port: 1514
          type: LoadBalancer
          externalTrafficPolicy: Local
    config:
      clients:
        - url: http://loki:3100/loki/api/v1/push
      snippets:
        extraScrapeConfigs: |
          - job_name: syslog
            syslog:
              listen_address: 0.0.0.0:1514
              labels:
                job: syslog
            relabel_configs:
              - source_labels:
                  - __syslog_message_hostname
                target_label: hostname
              - source_labels:
                  - __syslog_message_app_name
                target_label: app
              - source_labels:
                  - __syslog_message_severity
                target_label: level
```

**Design decisions:**
- Port 1514 (not 514) — non-privileged, avoids needing `hostNetwork` or root. OPNsense can
  specify any destination port in its syslog config.
- `externalTrafficPolicy: Local` — preserves source IP (OPNsense's LAN IP), avoids SNAT.
  Requires Promtail pods on every node (which DaemonSet already guarantees).
- DNS over hardcoded IP — `external-dns.alpha.kubernetes.io/hostname: syslog.${CLUSTER_DOMAIN}`
  annotation creates a DNS record via ExternalDNS (AdGuard Home). MetalLB auto-assigns the IP;
  ExternalDNS keeps the record in sync. No `${LOAD_BALANCER_SYSLOG}` needed.
- `config.snippets.extraScrapeConfigs` is the official Promtail chart mechanism (not a custom
  config override).
- Relabel configs extract standard syslog RFC 5424 fields into Loki labels.

**Commit:**
```bash
git add platform/monitoring/promtail.yaml
git commit -m "feat(monitoring): add Promtail syslog receiver for OPNsense remote logging"
```

---

### Post-merge: User steps

1. Force Flux reconcile:
   ```bash
   flux reconcile kustomization platform-monitoring --with-source
   ```

2. Verify Promtail syslog Service got its LB IP:
   ```bash
   kubectl get svc -n monitoring -l app.kubernetes.io/name=promtail
   ```

3. Verify ExternalDNS created the DNS record (wait ~90s):
   ```bash
   nslookup syslog.<internal-domain>
   # Should return the MetalLB IP assigned to the syslog service
   ```

4. Configure OPNsense (manual UI):
   **System → Settings → Logging → Destinations → Add:**
   - Transport: `TCP4` (Promtail syslog receiver is TCP-only)
   - Hostname: `syslog.<internal-domain>`
   - Port: `1514`
   - Facility: `local0`
   - Severity: `info`
   - RFC5424: enabled

5. Verify logs arrive in Loki:
   - Grafana → Explore → Loki → `{job="syslog"}` → last 15 minutes