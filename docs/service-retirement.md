# Remove Crossplane, Purko, ZeroClaw, Mattermost, and ErsatzTV

## Sequence

Use four separately reconciled stages. Stage 1 prepares deletion; the later
stages remove resources. Do not merge the stages together or advance before
checking the live cluster. Git describes desired state, not everything left
behind by earlier `prune: false` reconciliations.

| Stage | Change | Required evidence before advancing |
| --- | --- | --- |
| 1 (merged) | Give the active targets their own pruning scopes. Keep their resource specs unchanged. Delete the unused ErsatzTV manifest and references. | Owner reports all Kustomizations healthy after merging. Keep the live inventory for cleanup. |
| 2 (this PR) | Empty the Purko/ZeroClaw scopes and remove Mattermost's HelmRelease/HTTPRoute, keeping its namespace/repository. Drain any live Crossplane composites and managed resources operationally, with controllers still running. | App HelmReleases have finished uninstalling; Crossplane managed resources have finished their intended external deletion or explicit handoff. |
| 3 | Remove Crossplane providers and any discovered configuration/function packages and provider configs. | Providers, revisions, their controller workloads, and dependent resources are gone. Crossplane itself remains running until this completes. |
| 4 | Uninstall Crossplane, then remove namespaces, retained CRDs, dedicated storage, credentials, and repository references. | No target workloads, controllers, custom resources, dedicated PVs/Longhorn volumes, or unexplained finalizers remain. |

These are operational stages; a stage may need another PR if the live inventory
reveals resources or dependencies not represented in Git.

## Stage 1: adopt resources into deletion scopes

The shared `core`, `platform`, `services-automation`, and `services-media`
Kustomizations retain `prune: false`. This avoids collecting unrelated resources
that were removed from Git in the past. Their target resources move to:

| Flux Kustomization | Path | Expected inventory |
| --- | --- | --- |
| `core-crossplane` | `core/crossplane` | Namespace `crossplane`, OCIRepository and HelmRelease `crossplane` |
| `platform-crossplane` | `platform/crossplane` | The Backblaze, DigitalOcean, and GitHub Provider objects |
| `core-purko` | `core/purko` | OCIRepository and HelmRelease `operators-system/purko` (release targets `purko-system`) |
| `services-zeroclaw` | `services/automation/zeroclaw` | HelmRelease, PVC, Secret, and NetworkPolicy in `automation` |
| `services-mattermost` (existing) | `services/mattermost` | Namespace, HelmRepository, HelmRelease, HTTPRoute |

Only these dedicated scopes enable `prune: true`. Their
`deletionPolicy: WaitForTermination` requests deletion when the scope is removed
and waits up to `timeout: 10m`. A timeout can still allow the Kustomization to
disappear with resources terminating; always verify actual resources. Prefer
emptying each scope, reconciling, and verifying deletion **before** removing its
bootstrap file and directory. `dependsOn` orders application, not teardown.

No active resource identity or workload spec changes in this stage. The old
owners already have pruning disabled, which allows the ownership transfer.
Verify that setting in-cluster before merging. Reconcile and check both sides
after merging:

```sh
# Before merging: confirm spec.prune is false on all four source layers.
flux export kustomization core
flux export kustomization platform
flux export kustomization services-automation
flux export kustomization services-media

# After merging: pick up bootstrap definitions and reconcile the source layers.
flux reconcile kustomization flux-system --with-source
flux reconcile kustomization core
flux reconcile kustomization platform
flux reconcile kustomization services-automation

flux reconcile kustomization core-crossplane
flux reconcile kustomization platform-crossplane
flux reconcile kustomization core-purko
flux reconcile kustomization services-zeroclaw
flux reconcile kustomization services-mattermost

flux get kustomizations
flux tree kustomization core-crossplane
flux tree kustomization platform-crossplane
flux tree kustomization core-purko
flux tree kustomization services-zeroclaw
flux tree kustomization services-mattermost
flux tree kustomization core
flux tree kustomization platform
flux tree kustomization services-automation
```

Check each scope's `.status.lastAppliedRevision` matches the preparation commit
or a descendant and that `.status.inventory` matches the table. Confirm live
objects have the new `kustomize.toolkit.fluxcd.io/name` ownership label. The
previous layers must no longer inventory those objects. Do not proceed on a
failed reconciliation or an ownership mismatch.

### Capture the live inventory before deleting anything

```sh
kubectl get helmreleases -A
helm list -A --all
kubectl get pvc -A -o wide
kubectl get pv -o yaml
kubectl get volumes.longhorn.io -n longhorn-system
kubectl get crd -o yaml
kubectl get providers.pkg.crossplane.io
kubectl get providerrevisions.pkg.crossplane.io
kubectl get configurations.pkg.crossplane.io
kubectl get functions.pkg.crossplane.io
kubectl get compositeresourcedefinitions.apiextensions.crossplane.io
kubectl get managed -A -o yaml
kubectl get composite -A -o yaml
kubectl get claim -A -o yaml
```

Discovery categories may be absent when no corresponding types are installed.
Distinguish that from an authorization/API failure. Save exact names, scopes,
external identifiers, ownership, `deletionPolicy`, and `managementPolicies` for
all Crossplane resources, including resources installed manually. Inventory
provider configs, compositions/revisions, package revisions, runtime configs,
usages, connection secrets, and any other installed Crossplane APIs too. Git
currently defines three Providers but no managed resources or provider configs;
this does not establish that the live cluster has none.

Inspect installed Purko CRDs and their instances, Helm hooks, and cluster-scoped
RBAC/webhooks from the actual release. Do not infer an empty cluster from its
disabled starter agents/presets/trigger rules in Git.

Record each dedicated PVC's bound PV, reclaim policy, CSI volume handle, and
Longhorn volume before removing the claim. Also record Mattermost's PostgreSQL
database/role and external OIDC clients. Keep inventory exports containing
credentials out of Git.

## Stage 2: remove apps and drain Crossplane-managed resources

### Changes applied by this PR

`core/purko` and `services/automation/zeroclaw` now render zero resources. Their
existing Flux Kustomizations remain present with pruning enabled, so the next
reconciliation collects their previous inventories. Mattermost now renders only
its namespace and HelmRepository; its HelmRelease and HTTPRoute are collected.
The Crossplane controller and all three Providers are unchanged. No Crossplane
managed resources are declared in Git, so any live managed-resource teardown
must be done against the identified cluster objects before stage 3.

Before merging, capture `automation/zeroclaw-data`'s bound PV and CSI volume
handle using the inventory commands above: this PR deletes that PVC. Check for
Purko custom resources requiring its operator for finalization and remove those
first. Healthy Flux Kustomizations alone do not establish that such resources
are absent. Keep the operator installed until those deletions finish.

After merging, reconcile the scopes and check the actual uninstall results:

```sh
flux reconcile kustomization flux-system --with-source
flux reconcile kustomization core-purko
flux reconcile kustomization services-zeroclaw
flux reconcile kustomization services-mattermost

# These HelmReleases must disappear, not remain Terminating.
kubectl get helmrelease purko -n operators-system --ignore-not-found
kubectl get helmrelease zeroclaw -n automation --ignore-not-found
kubectl get helmrelease mattermost -n mattermost --ignore-not-found
helm list -A --all

# These dedicated resources must also disappear.
kubectl get pvc zeroclaw-data -n automation --ignore-not-found
kubectl get secret zeroclaw-oidc-credentials -n automation --ignore-not-found
kubectl get networkpolicy zeroclaw -n automation --ignore-not-found
kubectl get httproute mattermost -n mattermost --ignore-not-found

# Purko and ZeroClaw inventories should be empty. Mattermost should retain
# only its Namespace and HelmRepository until a later cleanup reconciliation.
flux tree kustomization core-purko
flux tree kustomization services-zeroclaw
flux tree kustomization services-mattermost

# Keep these installed. Inspect actual managed resources before stage 3.
kubectl get providers.pkg.crossplane.io
kubectl get managed -A -o yaml
kubectl get composite -A -o yaml
kubectl get claim -A -o yaml
```

Confirm the scopes applied the step-2 commit (or a descendant), then check for
remaining app pods, services, routes, and finalizers. A healthy Kustomization or
empty inventory is not sufficient proof that Helm finished uninstalling.
Mattermost's chart-retained PVCs, database/role, namespace, and source remain
for cleanup after the initial uninstall. ZeroClaw's retained PV/Longhorn data
also still needs the stage-4 storage cleanup. No live cluster operations are
performed by opening this PR.

### Operational order and remaining cleanup

- Remove Purko custom resources while its operator can still process their
  finalizers; then empty `core/purko` and reconcile `core-purko`.
- Empty `services/automation/zeroclaw` and reconcile `services-zeroclaw`. This
  removes its HelmRelease, `zeroclaw-data` PVC, OIDC Secret, and NetworkPolicy.
  Preserve the shared `automation` namespace and `app-template` source.
- Remove the Mattermost HelmRelease and HTTPRoute first, retaining its namespace
  and HelmRepository until uninstall is confirmed. Then remove dedicated PVCs
  retained by Helm, followed by the repository and namespace. Do not delete the
  namespace concurrently with the initial uninstall.
- ErsatzTV is complete in stage 1: the owner confirmed nothing exists in the
  cluster, so its excluded manifest and references are deleted directly. Keep
  `media-pool`, the `media` namespace, and the shared chart source.
- For Crossplane, inspect composite ownership and managed-resource policies
  before deleting composites/XRDs and remaining managed resources in dependency
  order. Keep Crossplane, providers, credentials, and provider configs available
  until external cleanup completes. `Orphan` or management policies without
  `Delete` can leave external infrastructure behind. Resolve those deliberately
  for the identified resources; do not globally overwrite policies. Confirm
  deletion externally, or document an explicit handoff for shared infrastructure
  that must survive. In particular, do not delete GitHub repositories or backup
  buckets just because a provider can manage them.

Use empty `resources: []` Kustomization files to retire scoped objects while
retaining the Flux scopes. Check actual HelmRelease deletion, Helm release
state, pods, routes, services, and finalizers after reconciliation. Do not remove
finalizers to make a blocked uninstall appear successful.

## Stage 3: remove Crossplane packages

After all composite/managed resources are drained, remove any discovered
Configuration packages that could recreate providers/functions. Empty
`platform/crossplane`, reconcile `platform-crossplane`, and confirm the three
Providers and their ProviderRevisions/controller deployments finish deletion.
Remove any additional installed providers, functions/revisions, provider configs,
runtime configs, compositions/revisions, and connection secrets identified in
the live inventory, in dependency order. Keep the Crossplane controller running
throughout this stage. Emptying Git alone cannot remove manually created items.

## Stage 4: uninstall Crossplane and finish cleanup

1. Remove just the Crossplane HelmRelease from `core/crossplane/release.yaml`.
   Reconcile `core-crossplane` and wait for its uninstall to finish. Then remove
   its OCIRepository and namespace, reconcile again, and verify deletion.
2. Remove the empty retirement scopes/directories and the Mattermost bootstrap
   definition only after their inventories and actual resources are empty.
3. Inspect retained Purko and Crossplane/provider CRDs, including provider API
   groups that do not end in `crossplane.io`. Verify each type has no remaining
   instances and delete only the exact CRDs belonging to the retired services.
   Check their cluster RBAC, webhooks, namespaces, and Helm hook leftovers too.
   Do not delete the shared `operators-system` namespace.
4. Finish dedicated storage cleanup from the recorded volume handles. The
   Longhorn chart sets `reclaimPolicy: Retain`: deleting a PVC does **not** prove
   its data or underlying volume was deleted. For each verified dedicated PV,
   use its recorded reclaim policy/CSI handle to complete backend deletion and
   confirm the PV and Longhorn volume are gone. Check dedicated snapshots and
   backup copies separately. Never delete `media-pool` or unrelated volumes.
5. Remove the Mattermost database and role from the shared PostgreSQL server
   after the app is stopped and connections are gone; these are managed
   manually, not by Flux. Remove dedicated OIDC clients/credentials and unused
   SOPS keys (including `MATTERMOST_DB_PASSWORD` and ZeroClaw OIDC variables),
   verifying they have no other consumers.
6. Remove Crossplane's Renovate package matcher/digest exception, its Velero
   namespace exclusion, and update README/CLAUDE architecture descriptions.
   Keep Velero and its Backblaze integration: they are separate services.
7. Compare the live cluster against the captured inventory, including external
   resources, rather than relying on `kubectl get all` (which omits CRs, PVCs,
   and other resource types). Account for every remaining object or stored copy.

## References

- [Flux resource ownership transfer](https://fluxcd.io/flux/faq/#how-can-i-safely-move-resources-from-one-dir-to-another)
- [Flux pruning and deletion policy](https://fluxcd.io/flux/components/kustomize/kustomizations/#prune)
- [Crossplane 2.4 uninstall order and retained CRDs](https://docs.crossplane.io/v2.4/guides/uninstall-crossplane/)
