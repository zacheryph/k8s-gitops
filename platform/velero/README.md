# Velero Cluster Backup

Cluster-level backup and restore using Velero, backed by Backblaze B2 (S3-compatible) and Longhorn CSI volume snapshots. Annotation-driven via Kyverno — annotate a PVC or HelmRelease and Kyverno generates the backup schedule.

> **Kopia vs Velero:** Kopia (`services/backup/kopia/`) does per-PVC file-level backup to offsite B2. Velero backs up entire cluster resources — Deployments, Services, ConfigMaps, Secrets, PVCs (via CSI snapshots) — and is the right tool for full-namespace or full-cluster restore. They complement each other; neither replaces the other.

---

## Table of contents

- [Architecture](#architecture)
- [What's deployed](#whats-deployed)
- [One-time bootstrap](#one-time-bootstrap)
- [Annotation-driven backup (Kyverno)](#annotation-driven-backup-kyverno)
- [Backup operations](#backup-operations)
  - [View existing backups](#view-existing-backups)
  - [Create a manual backup](#create-a-manual-backup)
  - [Create a scheduled backup](#create-a-scheduled-backup)
  - [Describe a backup](#describe-a-backup)
  - [Delete old backups](#delete-old-backups)
- [Restore operations](#restore-operations)
  - [Restore a single resource](#restore-a-single-resource)
  - [Restore an entire namespace](#restore-an-entire-namespace)
  - [Restore a whole-cluster backup](#restore-a-whole-cluster-backup)
  - [Restore into a different namespace](#restore-into-a-different-namespace)
  - [Restore PVC data from a CSI snapshot](#restore-pvc-data-from-a-csi-snapshot)
- [CSI snapshots](#csi-snapshots)
- [Troubleshooting](#troubleshooting)
- [Recovery scenarios](#recovery-scenarios)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Cluster                                                          │
│                                                                   │
│  Kyverno ClusterPolicy              Velero Deployment             │
│  ┌────────────────────┐            ┌──────────────────┐           │
│  │ watches PVCs +     │            │ velero server    │           │
│  │ HelmReleases for   │ generate   │                  │           │
│  │ backup.zacheryph/  │───────────▶│  schedules ──────┼──┐        │
│  │ enabled annotation │  Schedule  │  backups   ──────┼─┐│        │
│  └────────────────────┘            │  restores  ──────┼┐││        │
│                                    └──────────────────┘│││        │
│                                                         │││        │
│  Velero Node-Agent (DaemonSet)     Longhorn CSI Driver  │││        │
│  ┌──────────────────────────┐     ┌────────────────┐    │││        │
│  │ node-agent pod (per node)│     │ VolumeSnapshot  │    │││        │
│  │  - fs-backup (hostPath)  │     │  → Snapshot     │    │││        │
│  │  - CSI orchestration     │     │  (point-in-time)│    │││        │
│  └──────────────────────────┘     └────────────────┘    │││        │
│                                                         │││        │
└─────────────────────────────────────────────────────────┼┼┼────────┘
                                                          │││
           ┌──────────────────────────────────────────────┘││
           │           ┌───────────────────────────────────┘│
           │           │       ┌───────────────────────────┘
           ▼           ▼       ▼
  ┌─────────────────────────────────────────┐
  │  Backblaze B2                           │
  │  bucket: <b2-bucket>                    │
  │  prefix: backups/                        │
  │  backups/<name>/                         │
  │    ├── velero-backup.json                │
  │    ├── <name>-logs.gz                    │
  │    ├── <name>-volumesnapshots.json.gz    │
  │    └── <name>-csi-volumesnapshot-*.json  │
  └─────────────────────────────────────────┘
```

**Backend:** Backblaze B2 (S3-compatible). Bucket + application key created in the B2 console.

**Plugins:**
- `velero-plugin-for-aws` — S3 object store backend (v1.13.0)
- `velero-plugin-for-csi` — CSI VolumeSnapshot integration (v0.11.0)

**Schedules (built-in, managed by the chart):**

| Schedule | When | Scope | TTL |
|---|---|---|---|
| `daily-cluster` | 01:00 daily | All namespaces (excl. system) | 30 days |
| `daily-critical` | 00:00 daily | immich, minio, home-assistant, forgejo | 30 days |
| `weekly-full` | Sun 03:00 | All namespaces (excl. system) | 90 days |

**Annotation-driven schedules (Kyverno-generated):**

Annotate any PVC or HelmRelease with `backup.zacheryph/enabled: "true"` and Kyverno generates a `Schedule` in the `velero` namespace. See [Annotation-driven backup](#annotation-driven-backup-kyverno) below.

---

## What's deployed

| Resource | Namespace | Purpose |
|---|---|---|
| `Namespace/velero` | — | Velero's home |
| `OCIRepository/velero` | velero | Helm chart source (VMware Tanzu) |
| `HelmRelease/velero` | velero | Velero server + node-agent + schedules |
| `Secret/velero-s3-credentials` | velero | B2 application key (created out-of-band) |
| `ClusterPolicy/generate-velero-backup` | — | Kyverno policy: annotation → Schedule |
| `Schedule/daily-cluster` | velero | Cluster-wide daily backup |
| `Schedule/daily-critical` | velero | Critical namespaces daily |
| `Schedule/weekly-full` | velero | Weekly full backup (90d retention) |
| `Kustomization/platform-velero` | flux-system | Flux pointer |

---

## One-time bootstrap

After this PR merges and Flux reconciles `platform-velero`:

### 1. Create the B2 bucket and application key

In the [Backblaze B2 web console](https://secure.backblaze.com):

1. **Create a bucket** — name it `velero-<cluster>` (e.g., `velero-homelab`). Private. Default encryption is fine (Velero encrypts client-side, so B2's server-side encryption is a bonus second layer, not required).
2. **Create an Application Key** scoped to that one bucket with `readFiles`, `writeFiles`, `listFiles`, `deleteFiles` — **NOT** the master key. Copy the `keyID` and `applicationKey`.
3. **Note the endpoint and region.** For US West: endpoint `s3.us-west-004.backblazeb2.com`, region `us-west-004`. Check the bucket's page for the exact endpoint.

### 2. Add B2 config to cluster-secrets

Add these to the `cluster-secrets` Secret in `flux-system` (it's SOPS-encrypted):

```yaml
VELERO_B2_BUCKET: velero-homelab
VELERO_B2_REGION: us-west-004
VELERO_B2_ENDPOINT: s3.us-west-004.backblazeb2.com
```

### 3. Create the S3 credentials Secret

```bash
kubectl create secret generic velero-s3-credentials \
  -n velero \
  --from-literal=cloud="[default]
aws_access_key_id=<keyID>
aws_secret_access_key=<applicationKey>"
```

### 4. Let Flux reconcile

```bash
flux reconcile kustomization platform-velero
# Wait ~30s for the HelmRelease to install Velero
kubectl -n velero get pods -w
```

### 5. Verify

```bash
# Velero server is running
kubectl -n velero get deploy/velero

# Schedules are created
velero schedule get

# Node-agent DaemonSet is running
kubectl -n velero get ds/node-agent

# Kyverno policy is ready
kubectl get clusterpolicy generate-velero-backup

# First backup fires (or trigger one manually to test)
velero backup create test-bootstrap --wait
```

---

## Annotation-driven backup (Kyverno)

Instead of writing `Schedule` YAML files for each app, annotate the PVC or HelmRelease. Kyverno's `generate-velero-backup` ClusterPolicy watches for the annotation and creates a `Schedule` in the `velero` namespace.

### Annotation reference

| Annotation | Required | Default | Description |
|---|---|---|---|
| `backup.zacheryph/enabled` | Yes | — | Set to `"true"` to trigger schedule generation |
| `backup.zacheryph/schedule` | No | `"0 1 * * *"` | Cron expression for the backup |
| `backup.zacheryph/ttl` | No | `"720h0m0s"` | How long to keep backups (30 days) |

### PVC-level annotation

Annotate a PVC to back up its namespace. Good for data-heavy workloads where the PVC is the thing you care about.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  namespace: immich
  name: photos-pool
  annotations:
    backup.zacheryph/enabled: "true"
    backup.zacheryph/schedule: "0 23 * * *"   # daily at 23:00
    backup.zacheryph/ttl: "720h0m0s"          # keep 30 days
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 8Ti
  storageClassName: longhorn
```

Kyverno generates: `Schedule/auto-pvc-immich-photos-pool` in the `velero` namespace, backing up the `immich` namespace.

### HelmRelease-level annotation

Annotate a HelmRelease to back up its entire namespace — resources + PVCs. Good for app-level coverage.

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  namespace: immich
  name: immich
  annotations:
    backup.zacheryph/enabled: "true"
    backup.zacheryph/schedule: "0 2 * * *"
    backup.zacheryph/ttl: "2160h0m0s"         # keep 90 days for app config
spec:
  # ...
```

Kyverno generates: `Schedule/auto-hr-immich-immich` in the `velero` namespace.

### Lifecycle

- **Add annotation** → Kyverno creates the `Schedule`. Flux applies it. Velero starts backing up.
- **Change `schedule` or `ttl`** → Kyverno updates the `Schedule`.
- **Remove annotation (or set to `"false"`)** → Kyverno deletes the `Schedule`. Existing backups remain until their TTL expires.
- **Remove the PVC/HelmRelease** → Kyverno deletes the `Schedule` (it's owned by the source resource via `synchronize: true`).

### Multiple annotations per namespace

If a namespace has multiple annotated PVCs/HelmReleases, each generates its own `Schedule`. They'll run overlapping backups — fine for 1-2 per namespace (metadata is KB-MB). For 5+ annotated resources in one namespace, use a single annotation instead of one per resource.

---

## Backup operations

All commands use the `velero` CLI. Install it:

```bash
# macOS
brew install velero

# Linux
wget https://github.com/vmware-tanzu/velero/releases/download/v1.18.1/velero-v1.18.1-linux-amd64.tar.gz
tar xzf velero-v1.18.1-linux-amd64.tar.gz
sudo mv velero-v1.18.1-linux-amd64/velero /usr/local/bin/
```

The `velero` CLI reads kubeconfig from `~/.kube/config` by default. Use `--kubeconfig` to point at a different config file.

### View existing backups

```bash
# List all backups
velero backup get

# List all schedules (built-in + Kyverno-generated)
velero schedule get

# Show only Kyverno-generated schedules
kubectl -n velero get schedule -l backup.zacheryph/generated-by=kyverno

# Show backups for a specific schedule
velero backup get --selector velero.io/schedule-name=daily-cluster

# Show backups with a custom label
velero backup get --selector backup-tier=daily-critical
```

### Create a manual backup

```bash
# Full cluster (exclude system namespaces)
velero backup create manual-$(date +%Y%m%d-%H%M) \
  --exclude-namespaces kube-system,kube-public,kube-node-lease,flux-system,velero,longhorn-system \
  --wait

# Single namespace with volume snapshots
velero backup create immich-manual-$(date +%Y%m%d-%H%M) \
  --include-namespaces immich \
  --snapshot-volumes \
  --wait

# Specific resources only (no volumes)
velero backup create configs-only-$(date +%Y%m%d-%H%M) \
  --include-namespaces immich,home-assistant \
  --include-resources configmaps,secrets \
  --snapshot-volumes=false \
  --wait

# With a label selector
velero backup create immich-db-$(date +%Y%m%d-%H%M) \
  --include-namespaces immich \
  --selector "cnpg.io/cluster=immich-postgresql-18" \
  --snapshot-volumes \
  --wait
```

### Create a scheduled backup

```bash
# Via Velero CLI (also possible via Kyverno annotation — preferred)
velero schedule create home-automation-daily \
  --schedule="0 2 * * *" \
  --include-namespaces home-assistant,zwave-js \
  --ttl 720h0m0s

# Check it was created
velero schedule get home-automation-daily
```

For GitOps-managed schedules, add a `Schedule` CR to `platform/velero/schedules/` and append to `kustomization.yaml`. Example:

```yaml
apiVersion: velero.io/v1
kind: Schedule
metadata:
  name: my-app
  namespace: velero
spec:
  schedule: "0 2 * * *"
  template:
    ttl: 720h0m0s
    includedNamespaces:
      - my-namespace
    snapshotVolumes: true
```

### Describe a backup

```bash
# High-level summary
velero backup describe <backup-name>

# Detailed log (includes every resource backed up)
velero backup describe <backup-name> --details

# Logs from the backup pod (if the backup failed)
velero backup logs <backup-name>
```

### Delete old backups

```bash
# Delete a single backup + associated snapshots + S3 data
velero backup delete <backup-name>

# Delete all backups matching a label
velero backup delete --selector backup-tier=daily --confirm
```

> TTL set on the schedule handles automatic expiration — backups age out according to their schedule's `ttl` field. Manual deletes are for cleanup of ad-hoc backups or pre-expiry pruning.

---

## Restore operations

### Restore a single resource

```bash
# Restore one ConfigMap into its original namespace
velero restore create restore-forgejo-config \
  --from-backup daily-critical \
  --include-namespaces forgejo \
  --include-resources configmaps

# Restore a specific named resource
velero restore create restore-minio-pvc \
  --from-backup weekly-full \
  --include-namespaces minio \
  --include-resources persistentvolumeclaims
```

### Restore an entire namespace

```bash
# Full namespace restore (resources + PVCs via CSI snapshots)
velero restore create restore-immich \
  --from-backup daily-critical \
  --include-namespaces immich

# Check restore status
velero restore describe restore-immich
velero restore logs restore-immich
```

**Important:** Velero won't overwrite existing resources by default. If the namespace still exists with running workloads, delete the namespace first:

```bash
kubectl delete namespace immich
velero restore create restore-immich --from-backup daily-critical \
  --include-namespaces immich
```

### Restore a whole-cluster backup

**⚠️ Destructive — read carefully before running.**

A full cluster restore from a `weekly-full` backup. Only do this when rebuilding the cluster from scratch or recovering from catastrophic failure.

```bash
# 1. Install Velero on the fresh cluster, point it at the same B2 bucket
#    (the bootstrap section above handles this via Flux)

# 2. List available backups
velero backup get

# 3. Restore everything
velero restore create full-restore \
  --from-backup weekly-full \
  --restore-volumes=true

# 4. Check progress
velero restore describe full-restore
velero restore logs full-restore

# 5. After restore completes, Flux will reconcile and may overwrite resources
#    with the current git state. This is expected — the restore provides
#    the baseline state; Flux brings it current.
flux reconcile kustomization --all
```

### Restore into a different namespace

Use `--namespace-mappings` to remap namespaces during restore:

```bash
# Restore immich into a test namespace for validation
velero restore create test-immich-restore \
  --from-backup daily-critical \
  --include-namespaces immich \
  --namespace-mappings immich:immich-restore-test

# Verify the restore looks good, then delete
kubectl get all -n immich-restore-test
kubectl delete namespace immich-restore-test
```

This pattern is useful for:
- Testing restore integrity without affecting live workloads
- Cloning a namespace for staging/development
- Post-drill validation before declaring a restore successful

### Restore PVC data from a CSI snapshot

Velero creates `VolumeSnapshot` objects during backup. To restore:

```bash
# 1. Find the VolumeSnapshot
kubectl get volumesnapshot -n immich

# 2. Restore the backup — Velero automatically provisions PVCs
#    from the VolumeSnapshot data
velero restore create restore-photos-pvc \
  --from-backup daily-critical \
  --include-namespaces immich \
  --include-resources persistentvolumeclaims

# 3. Check that the PVC was created and is bound
kubectl -n immich get pvc

# 4. The PVC data comes from the Longhorn snapshot created by Velero.
#    Longhorn handles the snapshot → volume provisioning automatically.
```

---

## CSI snapshots

Velero uses Longhorn's CSI driver to create point-in-time volume snapshots.

**How it works:**
1. Velero creates a `VolumeSnapshot` custom resource
2. The `snapshot-controller` (running in `kube-system`) creates a `VolumeSnapshotContent`
3. Longhorn's CSI driver creates the actual block-level snapshot
4. On restore, Velero creates a PVC from the `VolumeSnapshot`, and Longhorn provisions a new volume with the snapshot data

**Snapshots are stored in Longhorn, not B2.** The B2 bucket stores backup metadata (JSON manifests) and node-agent filesystem backups. CSI snapshot data lives inside Longhorn's volume storage on the cluster nodes.

**What gets a CSI snapshot vs filesystem backup:**

| Volume type | Method | Notes |
|---|---|---|
| Longhorn PVC | CSI snapshot | Point-in-time, capacity-efficient |
| hostPath | node-agent (fs-backup) | Tar'd to B2 |
| NFS | node-agent (fs-backup) | Tar'd to B2 |
| emptyDir | Not backed up | Ephemeral by design |

**Disaster recovery note:** CSI snapshots are stored on-cluster (inside Longhorn). If the cluster's disks die, CSI snapshot data is lost. The B2 bucket contains resource manifests + filesystem backups, so you can restore resource definitions from B2, but PVC data backed up via CSI snapshots needs the Longhorn volumes to survive. For offsite PVC data protection, Kopia's `sync-to B2` (`services/backup/kopia/`) is the complement.

---

## Troubleshooting

### Backup stuck "InProgress"

```bash
# Check backup details
velero backup describe <name> --details

# Check the backup pod logs
kubectl -n velero logs -l velero.io/backup-name=<name>

# Common causes:
# - CSI snapshot timeout (Longhorn takes >10 min for large volumes)
# - B2 unreachable (network, expired credentials)
# - Node-agent DaemonSet not ready on a node
```

### "Failed to get backup store" / "No such bucket"

Velero can't reach B2 or the bucket doesn't exist:

```bash
# Check velero server logs for S3 errors
kubectl -n velero logs deploy/velero | grep -i "s3\|b2\|bucket"

# Verify the bucket exists in the B2 console
# Verify the application key still has access (they can be revoked)

# Verify cluster-secrets values are set correctly
kubectl -n flux-system get secret cluster-secrets -o jsonpath='{.data.VELERO_B2_BUCKET}' | base64 -d

# Re-create credentials if needed
kubectl -n velero delete secret velero-s3-credentials
kubectl create secret generic velero-s3-credentials -n velero \
  --from-literal=cloud="[default]
aws_access_key_id=<correct-keyID>
aws_secret_access_key=<correct-applicationKey>"
kubectl -n velero rollout restart deploy/velero
```

### CSI snapshot fails

```bash
# Check that the CSI plugin is loaded
kubectl -n velero logs deploy/velero | grep -i "csi"

# Check VolumeSnapshotClass exists
kubectl get volumesnapshotclass

# Check snapshot-controller is running
kubectl -n kube-system get pods -l app=snapshot-controller

# Manual CSI snapshot test
kubectl create -f - <<EOF
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: test-snapshot
  namespace: immich
spec:
  volumeSnapshotClassName: longhorn
  source:
    persistentVolumeClaimName: <some-pvc>
EOF
kubectl -n immich get volumesnapshot test-snapshot
kubectl -n immich delete volumesnapshot test-snapshot
```

### Schedule not firing

```bash
# Check schedule status
velero schedule get
velero schedule describe <schedule-name>

# Check the schedule CR directly
kubectl -n velero get schedule <schedule-name> -o yaml

# Manual trigger from a schedule
velero backup create --from-schedule <schedule-name> --wait
```

### Kyverno policy not generating schedules

```bash
# Check the ClusterPolicy is applied
kubectl get clusterpolicy generate-velero-backup

# Check Kyverno logs for generation errors
kubectl -n kyverno logs -l app.kubernetes.io/name=kyverno | grep generate-velero-backup

# Verify the annotation is correct (case-sensitive, quoted "true")
kubectl -n <namespace> get <kind>/<name> -o jsonpath='{.metadata.annotations}'

# Manual test: create a throwaway PVC with the annotation
kubectl -n default apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-kyverno
  annotations:
    backup.zacheryph/enabled: "true"
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 1Gi
  storageClassName: longhorn
EOF
# Check if the schedule was created
kubectl -n velero get schedule auto-pvc-default-test-kyverno
# Clean up
kubectl -n default delete pvc test-kyverno
```

### TLS / SSL errors (B2 endpoint)

```bash
# B2 uses public CA-issued certs — no custom CA needed.
# If you see TLS errors, verify the endpoint URL:
#   s3Url: https://s3.us-west-004.backblazeb2.com
# (Must include https:// prefix, no trailing slash)

# Test connectivity from the Velero pod
kubectl -n velero exec deploy/velero -- curl -v https://s3.us-west-004.backblazeb2.com
```

### Restore fails with "resource already exists"

Velero doesn't overwrite existing resources by default:

```bash
# Delete the conflicting resource first
kubectl -n <namespace> delete <kind> <name>

# Or delete the entire namespace and restore into it
kubectl delete namespace <namespace>
velero restore create restore-<ns> --from-backup <backup> \
  --include-namespaces <namespace>
```

---

## Recovery scenarios

### Scenario 1: Single namespace accidentally deleted

**Impact:** One app/service is down. All other namespaces unaffected.

```bash
# 1. Find the latest backup covering this namespace
velero backup get | grep -E "(daily-critical|daily-cluster)"

# 2. Restore the namespace
velero restore create restore-<ns> \
  --from-backup daily-critical \
  --include-namespaces <namespace>

# 3. Verify
kubectl -n <namespace> get all
# Flux will reconcile automatically within 10 minutes
```

### Scenario 2: PVC accidentally deleted

**Impact:** Data loss for one volume. App may be degraded.

```bash
# 1. Restore just the PVC from the latest backup
velero restore create restore-<pvc-name> \
  --from-backup daily-critical \
  --include-namespaces <namespace> \
  --include-resources persistentvolumeclaims

# 2. Delete the old pod so it re-mounts the restored PVC
kubectl -n <namespace> delete pod <pod-name>
```

### Scenario 3: ConfigMap or Secret overwritten

**Impact:** App misconfiguration. Can be rolled back from a backup without touching PVC data.

```bash
# 1. Restore the specific resource into a temp namespace
velero restore create restore-config \
  --from-backup daily-critical \
  --include-namespaces <namespace> \
  --include-resources configmaps \
  --namespace-mappings <namespace>:temp-restore

# 2. Extract the correct value
kubectl -n temp-restore get configmap <name> -o yaml > /tmp/restored-cm.yaml

# 3. Apply it back (edit namespace in the file first)
sed -i 's/namespace: temp-restore/namespace: <namespace>/' /tmp/restored-cm.yaml
kubectl apply -f /tmp/restored-cm.yaml

# 4. Clean up
kubectl delete namespace temp-restore
```

### Scenario 4: Cluster rebuild from scratch

**Impact:** Full cluster failure. Rebuild from Velero backup + GitOps.

```bash
# 1. Provision new cluster (k0s, same config)
# 2. Bootstrap Flux (points at the same GitOps repo)
# 3. Flux reconciles core → platform → services in order
# 4. Add B2 config to cluster-secrets, create velero-s3-credentials
# 5. Velero is deployed, connects to the existing B2 bucket
# 6. Restore from the latest weekly-full backup
velero restore create full-restore \
  --from-backup weekly-full \
  --restore-volumes=true

# 7. Flux reconciles, overwriting with current git state
#    (restore provides PVC data + resource baseline; Flux updates config)
flux reconcile kustomization --all
```

### Scenario 5: B2 bucket lost (Velero metadata gone)

**Impact:** Velero metadata + filesystem backups are gone. CSI snapshots still exist in Longhorn.

```bash
# 1. Recreate the bucket and application key in B2 console
# 2. Update cluster-secrets if bucket name changed
# 3. Recreate velero-s3-credentials with new application key
kubectl -n velero delete secret velero-s3-credentials
kubectl create secret generic velero-s3-credentials -n velero \
  --from-literal=cloud="[default]
aws_access_key_id=<new-keyID>
aws_secret_access_key=<new-applicationKey>"

# 4. Restart Velero (it sees an empty bucket and starts fresh)
kubectl -n velero rollout restart deploy/velero

# 5. Run a new full backup immediately
velero backup create post-recovery --wait

# 6. CSI snapshots from before the loss still exist in Longhorn,
#    but Velero's metadata linking backups → snapshots is gone.
#    For critical PVCs, manually create PVCs from surviving snapshots:
kubectl get volumesnapshot -A
```

---

## Operational notes

- **Velero consumes ~128-512 MB RAM** for the server; the node-agent runs on every node with modest resources (~50m CPU, 64Mi RAM).
- **Backup size in B2** varies: metadata is small (KB-MB), node-agent filesystem backups can be large (GB). CSI snapshots don't consume B2 space (they live in Longhorn).
- **B2 costs:** Storage (~$6/TB/month) + download ($0.01/GB). Velero backups are incremental within a schedule, so ongoing storage grows slowly.
- **Application key scope:** Use a key scoped to the one bucket, not the master key. If the key is compromised, rotate it in the B2 console and update the Secret.
- **`prune: true` on the Flux Kustomization** means Flux will delete Velero resources if the kustomization is removed from git.
- **Plugin updates:** When Renovate bumps the chart version, verify plugin compatibility. The chart bundles specific plugin versions set in `initContainers`.
- **Supplement with Kopia for offsite PVC data:** Velero backs up to B2 (metadata + resource config). Kopia backs up to B2 (file-level PVC data). Together they give you full coverage: Velero = cluster resources + on-cluster CSI snapshots, Kopia = offsite file-level PVC data.
- **Schedule overlap is fine.** `daily-cluster` at 01:00, `daily-critical` at 00:00, Kyverno-generated schedules at configurable times — they may overlap but each costs only metadata space (KB-MB) in B2 and gives you more restore granularity.
