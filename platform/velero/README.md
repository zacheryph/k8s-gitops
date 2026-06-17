# Velero Cluster Backup

Cluster-level backup and restore using Velero, backed by in-cluster MinIO (S3-compatible) and Longhorn CSI volume snapshots.

> **Kopia vs Velero:** Kopia (`services/backup/kopia/`) does per-PVC file-level backup of specific volumes (e.g., the 8 TB immich photos library). Velero backs up entire cluster resources — Deployments, Services, ConfigMaps, Secrets, PVCs (via CSI snapshots) — and is the right tool for full-namespace or full-cluster restore. They complement each other; neither replaces the other.

---

## Table of contents

- [Architecture](#architecture)
- [What's deployed](#whats-deployed)
- [One-time bootstrap](#one-time-bootstrap)
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
│  Velero Deployment          Velero Node-Agent (DaemonSet)          │
│  ┌──────────────────┐      ┌──────────────────────────────┐      │
│  │ velero server    │      │ node-agent pod (per node)    │      │
│  │                  │      │  - fs-backup (hostPath/NFS)  │      │
│  │  schedules ──────┼──┐   │  - CSI snapshot orchestration│      │
│  │  backups   ──────┼─┐│   └──────────────────────────────┘      │
│  │  restores  ──────┼┐││                                          │
│  └────────┬─────────┘│││   Longhorn CSI Driver                     │
│           │          │││   ┌──────────────────────────────┐      │
│           │ S3 API   │││   │ VolumeSnapshot → Snapshot     │      │
│           ▼          │││   │  (point-in-time PVC clone)    │      │
│  ┌──────────────────┐│││   └──────────────────────────────┘      │
│  │ MinIO (in-cluster)│││                                          │
│  │ bucket: velero    │││                                          │
│  │ prefix: backups/  │││                                          │
│  └──────────────────┘│││                                          │
└──────────────────────┼┼┼──────────────────────────────────────────┘
                       │││
           ┌───────────┘│└───────────── S3 backups (JSON metadata)
           │            └────────────── PVC backups (CSI snapshots)
           ▼
  ┌────────────────────────────────────────┐
  │  MinIO bucket `velero/`                │
  │  backups/<name>/                        │
  │    ├── velero-backup.json               │
  │    ├── <name>-logs.gz                   │
  │    ├── <name>-volumesnapshots.json.gz    │
  │    ├── <name>-csi-volumesnapshot-*.json │
  │    └── <name>-tar.gz (node-agent fs)    │
  └────────────────────────────────────────┘
```

**Backend:** MinIO (S3-compatible) running in the `minio` namespace.

**Plugins:**
- `velero-plugin-for-aws` — S3 object store backend (v1.13.0)
- `velero-plugin-for-csi` — CSI VolumeSnapshot integration (v0.11.0)

**Schedules:**

| Schedule | When | Scope | TTL |
|---|---|---|---|
| `daily-cluster` | 01:00 daily | All namespaces (excl. system) | 30 days |
| `daily-critical` | 00:00 daily | immich, minio, home-assistant, forgejo | 30 days |
| `weekly-full` | Sun 03:00 | All namespaces (excl. system) | 90 days |

**Per-app schedules** (in `schedules/`):

| Schedule | When | Scope | TTL |
|---|---|---|---|
| `immich-media` | 23:00 daily | immich namespace + label filter | 30 days |

---

## What's deployed

| Resource | Namespace | Purpose |
|---|---|---|
| `Namespace/velero` | — | Velero's home |
| `OCIRepository/velero` | velero | Helm chart source (VMware Tanzu) |
| `HelmRelease/velero` | velero | Velero server + node-agent + schedules |
| `Secret/velero-s3-credentials` | velero | MinIO access key (created out-of-band) |
| `Schedule/daily-cluster` | velero | Cluster-wide daily backup |
| `Schedule/daily-critical` | velero | Critical namespaces daily |
| `Schedule/weekly-full` | velero | Weekly full backup (90d retention) |
| `Schedule/immich-media` | velero | Per-app immich backup |
| `Kustomization/platform-velero` | flux-system | Flux pointer |

---

## One-time bootstrap

After this PR merges and Flux reconciles `platform-velero`:

### 1. Create the MinIO user and bucket for Velero

Velero needs its own MinIO credentials (access key + secret key) and a dedicated bucket.

```bash
# Create a MinIO alias for the cluster's MinIO
kubectl -n minio exec deploy/minio -- mc alias set local http://localhost:9000 \
  ${MINIO_ROOT_USERNAME} ${MINIO_ROOT_PASSWORD}

# Create a dedicated user for Velero
kubectl -n minio exec deploy/minio -- mc admin user add local \
  <velero-access-key> <velero-secret-key>

# Attach the built-in readwrite policy
kubectl -n minio exec deploy/minio -- mc admin policy attach local readwrite \
  --user <velero-access-key>

# Create the velero bucket
kubectl -n minio exec deploy/minio -- mc mb local/velero
```

### 2. Create the S3 credentials Secret

```bash
kubectl create secret generic velero-s3-credentials \
  -n velero \
  --from-literal=cloud="[default]
aws_access_key_id=<velero-access-key>
aws_secret_access_key=<velero-secret-key>"
```

### 3. Let Flux reconcile

```bash
flux reconcile kustomization platform-velero
# Wait ~30s for the HelmRelease to install Velero
kubectl -n velero get pods -w
```

### 4. Verify

```bash
# Velero server is running
kubectl -n velero get deploy/velero

# Schedules are created
velero schedule get

# Node-agent DaemonSet is running
kubectl -n velero get ds/node-agent

# First backup fires (or trigger one manually to test)
velero backup create test-bootstrap --wait
```

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

# List all schedules
velero schedule get

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
  --include-namespaces immich,minio \
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
# Via Velero CLI
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
  --include-resources persistentvolumeclaims \
  --selector "app.kubernetes.io/name=minio"
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
# 1. Install Velero on the fresh cluster, point it at the same MinIO bucket
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

**Restore order matters:**
1. CRDs first (Velero backs these up)
2. Cluster-scoped resources
3. Namespaced resources
4. PVCs / CSI snapshots
5. Flux reconciles to bring state current

Velero handles the ordering automatically for single-restore operations. For multi-step restores, use `--include-cluster-resources` and `--include-namespaces` to split the restore into phases.

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

**Verification:**

```bash
# Check the VolumeSnapshotContent was created
kubectl get volumesnapshotcontent | grep immich

# In Longhorn UI: Volume → Snapshot — the snapshot appears with
# "Created by Velero" label
```

---

## CSI snapshots

Velero uses Longhorn's CSI driver to create point-in-time volume snapshots.

**How it works:**
1. Velero creates a `VolumeSnapshot` custom resource
2. The `snapshot-controller` (running in `kube-system`) creates a `VolumeSnapshotContent`
3. Longhorn's CSI driver creates the actual block-level snapshot
4. On restore, Velero creates a PVC from the `VolumeSnapshot`, and Longhorn provisions a new volume with the snapshot data

**Snapshots are stored in Longhorn, not MinIO.** The MinIO bucket stores backup metadata (JSON manifests) and node-agent filesystem backups. CSI snapshot data lives inside Longhorn's volume storage on the cluster nodes.

**What gets a CSI snapshot vs filesystem backup:**

| Volume type | Method | Notes |
|---|---|---|
| Longhorn PVC | CSI snapshot | Point-in-time, capacity-efficient |
| hostPath | node-agent (fs-backup) | Tar'd to MinIO |
| NFS | node-agent (fs-backup) | Tar'd to MinIO |
| emptyDir | Not backed up | Ephemeral by design |

**Disaster recovery note:** CSI snapshots are stored on-cluster (inside Longhorn). If the cluster's disks die, CSI snapshot data is lost. The MinIO bucket contains resource manifests + filesystem backups, so you can restore resource definitions from MinIO, but PVC data backed up via CSI snapshots needs the Longhorn volumes to survive. For offsite PVC data protection, Kopia's `sync-to B2` pattern (`services/backup/kopia/`) is the complement — Tier 1 = Velero CSI snapshots (fast, on-cluster), Tier 2 = Kopia B2 sync (offsite).

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
# - MinIO unreachable (check MinIO pod, S3 URL config)
# - Node-agent DaemonSet not ready on a node
```

### "Failed to get backup store" / "No such bucket"

Velero can't reach MinIO or the bucket doesn't exist:

```bash
# Verify MinIO is running
kubectl -n minio get pods

# Check velero server logs for S3 errors
kubectl -n velero logs deploy/velero | grep -i "s3\|minio\|bucket"

# Verify the bucket exists
kubectl -n minio exec deploy/minio -- mc ls local/

# Re-create credentials if needed
kubectl -n velero delete secret velero-s3-credentials
kubectl create secret generic velero-s3-credentials -n velero \
  --from-literal=cloud="[default]
aws_access_key_id=<correct-key>
aws_secret_access_key=<correct-secret>"
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

### TLS certificate errors

If Velero can't verify MinIO's TLS certificate:

```bash
# Check if the cert is trusted
kubectl -n velero exec deploy/velero -- curl -v https://minio.minio.svc:9000

# Temporary fix (for debugging):
# Add to helmrelease.yaml values:
#   configuration:
#     backupStorageLocation:
#       - config:
#           insecureSkipTLSVerify: "true"
# Then: flux reconcile kustomization platform-velero

# Permanent fix: ensure MinIO's cert is issued by a CA the pods trust,
# or mount the CA bundle into the Velero pod.
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
# 4. Velero is deployed, connects to the existing MinIO bucket
# 5. Restore from the latest weekly-full backup
velero restore create full-restore \
  --from-backup weekly-full \
  --restore-volumes=true

# 6. Flux reconciles, overwriting with current git state
#    (restore provides PVC data + resource baseline; Flux updates config)
flux reconcile kustomization --all
```

### Scenario 5: MinIO bucket lost (Velero metadata gone)

**Impact:** Velero metadata + filesystem backups are gone. CSI snapshots still exist in Longhorn.

```bash
# 1. Recreate the bucket and credentials
kubectl -n minio exec deploy/minio -- mc mb local/velero

# 2. Restart Velero (it sees an empty bucket and starts fresh)
kubectl -n velero rollout restart deploy/velero

# 3. Run a new full backup immediately
velero backup create post-recovery --wait

# 4. CSI snapshots from before the loss still exist in Longhorn,
#    but Velero's metadata linking backups → snapshots is gone.
#    For critical PVCs, manually create PVCs from surviving snapshots:
kubectl get volumesnapshot -A
```

---

## Operational notes

- **Velero consumes ~128-512 MB RAM** for the server; the node-agent runs on every node with modest resources (~50m CPU, 64Mi RAM).
- **Backup size in MinIO** varies: metadata is small (KB-MB), node-agent filesystem backups can be large (GB). CSI snapshots don't consume MinIO space.
- **MinIO bucket lifecycle:** Set a lifecycle rule on the `velero` bucket to expire incomplete multipart uploads after 7 days (prevents orphaned uploads from failed backups).
- **`prune: true` on the Flux Kustomization** means Flux will delete Velero resources if the kustomization is removed from git.
- **Plugin updates:** When Renovate bumps the chart version, verify plugin compatibility. The chart bundles specific plugin versions that may need manual updating in `initContainers`.
- **Supplement with Kopia for offsite PVC data:** Velero's CSI snapshots live on-cluster (Longhorn). For offsite PVC data protection, Kopia's `sync-to B2` (`services/backup/kopia/`) is the complement. Together they form a two-tier strategy: Velero CSI snapshots (fast, on-cluster, resource + volume) + Kopia B2 sync (offsite, file-level, per-PVC).
- **Schedule overlap is fine.** `daily-cluster` at 01:00, `daily-critical` at 00:00, `immich-media` at 23:00 — they back up overlapping data but from different scopes and for different purposes. The extra backups cost MinIO metadata space (KB-MB each) but give you more restore granularity.
