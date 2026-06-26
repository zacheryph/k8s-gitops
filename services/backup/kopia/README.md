# k8s PVC Backup with Kopia

Multi-tier (NAS + S3) scheduled backup of cluster PVCs, with quarterly restore drills and Slack notification. One CronJob per PVC in the `backup` namespace.

This implementation is **Option B (Kopia)** from the design doc at `.hermes/plans/2026-06-11_105417-k8s-pvc-backup.md`. Workloads are NOT backed up here — Flux + the GitOps repo is the source of truth for those.

---

## Table of contents

- [Architecture](#architecture)
- [What's deployed](#whats-deployed)
- [One-time bootstrap (per PVC)](#one-time-bootstrap-per-pvc)
- [Adding a new PVC to the backup set](#adding-a-new-pvc-to-the-backup-set)
- [Daily backup (automated)](#daily-backup-automated)
- [Manual backup](#manual-backup)
- [Restore](#restore)
- [Quarterly restore drill](#quarterly-restore-drill)
- [Troubleshooting](#troubleshooting)
- [Recovery scenarios](#recovery-scenarios)
- [Operational notes](#operational-notes)

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Cluster                                                            │
│                                                                     │
│  CronJob (daily 02:00)        CronJob (Jan/Apr/Jul/Oct, 04:00)      │
│  ┌──────────────────┐         ┌──────────────────┐                  │
│  │ kopia-backup-    │         │ kopia-restore-   │                  │
│  │  <pvc>           │         │  drill-<pvc>     │                  │
│  │                  │         │  (alt: nas|b2)   │                  │
│  │  /data ◄── PVC   │         │  /restore ◄──    │                  │
│  │   (read-only)    │         │   drill PVC      │                  │
│  └────┬─────────────┘         └────┬─────────────┘                  │
│       │                            │                                │
└───────┼────────────────────────────┼────────────────────────────────┘
        │ SFTP (LAN)                 │
        ▼                            ▼
  ┌──────────────────┐         ┌──────────────────┐
  │ Synology NAS     │         │ Backblaze B2     │
  │ /volume1/        │         │ k8s-backups/     │
  │  k8s-backups/    │ ◄─sync─ │  <pvc>/          │
  │   <pvc>/         │  to     │                  │
  │  (tier 1)        │         │ (tier 2)         │
  └──────────────────┘         └──────────────────┘
```

**Tier 1 — Synology NAS (local, fast).** Primary repo via SFTP. Daily snapshot is written here first. Restore from this tier is fast (LAN).

**Tier 2 — Backblaze B2 (offsite, DR).** Mirror repo, kept in sync via `kopia repository sync-to` after each backup. Restore from this tier exercises the DR path: WAN bandwidth applies, but data is intact even if the NAS dies.

**Encryption.** Both repos are encrypted client-side by Kopia (AES-256-GCM, key derived from `KOPIA_PASSWORD`). The password is the actual key — losing it = losing the data, even with valid bucket credentials. The bucket/SFTP transport is authenticated separately.

**Multi-tier in one command.** Kopia's `repository sync-to` pushes the entire primary repo (every snapshot, every blob) to the DR repo. The DR repo's password is set to a derived value (`--sync-password`) so a stolen B2 key still can't decrypt the data.

**Retention.** 7 daily / 4 weekly / 12 monthly snapshots, applied as a Kopia global policy. Edit the JSON in `_base/kopia-policy.yaml` to change.

---

## What's deployed

After this PR merges + Flux reconciles, the cluster has:

| Resource | Namespace | Purpose |
|---|---|---|
| `Namespace/backup` | — | Backing namespace |
| `ServiceAccount/kopia-backup` | backup | No API access. Used by backup CronJob + init Job. |
| `ServiceAccount/kopia-drill` | backup | Can create/delete drill PVCs in `backup`. |
| `Role/kopia-drill` + `RoleBinding/kopia-drill` | backup | RBAC for the drill SA |
| `ConfigMap/kopia-scripts` | backup | The `backup.sh`, `init.sh`, `restore.sh`, `common.sh` scripts |
| `ConfigMap/kopia-policy` | backup | Global retention/compression/encryption policy JSON |
| `Secret/kopia-immich-photos-pool` | backup | Env-style creds (KOPIA_PASSWORD, B2 keys, NAS SFTP) |
| `Secret/kopia-immich-photos-pool-ssh` | backup | SSH private key + known_hosts for the NAS |
| `CronJob/kopia-backup-immich-photos-pool` | backup | Daily 02:00 backup |
| `CronJob/kopia-restore-drill-immich-photos-pool` | backup | Quarterly drill (alt NAS / B2) |
| `Job/kopia-init-immich-photos-pool` | backup | One-shot — create both repos |
| `Kustomization/services-backup` | flux-system | Flux pointer to this directory |

---

## One-time bootstrap (per PVC)

You only need to do this once per PVC. The GitOps repo ships the structure; you provide the secrets and run the init Job.

### 1. Create the NAS SFTP user

On the Synology DSM:

1. **Control Panel → User & Group → Create** → user `backup` (or whatever you set in `KOPIA_NAS_SFTP_USER`). Disable everything except SFTP access.
2. **Control Panel → File Services → FTP** → enable SFTP (port 22 is fine).
3. Create the destination directory, e.g. `/volume1/k8s-backups/immich-photos-pool`, and grant `backup` read/write.
4. As `backup` (or root), generate a key:

   ```bash
   ssh-keygen -t ed25519 -N "" -f /tmp/kopia-nas
   # Authorize the public key for user 'backup' in DSM
   # (Control Panel → User → backup → Edit → Authorised keys)
   ```

5. Capture the host key for `known_hosts`:

   ```bash
   ssh-keyscan -t ed25519 <NAS_ADDRESS> > /tmp/known_hosts
   ```

### 2. Create the B2 bucket + application key

1. Backblaze B2 → **Buckets → Create a Bucket** named `k8s-backups` (private, default encryption on).
2. **App Keys → Add a New Application Key**:
   - Name: `kopia-k8s-backups`
   - Bucket: `k8s-backups` only (single-bucket scope)
   - Capabilities: `listFiles`, `readFiles`, `writeFiles`, `deleteFiles`. **Do not** grant `listAllBucketNames` or any account-wide perms.
   - **Note the `keyID` and `applicationKey`.**

3. The Kopia S3 endpoint depends on the bucket's region. For `us-west-002` it's `s3.us-west-002.backblazeb2.com`. Find yours in the B2 dashboard (Bucket → Endpoint).

### 3. Generate the Kopia repo password

```bash
head -c 32 /dev/urandom | base64
```

Store this in Bitwarden / 1Password / printed-and-locked-in-safe. **If you lose this, the backups are unrecoverable.** This is non-negotiable.

### 4. Create the encrypted Secret

Copy `secrets.enc.yaml.example` to `secrets.enc.yaml` and fill in the real values. Then encrypt in place:

```bash
cd services/backup/kopia
cp secrets.enc.yaml.example secrets.enc.yaml
$EDITOR secrets.enc.yaml       # fill in the real values
sops --encrypt --in-place secrets.enc.yaml
git add secrets.enc.yaml
git commit -m "feat(backup): add kopia secrets for <pvc>"
```

### 5. Run the init Job

```bash
# Trigger Flux to reconcile first (or wait ~10 min for the next interval)
flux reconcile kustomization services-backup

# Then run the init Job. This creates both repos (NAS via create, B2 via sync-to).
kubectl -n backup create job --from=cronjob/kopia-init-immich-photos-pool manual-init-immich-photos-pool
# Or, if Flux hasn't created the CronJob yet, the Job in jobs/ should be present:
kubectl -n backup create job --from=job/kopia-init-immich-photos-pool manual-init-immich-photos-pool

# Watch the logs:
kubectl -n backup logs -f job/manual-init-immich-photos-pool
```

The init Job:
- Calls `kopia repository create sftp://...` to initialise the NAS repo.
- Connects to it, applies the global policy.
- Calls `kopia repository sync-to s3://...` to bootstrap the B2 repo from the NAS repo's contents (empty at this point, but the bucket/prefix/keys are wired up).

### 6. Confirm

```bash
# A throwaway debug pod to list snapshots
kubectl -n backup run kopia-debug --rm -it --restart=Never \
  --image=kopia/kopia:0.18 \
  --env-from secret/kopia-immich-photos-pool \
  --env-from secret/kopia-immich-photos-pool-ssh \
  --overrides='{ "spec": { "automountServiceAccountToken": false, "volumes": [{"name":"ssh","secret":{"secretName":"kopia-immich-photos-pool-ssh"}},{"name":"cfg","emptyDir":{}}], "containers": [{"name":"kopia-debug","image":"kopia/kopia:0.18","command":["sh","-c","kopia --config-file=/cfg/repository.config --cache-directory=/cfg --log-level=info --password=\"$KOPIA_PASSWORD\" repository connect sftp --host=\"$KOPIA_NAS_SFTP_HOST\" --port=\"${KOPIA_NAS_SFTP_PORT:-22}\" --username=\"$KOPIA_NAS_SFTP_USER\" --path=\"$KOPIA_NAS_SFTP_PATH\" --keyfile=/ssh/id_ed25519 --known-hosts=/ssh/known_hosts && kopia snapshot list"], "volumeMounts":[{"name":"ssh","mountPath":"/ssh","readOnly":true},{"name":"cfg","mountPath":"/cfg"}]}]}}'
```

You should see an empty list (no snapshots yet — they're made by the daily CronJob).

### 7. Clean up the init Job

The init Job is intentionally NOT auto-pruned (we don't want Flux to recreate it once you delete it after success). To remove it:

```bash
kubectl -n backup delete job kopia-init-immich-photos-pool
```

---

## Adding a new PVC to the backup set

You need three things: a CronJob, a Secret, an init Job. The pattern is:

1. **Copy the example file:**

   ```bash
   cd services/backup/kopia
   cp cronjobs/pvc-immich-photos-pool.yaml cronjobs/pvc-<namespace>-<name>.yaml
   cp jobs/kopia-init-immich-photos-pool.yaml jobs/kopia-init-<namespace>-<name>.yaml
   cp drill/kopia-restore-drill-immich-photos-pool.yaml drill/kopia-restore-drill-<namespace>-<name>.yaml
   cp secrets.enc.yaml.example secrets.enc.yaml   # edit, then sops --encrypt --in-place
   ```

2. **In each new file**, find/replace:
   - `immich-photos-pool` → `<namespace>-<name>` (slug)
   - `photos-pool` → the actual PVC `metadata.name` in the source namespace
   - `immich` → the source namespace
   - `kopia-immich-photos-pool` → `kopia-<slug>` (the Secret name)
   - All `secretKeyRef.name: kopia-immich-photos-pool` references
   - In the drill: `claimName: drill-restore-immich-photos-pool` → `drill-restore-<slug>`, and the `kubectl apply` PVC name
   - Drill `RESTORE_FROM` default — leave `nas` for now, flip per quarter

3. **Append the new file paths** to `kustomization.yaml` `resources:`.

4. **Commit + push.** Flux reconciles in ≤10 min. Then run the init Job (steps 5–7 above) for the new PVC.

---

## Daily backup (automated)

`CronJob/kopia-backup-immich-photos-pool` runs every day at 02:00 cluster-local. Each run:

1. `repository connect sftp://...` — opens the NAS repo.
2. `policy set --global <policy.json>` — applies retention / compression.
3. `snapshot create /data --all` — snapshot the source PVC. Incremental after the first run.
4. `maintenance run --full` — drop expired snapshots, verify index integrity.
5. `repository sync-to s3://...` — push the entire repo to B2.
6. `repository disconnect`.

Logs land in `/var/kopia/log` (an emptyDir) — the pod log shows the same.

To watch a live run:

```bash
kubectl -n backup logs -f -l job-name=kopia-backup-immich-photos-pool-<id>
# Or list all backup jobs:
kubectl -n backup get jobs -l app.kubernetes.io/name=kopia
```

If a run fails, the CronJob retries (backoffLimit: 2). Inspect the failed pod:

```bash
kubectl -n backup describe job -l app.kubernetes.io/name=kopia
kubectl -n backup logs <failed-pod>
```

---

## Manual backup

If you need to force a backup *right now* (e.g. before a risky operation), trigger one ad-hoc:

```bash
# Use the CronJob as a template
kubectl -n backup create job --from=cronjob/kopia-backup-immich-photos-pool \
  manual-backup-$(date +%Y%m%d-%H%M%S)

# Watch it
kubectl -n backup logs -f -l job-name=manual-backup-*
```

The CronJob's schedule is `0 2 * * *` with `concurrencyPolicy: Forbid`, so a manual run will NOT block tomorrow's scheduled run.

---

## Restore

### Restore to a throwaway PVC (the safe path)

This is what the drill does. Same recipe on demand:

```bash
# 1. Spin up a debug pod with the same env/creds as the drill.
# 2. Use the restore.sh script.

kubectl -n backup run restore-debug --rm -it --restart=Never \
  --image=kopia/kopia:0.18 \
  --env-from secret/kopia-immich-photos-pool \
  --env-from secret/kopia-immich-photos-pool-ssh \
  --env RESTORE_FROM=nas \
  --env RESTORE_TARGET=/restore \
  --overrides='{ ... volumeMounts / volumes ... }'
```

### Restore into the LIVE PVC (DANGEROUS — read this first)

Restoring into the live PVC means the running application will see the restored state mid-flight. This is only safe when:

- The application is **stopped** (Deployment scaled to 0, or StatefulSet pods deleted).
- No other process is writing to the PVC.

**Procedure:**

1. Scale the app to 0 replicas (or stop the StatefulSet).
2. Create an empty PVC matching the original `spec.resources.requests.storage` (or reuse the existing one — but Kopia restore overwrites, not merges).
3. Run `restore.sh` with `RESTORE_TARGET` set to the PVC's mount path. Kopia restore is non-destructive in the sense that it creates files, but if the live data has files not in the snapshot, they'll remain.
4. **Safer approach:** restore to a new PVC, swap the PV, or use a copy strategy.

For most cases, the drill mechanism (throwaway PVC, no live app) is the right tool. The "restore into live" recipe is intentionally not pre-baked — every situation is different.

---

## Quarterly restore drill

`CronJob/kopia-restore-drill-immich-photos-pool` runs at 04:00 on the 1st of January, April, July, October.

What it does:

1. `initContainer/provision-restore-pvc` creates a 100 Gi PVC (`drill-restore-immich-photos-pool`) in the `backup` namespace, using the `longhorn-nfs` storage class (override with `STORAGE_CLASS` env if needed).
2. The `kopia-restore` container runs `restore.sh` with `RESTORE_FROM=nas`.
3. Restore output includes a file count and total size, which lands in the pod log.
4. The Job auto-deletes after 24h via `ttlSecondsAfterFinished`. The PVC is also auto-deleted because the Job's pod was the last user.

### Drill path rotation

| Quarter | RESTORE_FROM | Exercises |
|---|---|---|
| Q1 (Jan) | `nas` | Fast local restore — the happy path |
| Q2 (Apr) | `b2` | DR restore — WAN bandwidth, S3 throttling, etc. |
| Q3 (Jul) | `nas` | Fast local restore |
| Q4 (Oct) | `b2` | DR restore |

**To rotate:** edit `drill/kopia-restore-drill-immich-photos-pool.yaml`, change `RESTORE_FROM`, commit, push. No need to delete and recreate the CronJob.

### Inspecting a drill result

```bash
kubectl -n backup get jobs -l backup.zacheryph/role=drill
kubectl -n backup logs -l backup.zacheryph/role=drill --tail=200
```

The pod log includes:
- Repo connect confirmation
- Resolved snapshot ID (the "latest")
- Total size + file count after restore

### (Future) Slack notification

The drill does not post to Slack yet — see [Operational notes](#operational-notes). The current workflow is: review logs quarterly, or grep for `kopia-restore-drill` failures via Prometheus when that alert exists.

---

## Troubleshooting

### `kopia repository connect`: host key verification failed

Either the NAS IP changed, or the SSH host key is rotated. Refresh `known_hosts`:

```bash
ssh-keyscan -t ed25519 <NAS_ADDRESS> > new_known_hosts
# Update the Secret (re-encrypt after edit):
sops secrets.enc.yaml
```

### `kopia repository connect`: authentication failed (publickey)

- The SSH key in the Secret doesn't match what's authorised on the NAS.
- The `KOPIA_NAS_SFTP_USER` doesn't exist or has no SFTP permission in DSM.
- DSM's SFTP service isn't running. **Control Panel → File Services → FTP** → enable SFTP.

### `kopia snapshot create` is slow / OOMKilled

- Bump `resources.limits.memory` in the CronJob.
- Bump the cache `emptyDir.sizeLimit` — large PVCs need more dedup metadata cache.
- For very large PVCs (>5 TB), consider chunked backups or running on a dedicated node with an `nodeSelector`.

### `repository sync-to` is slow / times out

- B2 throttling at the application-key level. Each B2 key has a default 100 req/s cap. Adjust the key's throttle or run syncs less frequently.
- WAN bandwidth. The first sync after init is the entire repo (large). Subsequent syncs are incremental.

### A CronJob stopped running

```bash
kubectl -n backup get cronjob
kubectl -n backup get jobs -l app.kubernetes.io/name=kopia
kubectl -n backup describe cronjob kopia-backup-immich-photos-pool
```

Likely causes: CronJob is `Suspended`, or Kustomization `services-backup` is failing. Check `flux get kustomization services-backup`.

### A backup ran but produced no snapshot

Check the source PVC was actually mounted and non-empty:

```bash
kubectl -n backup exec <pod> -- ls -la /data
```

If empty, the source PVC might be detached from its workload. Check the workload's `kubectl get pvc` and pod status.

### Restore hangs forever

- Kopia restore is single-threaded by default for small files. For large restores, the `--parallel` flag on the snapshot restore command helps — but it can also hammer the source repo. Try `--parallel=4` as a starting point.
- Check the Job's `activeDeadlineSeconds`. The drill's is 6h.

---

## Recovery scenarios

### NAS dies, B2 is intact

1. Provision a new NAS (or any SFTP host).
2. Re-create the NAS SFTP user + key.
3. Update the Secret with the new `KOPIA_NAS_SFTP_HOST` and SSH key.
4. Run the init Job (it'll fail to "create" the NAS repo since it exists, but that's fine — the B2 repo sync will continue).
5. Daily backups resume. Use `RESTORE_FROM=b2` for immediate restores until the new NAS has data.

### B2 credentials leaked, or bucket is wiped

1. Rotate the B2 application key. Create a new one.
2. Update the Secret.
3. The next backup run will sync to the new B2 location (or fail if you also wiped the bucket — in which case run the init Job to recreate it; Kopia will rebuild the B2 repo from the NAS contents).

### Both tiers lost

If you have a third copy (offline / Glacier / another cloud), great. If not, the data is gone. This is why retention is 12 months and the schedule is daily — there's a wide window to notice a problem.

### Lost KOPIA_PASSWORD

The data is unrecoverable. The Kopia encryption key is derived from the password; the bucket / SFTP transport only guards against unauthorised writes, not decryption. **Store the password in Bitwarden AND a printed copy in a safe.**

### Cluster lost (worst case)

1. Rebuild the cluster (Flux will restore workloads from this repo).
2. Re-run the bootstrap steps in [One-time bootstrap](#one-time-bootstrap-per-pvc). Flux re-creates the namespace, ConfigMaps, ServiceAccounts.
3. Restore the Secrets from Bitwarden / the GitOps repo's history.
4. Re-create the SSH key on the NAS (or re-add the existing one).
5. Re-create the B2 application key.
6. Verify: run the drill manually. The B2 repo is intact, so `RESTORE_FROM=b2` should restore cleanly.

---

## Operational notes

### Out of scope (Phase 1)

- **Prometheus alerts** for "no new snapshot in 26h" or "drill failed". Add a `PrometheusRule` to the drill/ folder.
- **Slack notification** from the drill. Add a curl step in the restore container, using a webhook URL in a Secret.
- **Compression tuning** per-PVC. Default `zstd-fast` is a safe starting point; revisit after a few real runs.
- **TLS host-key verification** beyond plain `known_hosts`. (The current design uses `known_hosts` — first-time MITM risk is bounded by the homelab network.)
- **B2 lifecycle rules** for the bucket itself. The Kopia policy handles retention at the snapshot level; B2 lifecycle can be added for cheaper long-tail storage.

### Phase 2 candidates

- Add per-PVC overlays instead of one file per PVC (Helm/Kustomize generator).
- A simple Web UI (Kopia server) on the NAS for ad-hoc browsing.
- A Slack notifier sidecar for both backup and drill outcomes.

### See also

- Design doc: `.hermes/plans/2026-06-11_105417-k8s-pvc-backup.md`
- Implementation plan: `.hermes/plans/2026-06-15_221500-kopia-pvc-backup-impl.md`
- Kopia docs: <https://kopia.io/docs/>
