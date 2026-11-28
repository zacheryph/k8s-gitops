# Service retirement: final cleanup

Crossplane, Purko, ZeroClaw, Mattermost, and ErsatzTV have been removed from the
active workload manifests in earlier revisions. This revision completes the
GitOps namespace/scope cleanup. Live resource deletion and retained data still
require the checks below; this document does not claim they are already gone.

## What this revision does

- Removes the five temporary Flux Kustomizations and their build directories.
  The root `flux-system` Kustomization has `prune: true`, so it deletes those
  scopes. Their existing live `deletionPolicy: WaitForTermination` and saved
  inventories collect Crossplane's remaining Namespace and OCIRepository.
  The other four scopes should already have empty inventories.
- Runs `flux-system/retire-purko-namespace`, a one-time Job that explicitly
  deletes `purko-system` and waits up to ten minutes for it to disappear.
  Purko's namespace was not in a Flux inventory, so deleting its old empty
  scope alone would not remove the namespace.
- Removes `crossplane` from Velero's `daily-infra` included namespaces. Daily
  and weekly schedules use wildcards and need no new namespace exclusions.
  Velero, its Backblaze credentials, schedules, and shared backups remain.

The cleanup Job can only get/delete the exact Namespace `purko-system` and read
`operators-system/purko` HelmRelease. It refuses to delete the namespace while
that HelmRelease exists. It treats an already-absent namespace as success and
fails on authorization/API errors or a deletion timeout. It never removes
finalizers or deletes CRDs, other namespaces, PVs, or external resources.

The completed Job and its narrowly scoped RBAC remain as a receipt, without a
TTL. This prevents a recreate/delete loop on each Flux reconciliation. Renovate
ignores this one-time manifest because Job pod templates are immutable.
After successful verification, the single manifest and its Renovate ignore can
be removed during ordinary cleanup; the root scope will prune all six objects.
No further adoption/reconciliation stage is needed to delete Purko's namespace.

## Before merging

The previous [Crossplane uninstall PR](https://github.com/zacheryph/k8s-gitops/pull/3762)
is merged. Confirm uninstall actually finished before removing its namespace:

```sh
kubectl get helmrelease crossplane -n crossplane --ignore-not-found
kubectl get deployment,pod -n crossplane
helm list -A --all
flux tree kustomization core-crossplane
flux tree kustomization platform-crossplane
flux tree kustomization core-purko
flux tree kustomization services-zeroclaw
flux tree kustomization services-mattermost
```

Crossplane must have no remaining HelmRelease, installed Helm release, controller
workloads, or package/managed-resource finalizers. Its Flux inventory should
contain only its Namespace and OCIRepository. All other retirement scopes must
have empty inventories and their uninstalls must have finished.

Confirm Purko's app is gone, inspect any namespace leftovers, and record any
remaining PVC/PV bindings and CSI volume handles before the namespace is deleted:

```sh
kubectl get helmrelease purko -n operators-system --ignore-not-found
kubectl get deployment,statefulset,pod -n purko-system
kubectl get pvc -n purko-system -o yaml
kubectl get pv -o yaml
kubectl get crd -o yaml
```

Use the earlier captured inventory for claims already removed. Keep exports
containing credentials out of Git. The Job's namespace deletion includes all
remaining namespaced resources, including PVCs; Longhorn's `Retain` policy can
still leave their PVs and backing data behind.

## After merging

```sh
# Remove the old Flux scopes and start the explicit Purko namespace cleanup.
flux reconcile kustomization flux-system --with-source
kubectl wait -n flux-system --for=condition=complete job/retire-purko-namespace --timeout=12m
kubectl logs -n flux-system job/retire-purko-namespace

# All three dedicated namespaces must be absent, not Terminating.
kubectl get namespace crossplane purko-system mattermost --ignore-not-found

# These five Flux scopes must also be absent, not stuck finalizing.
kubectl get kustomization -n flux-system core-crossplane platform-crossplane core-purko services-zeroclaw services-mattermost --ignore-not-found

# Apply the backup change and inspect actual schedules.
flux reconcile kustomization platform
flux reconcile helmrelease velero -n velero
kubectl get schedules.velero.io -n velero -o yaml
```

The Purko Job logs must say the namespace is already absent or deletion completed.
If it fails, inspect its logs, the Namespace, and the relevant custom resources
and finalizers. Resolve the underlying cleanup; do not force-finalize the
namespace or strip finalizers to hide stuck resources.

Check the root and platform scopes applied this revision (or a descendant).
`daily-infra` must no longer include `crossplane`, and all other schedule values
must match the previous configuration. Existing shared backups retain their
configured lifetimes; removing a namespace from a schedule does not erase old
backup copies. Do not delete shared archives just to remove one retired app.

`WaitForTermination` has a timeout: a removed Flux scope can disappear while
its old resources still terminate. A healthy Flux status or missing scope alone
is not proof that namespace/resource deletion finished. Verify the actual
Namespaces, OCIRepository, Job result, and captured inventory.

## Retained resources and data

| Item | Required cleanup/verification |
| --- | --- |
| Crossplane/Purko CRDs and custom resources | Compare the captured CRD inventory, including provider groups that do not end in `crossplane.io`. Verify no instances or finalizers remain before deleting exact retired CRDs. |
| Cluster RBAC, webhooks, Helm hooks | Remove only confirmed retired-service objects. Namespace deletion does not remove unrelated cluster-scoped resources. |
| Longhorn/PVs | Match recorded CSI handles to the retired claims, including `zeroclaw-data` and Mattermost/Purko claims. Finish backend deletion and verify both PV and Longhorn volume are gone. Check dedicated snapshots/backups separately. |
| Mattermost PostgreSQL | Remove its database and role from the shared PostgreSQL server after connections are gone. These are manually managed outside Flux. |
| OIDC and SOPS | Remove dedicated clients and unused keys, including Mattermost's DB password and ZeroClaw OIDC variables, after checking other consumers. Edit SOPS with the decryption key so its integrity metadata is updated. |
| DNS and external resources | Check exact retired hostnames and any prior Crossplane external identifiers; confirm deletion or an explicit handoff for shared infrastructure. |

Preserve `operators-system`, `automation`, and `media` namespaces, the shared
`app-template` source, `media-pool`, Velero's Backblaze bucket/credentials, and
all unrelated workloads and storage. Do not use blanket CRD/PV or backup deletion.

The PR does not execute live cluster/database commands or claim unknown retained
objects have been deleted. Reconcile and compare the actual cluster against the
captured inventory, rather than relying on `kubectl get all`, which omits CRDs,
PVCs, and other resource types.

## Earlier steps

| PR | Result |
| --- | --- |
| [3756](https://github.com/zacheryph/k8s-gitops/pull/3756) | Created dedicated pruning scopes; removed unused ErsatzTV files. |
| [3758](https://github.com/zacheryph/k8s-gitops/pull/3758) | Removed Purko, ZeroClaw, and Mattermost workloads. |
| [3759](https://github.com/zacheryph/k8s-gitops/pull/3759) | Removed the three declared Crossplane Providers. |
| [3762](https://github.com/zacheryph/k8s-gitops/pull/3762) | Uninstalled Crossplane and removed Mattermost's namespace/source. |

## References

- [Flux pruning and deletion policies](https://fluxcd.io/flux/components/kustomize/kustomizations/#prune)
- [Crossplane uninstall order and retained CRDs](https://docs.crossplane.io/v2.4/guides/uninstall-crossplane/)
