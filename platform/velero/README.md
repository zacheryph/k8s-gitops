# Velero Cluster Backup

Cluster-level backup and restore using Velero, backed by Backblaze B2 (S3-compatible) and Longhorn CSI volume snapshots. Backups are driven by a small set of built-in `Schedule`s defined in the HelmRelease; add more by committing `Schedule` CRs to this directory.

> **Kopia vs Velero:** Kopia (`services/backup/kopia/`) does per-PVC file-level backup to offsite B2. Velero backs up cluster resources — Deployments, Services, ConfigMaps, Secrets, PVC/PV objects, plus Longhorn CSI snapshots for app/DB volumes — and is the right tool for full-namespace or full-cluster restore. They complement each other; neither replaces the other.

> **What Velero does NOT back up here:** the large NFS-backed media libraries (`media-pool` 4Ti, `photos-pool` 8Ti) are deliberately skipped via the `velero-volume-policy` resource policy — that data lives on the NAS and is covered by Kopia, not Velero. CSI snapshots are also on-cluster (Longhorn), not in B2; see [CSI snapshots](#csi-snapshots).

---

## Table of contents

- [Architecture](#architecture)
- [What's deployed](#whats-deployed)
- [One-time bootstrap](#one-time-bootstrap)
- [Adding a backup schedule](#adding-a-backup-schedule)
- [Excluding resources and volumes](#excluding-resources-and-volumes)
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
│                                     Velero Deployment             │
│                                    ┌──────────────────┐           │
│                                    │ velero server    │           │
│  HelmRelease schedules ───────────▶│  schedules ──────┼──┐        │
│  (daily-cluster, daily-critical,   │  backups   ──────┼─┐│        │
│   weekly-full) + any Schedule CRs  │  restores  ──────┼┐││        │
│  committed to this dir             └──────────────────┘│││        │
│                                                         │││        │
│  Velero Node-Agent (DaemonSet)     Longhorn CSI Driver  │││        │
│  ┌──────────────────────────┐     ┌────────────────┐    │││        │
│  │ node-agent pod (per node)│     │ VolumeSnapshot  │    │││        │
│  │  - fs-backup (hostPath)  │     │  → Snapshot     │    │││        │
│  │  - CSI orchestration     │     │  (point-in-time)│    │││        │
│  └──────────────────────────┘     └────────────────┘    │││        │
│                                                         │││        │
│  resourcePolicy: velero-volume-policy → skip NFS volumes│││        │
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

**Plugin:**
- `velero-plugin-for-aws` — S3 object store backend (v1.13.0). CSI VolumeSnapshot support is built into Velero core (v1.14+), enabled with the `EnableCSI` feature flag — no separate CSI plugin.

**Schedules (built-in, managed by the chart):**

| Schedule | When | Scope | Volumes | TTL |
|---|---|---|---|---|
| `daily-cluster` | 01:00 daily | All namespaces (excl. system) | manifests only | 30 days |
| `daily-critical` | 00:00 daily | immich, minio, home-assistant, forgejo | CSI snapshots (NFS skipped) | 30 days |
| `weekly-full` | Sun 03:00 | All namespaces (excl. system) | manifests only | 90 days |

The broad sweeps (`daily-cluster`, `weekly-full`) are **manifests only** (`snapshotVolumes: false`) — they protect against "I deleted a resource" without snapshotting every PVC daily. Only `daily-critical` takes Longhorn CSI snapshots, and the `velero-volume-policy` resource policy skips NFS media volumes within it. See [Excluding resources and volumes](#excluding-resources-and-volumes).

---

## What's deployed

| Resource | Namespace | Purpose |
|---|---|---|
| `Namespace/velero` | — | Velero's home |
| `OCIRepository/velero` | velero | Helm chart source (VMware Tanzu) |
| `HelmRelease/velero` | velero | Velero server + node-agent + schedules |
| `Secret/velero-s3-credentials` | velero | B2 application key (rendered from cluster-secrets) |
| `ConfigMap/velero-volume-policy` | velero | Volume policy: skip NFS-backed media volumes |
| `Schedule/daily-cluster` | velero | Cluster-wide daily backup (manifests) |
| `Schedule/daily-critical` | velero | Critical namespaces daily (with snapshots) |
| `Schedule/weekly-full` | velero | Weekly full backup, manifests (90d retention) |

Velero is applied by the `platform` Flux Kustomization (it's listed in `platform/kustomization.yaml`), not a dedicated Flux pointer.

---

## One-time bootstrap

After this PR merges:

### 1. Create the B2 bucket and application key

In the [Backblaze B2 web console](https://secure.backblaze.com):

1. **Create a bucket** — name it `velero-<cluster>` (e.g., `velero-homelab`). Private. Default encryption is fine (Velero encrypts client-side, so B2's server-side encryption is a bonus second layer, not required).
2. **Create an Application Key** scoped to that one bucket with `readFiles`, `writeFiles`, `listFiles`, `deleteFiles` — **NOT** the master key. Copy the `keyID` and `applicationKey`.
3. **Note the endpoint and region.** For US West: endpoint `s3.us-west-004.backblazeb2.com`, region `us-west-004`. Check the bucket's page for the exact endpoint.

### 2. Add B2 config + credentials to cluster-secrets

Everything goes into the SOPS-encrypted `cluster-secrets` Secret — no out-of-band `kubectl create secret`. The `velero-s3-credentials` Secret (`secret.yaml`) and the HelmRelease are rendered from these via Flux substitution:

```bash
sops config/secrets.yaml
```

Add:

```yaml
VELERO_B2_BUCKET: velero-homelab
VELERO_B2_REGION: us-west-004
VELERO_B2_ENDPOINT: s3.us-west-004.backblazeb2.com
VELERO_B2_KEY_ID: <keyID>
VELERO_B2_APPLICATION_KEY: <applicationKey>
```

### 3. Let Flux reconcile

```bash
flux reconcile kustomization platform --with-source
# Wait ~30s for the HelmRelease to install Velero
kubectl -n velero get pods -w
```

### 4. Verify

```bash
# Velero server is running
kubectl -n velero get deploy/velero

# Schedules are created
velero schedule get

# Volume policy ConfigMap is present
kubectl -n velero get configmap velero-volume-policy

# Node-agent DaemonSet is running
kubectl -n velero get ds/node-agent

# First backup fires (or trigger one manually to test)
velero backup create test-bootstrap --wait
```

---

## Adding a backup schedule

The three built-in schedules live in `helmrelease.yaml` under `values.schedules`. For anything more targeted, commit a `Schedule` CR to this directory and add it to `kustomization.yaml` — Flux applies it like any other manifest.

```yaml
# platform/velero/schedules-<app>.yaml
---
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
    # Reuse the NFS-skip policy if this schedule snapshots volumes
    resourcePolicy:
      kind: configmap
      name: velero-volume-policy
```

To scope a backup to one app within a shared namespace, add a `labelSelector` (e.g. `app.kubernetes.io/instance: <release-name>` for bjw-s charts). Note that resources without that label — Flux-generated Secrets, manually-created PVCs — won't be captured, so prefer namespace-scoped schedules unless you specifically need to split a namespace.

---

## Excluding resources and volumes

Velero's model is "back up broadly, opt specific things out":

- **Skip a whole resource** — label it `velero.io/exclude-from-backup: "true"`. Works on any object; it's dropped from every backup.
- **Skip volume data by rule** — the `velero-volume-policy` ConfigMap (`volume-policy.yaml`) is a Velero resource policy referenced by snapshotting schedules via `template.resourcePolicy`. It currently skips **all NFS-backed volumes**, which is how the large NAS media libraries (`media-pool`, `photos-pool`) stay out of Velero. To skip more, add conditions (e.g. `capacity: "500Gi,"` or `storageClass: [...]`) with `action: {type: skip}`.
- **Skip a single volume on a pod** — pod annotation `backup.velero.io/backup-volumes-excludes: <volumeName>` (fs-backup path).

Volume data that Velero skips is not backed up by Velero at all — for NFS media that's intentional (the NAS + Kopia own it). Don't rely on Velero for offsite file-level DR of those volumes.

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
# Ad-hoc, via the Velero CLI
velero schedule create home-automation-daily \
  --schedule="0 2 * * *" \
  --include-namespaces home-assistant,zwave-js \
  --ttl 720h0m0s

# Check it was created
velero schedule get home-automation-daily
```

For GitOps-managed schedules, commit a `Schedule` CR to this directory — see [Adding a backup schedule](#adding-a-backup-schedule).

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

# Restore a specific named resource (use daily-critical for PVC *data* —
# weekly-full is manifests-only and carries no volume snapshots)
velero restore create restore-minio-pvc \
  --from-backup daily-critical \
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

> **`weekly-full` restores resource manifests only — not volume data.** It re-creates Deployments, Services, ConfigMaps, Secrets, PVC/PV objects, etc. PVC *data* comes from elsewhere: Longhorn CSI snapshots (only if the cluster disks survived — restore from `daily-critical`), or Kopia's offsite file-level B2 backup (`services/backup/kopia/`) for a true rebuild-from-nothing. NFS media lives on the NAS.

```bash
# 1. Install Velero on the fresh cluster, point it at the same B2 bucket
#    (the bootstrap section above handles this via Flux)

# 2. List available backups
velero backup get

# 3. Restore resource manifests from the weekly full
velero restore create full-restore \
  --from-backup weekly-full

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

### Volume policy not skipping a volume

```bash
# Confirm the ConfigMap exists and has the expected policy
kubectl -n velero get configmap velero-volume-policy -o yaml

# Confirm the schedule references it
kubectl -n velero get schedule daily-critical -o jsonpath='{.spec.template.resourcePolicy}'

# A skipped volume shows as "skipped" in the backup's volume list
velero backup describe <backup-name> --details | grep -iA3 "skipped\|resource policy"
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
#    (B2 config + credentials come from SOPS-encrypted cluster-secrets)
# 4. Velero is deployed, connects to the existing B2 bucket
# 5. Restore resource manifests from the latest weekly-full backup
velero restore create full-restore \
  --from-backup weekly-full

# 6. Restore PVC data. On a from-scratch rebuild the on-cluster Longhorn
#    snapshots are gone, so file data comes from Kopia's offsite B2 backup
#    (services/backup/kopia/). NFS media is already on the NAS.

# 7. Flux reconciles, overwriting with current git state
#    (restore provides the resource baseline; Flux updates config)
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
- **Application key scope:** Use a key scoped to the one bucket, not the master key. If the key is compromised, rotate it in the B2 console and update `VELERO_B2_KEY_ID`/`VELERO_B2_APPLICATION_KEY` in `config/secrets.yaml`.
- **`prune: false`** follows the repo convention — deleting a file from git does NOT delete the resource from the cluster. Clean up removed Velero resources manually with kubectl.
- **Plugin updates:** When Renovate bumps the chart version, verify the `velero-plugin-for-aws` version in `initContainers` is still compatible. CSI support tracks the Velero core version, not a separate plugin.
- **Supplement with Kopia for offsite PVC data:** Velero backs up to B2 (manifests + resource config) and takes on-cluster Longhorn CSI snapshots for app/DB volumes. Kopia backs up file-level PVC data offsite to B2. Together: Velero = cluster resources + on-cluster CSI snapshots, Kopia = offsite file-level PVC data (including the NFS media Velero skips).
- **Schedule overlap is fine.** `daily-cluster` at 01:00 and `daily-critical` at 00:00 may overlap; the broad sweeps are manifests-only (KB-MB) so the cost is negligible and you get more restore granularity.
