# Promtail Syslog Receiver for OPNsense Remote Logging

> **For Hermes:** Use subagent-driven-development to implement this plan task-by-task.

**Goal:** Add a Promtail syslog listener on port 1514 exposed via MetalLB so OPNsense can ship
its logs to Loki in real-time. Logs survive OPNsense reboots because Loki is the source of truth.

**Context:** OPNsense went down ~July 16 and the logs were lost on reboot (July 19). OPNsense has
no `os-loki` plugin — its syslog-ng only supports `udp4/tcp4/tls4` transports (no HTTP push).
The correct path is: OPNsense → syslog/UDP → Promtail (syslog listener) → Loki.

**Architecture:**
```
OPNsense (syslog-ng)
    │ UDP :1514
    ▼
Promtail DaemonSet (syslog scrape config)
    │ HTTP push
    ▼
Loki :3100 ( monitoring namespace)
```

**Existing stack:** Promtail chart 6.17.1 via OCIRepository, DaemonSet with 3 pods, pushing to
`http://loki:3100/loki/api/v1/push`. No extra ports or syslog scrapes currently configured.

**Metallb IP:** Use `${LOAD_BALANCER_SYSLOG}` in the manifest — user adds the actual IP to
`config/secrets.yaml` via `scripts/cluster-secrets set LOAD_BALANCER_SYSLOG 10.72.16.10`.
LoadBalancer services currently in use: .2 (plex), .4–.7 (kafka), .8 (envoy), .10 is free.

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
        service:
          port: 1514
          type: LoadBalancer
          externalTrafficPolicy: Local
          loadBalancerIP: ${LOAD_BALANCER_SYSLOG}
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
- MetalLB BGP/L2 assigns the `${LOAD_BALANCER_SYSLOG}` IP. OPNsense uses that IP as the
  syslog target.
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

1. Add the MetalLB IP to cluster-secrets **before** merging:
   ```bash
   sops config/secrets.yaml
   # Add: LOAD_BALANCER_SYSLOG: "10.72.16.10"   (or pick another free IP)
   git commit -am "chore(config): add LOAD_BALANCER_SYSLOG to cluster-secrets"
   ```
   Or: `./scripts/cluster-secrets set LOAD_BALANCER_SYSLOG 10.72.16.10`

2. Force Flux reconcile:
   ```bash
   flux reconcile kustomization platform-monitoring --with-source
   ```

3. Verify Promtail syslog Service got its LB IP:
   ```bash
   kubectl get svc -n monitoring -l app.kubernetes.io/name=promtail
   ```

4. Configure OPNsense ( manual UI):
   **System → Settings → Logging → Destinations → Add:**
   - Transport: `UDP4`
   - IP: `<LOAD_BALANCER_SYSLOG>`
   - Port: `1514`
   - Facility: `local0`
   - Severity: `info`
   - RFC5424: enabled

5. Verify logs arrive in Loki:
   - Grafana → Explore → Loki → `{job="syslog"}` → last 15 minutes