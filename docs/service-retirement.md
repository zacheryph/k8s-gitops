# Remove Crossplane, Purko, ZeroClaw, Mattermost, and ErsatzTV

## Sequence

Use four stages with separately reconciled changes. Stage 1 prepares deletion;
the later stages remove resources. Stage 4 needs two reconciled revisions so
Crossplane uninstalls before its namespace is deleted. Do not advance before
checking the live cluster. Git describes desired state, not everything left
behind by earlier `prune: false` reconciliations.

| Stage | Change | Required evidence before advancing |
| --- | --- | --- |
| 1 (merged) | Give the active targets their own pruning scopes. Keep their resource specs unchanged. Delete the unused ErsatzTV manifest and references. | Owner reports all Kustomizations healthy after merging. Keep the live inventory for cleanup. |
| 2 (merged) | Empty the Purko/ZeroClaw scopes and remove Mattermost's HelmRelease/HTTPRoute, keeping its namespace/repository. Drain any live Crossplane composites and managed resources operationally, with controllers still running. | Owner reports step 2 merged and healthy. Verify live Crossplane managed-resource cleanup before removing providers. |
| 3 (merged) | Empty the Crossplane provider scope to prune the three declared Providers. Remove any discovered configuration/function packages and provider configs operationally. | Owner reports the PR merged. Verify Providers, revisions, their controller workloads, and dependent resources are gone before 4A. |
| 4A (this PR) | Remove Crossplane's HelmRelease; empty Mattermost's scope; remove obsolete provider Renovate rules. | Crossplane Helm uninstall has finished, Mattermost's namespace/source are gone, and retained storage is accounted for. |
| 4B (next reconciliation) | Remove Crossplane's namespace/source and empty scopes; finish retained CRD, storage, database, credential, and repository cleanup. | No target workloads, controllers, custom resources, dedicated PVs/Longhorn volumes, or unexplained finalizers remain. |

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
defined three Providers at stage 1 but no managed resources or provider configs;
this does not establish that the live cluster has none.

Inspect installed Purko CRDs and their instances, Helm hooks, and cluster-scoped
RBAC/webhooks from the actual release. Do not infer an empty cluster from its
disabled starter agents/presets/trigger rules in Git.

Record each dedicated PVC's bound PV, reclaim policy, CSI volume handle, and
Longhorn volume before removing the claim. Also record Mattermost's PostgreSQL
database/role and external OIDC clients. Keep inventory exports containing
credentials out of Git.

## Stage 2: remove apps and drain Crossplane-managed resources

### Changes applied in stage 2

`core/purko` and `services/automation/zeroclaw` now render zero resources. Their
existing Flux Kustomizations remain present with pruning enabled, so the next
reconciliation collects their previous inventories. Mattermost now renders only
its namespace and HelmRepository; its HelmRelease and HTTPRoute are collected.
The Crossplane controller and all three Providers are unchanged. No Crossplane
managed resources are declared in Git, so any live managed-resource teardown
must be done against the identified cluster objects before stage 3.

Before merging, capture `automation/zeroclaw-data`'s bound PV and CSI volume
handle using the inventory commands above: stage 2 deletes that PVC. Check for
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

Stage 3 removes the three Provider manifests (Backblaze, DigitalOcean, GitHub)
and leaves `platform/crossplane` with `resources: []`. The existing
`platform-crossplane` Flux Kustomization remains with pruning enabled, so it
deletes the Providers from its previous inventory. The Crossplane HelmRelease,
OCIRepository, namespace, and `core-crossplane` scope are unchanged.

Before merging, verify live composite/managed/claim resources have finished
deleting or have an explicit handoff. Stage-2 Kustomization health does not
prove this. Git contains no provider configs or managed resources to delete;
do not infer that manually created resources are absent. Capture the installed
Providers, ProviderRevisions, controller workloads, and their CRDs so stage 4
can account for retained objects even after the package inventory is gone.

```sh
# Before merging: inspect these while providers still run.
kubectl get managed -A -o yaml
kubectl get composite -A -o yaml
kubectl get claim -A -o yaml
kubectl get configurations.pkg.crossplane.io
kubectl get functions.pkg.crossplane.io
kubectl get providers.pkg.crossplane.io -o yaml
kubectl get providerrevisions.pkg.crossplane.io -o yaml
kubectl get deployment,pod -n crossplane --show-labels
kubectl get crd -o yaml

# After merging: collect the old provider inventory.
flux reconcile kustomization flux-system --with-source
flux reconcile kustomization platform-crossplane
flux tree kustomization platform-crossplane

# These exact Providers must disappear, not remain Terminating.
kubectl get providers.pkg.crossplane.io markopolo123-provider-backblaze --ignore-not-found
kubectl get providers.pkg.crossplane.io crossplane-contrib-provider-upjet-digitalocean --ignore-not-found
kubectl get providers.pkg.crossplane.io crossplane-contrib-provider-upjet-github --ignore-not-found
kubectl get providers.pkg.crossplane.io
kubectl get providerrevisions.pkg.crossplane.io
kubectl get deployment,pod -n crossplane --show-labels

# Crossplane itself must still be present for package finalization.
kubectl get helmrelease crossplane -n crossplane
flux tree kustomization core-crossplane
```

Check `platform-crossplane` applied the step-3 commit (or a descendant) and its
inventory is empty. Compare actual ProviderRevisions and provider controller
workloads against the captured inventory; an empty Flux inventory or healthy
Kustomization alone does not prove package cleanup finished. Do not advance to
stage 4 while provider finalizers, revisions, or controller workloads remain.
Resolve blocked cleanup with Crossplane running rather than removing finalizers.
Leave retained CRDs for the explicit stage-4 inventory check and cleanup.

After all composite/managed resources are drained, remove any discovered
Configuration packages that could recreate providers/functions. Empty
`platform/crossplane`, reconcile `platform-crossplane`, and confirm the three
Providers and their ProviderRevisions/controller deployments finish deletion.
Remove any additional installed providers, functions/revisions, provider configs,
runtime configs, compositions/revisions, and connection secrets identified in
the live inventory, in dependency order. Keep the Crossplane controller running
throughout this stage. Emptying Git alone cannot remove manually created items.

## Stage 4: uninstall Crossplane and finish cleanup

### 4A: uninstall Crossplane (this PR)

Remove only the Crossplane HelmRelease from `core/crossplane/release.yaml`.
Keep its namespace, OCIRepository, and `core-crossplane` scope in place until
Helm has finished uninstalling. The existing pruning scope deletes the
HelmRelease and helm-controller runs the uninstall. This revision also empties
`services/mattermost` to prune its remaining Namespace and HelmRepository after
the stage-2 application uninstall. All five retirement Flux scopes stay present.
Obsolete Crossplane provider Renovate matching and digest exceptions are removed.

Before merging, confirm Provider and ProviderRevision cleanup from stage 3 is
complete, including manually installed packages/functions/configurations. Do
not uninstall Crossplane with unresolved package or managed-resource finalizers.
Confirm Mattermost's HelmRelease and application workloads are gone, then
record any remaining Mattermost PVC/PV bindings and CSI volume handles: deleting
the namespace also deletes its retained PVCs. The Longhorn data still requires
explicit cleanup when its PV reclaim policy is Retain.

```sh
# Before merging: these must have no remaining packages needing Crossplane.
kubectl get providers.pkg.crossplane.io
kubectl get providerrevisions.pkg.crossplane.io
kubectl get configurations.pkg.crossplane.io
kubectl get functions.pkg.crossplane.io
kubectl get configurationrevisions.pkg.crossplane.io
kubectl get functionrevisions.pkg.crossplane.io
kubectl get deployment,pod -n crossplane --show-labels

# Mattermost's application must be gone; record any remaining storage first.
kubectl get helmrelease mattermost -n mattermost --ignore-not-found
kubectl get deployment,statefulset,pod -n mattermost
kubectl get pvc -n mattermost -o yaml
kubectl get pv -o yaml

# After merging: uninstall Crossplane and prune Mattermost's final inventory.
flux reconcile kustomization flux-system --with-source
flux reconcile kustomization core-crossplane
flux reconcile kustomization services-mattermost
kubectl get helmrelease crossplane -n crossplane --ignore-not-found
helm list -A --all
kubectl get deployment,pod -n crossplane
kubectl get namespace mattermost --ignore-not-found

# Expected: Crossplane retains just Namespace + OCIRepository; Mattermost empty.
flux tree kustomization core-crossplane
flux tree kustomization services-mattermost
```

Check the step-4A revision (or a descendant) was applied. The Crossplane
HelmRelease must be absent rather than Terminating, the Helm release must no
longer be installed/pending/failed-uninstall, and its controller workloads must
be gone. Mattermost's namespace must be absent, with no unexplained terminating
resources. A healthy Kustomization alone does not establish those results.
Keep CRDs available until any remaining custom resources/finalizers have been
resolved. No cluster or database commands run as part of preparing this PR.

### 4B: cleanup after uninstall is verified (next reconciliation)

1. After 4A has finished uninstalling Crossplane, confirm `core-crossplane`'s
   inventory contains only its Namespace and OCIRepository. Remove its bootstrap
   definition and directory in 4B: the live scope's `WaitForTermination` deletion
   policy collects those two remaining objects. Verify actual deletion, including
   any timeout/finalizer leftovers. No HelmRelease may remain at this point.
2. In the same 4B revision, remove the other empty retirement scopes/directories
   and the Mattermost bootstrap definition only after their inventories and
   actual resources are empty. The root `flux-system` scope has pruning enabled,
   so removing bootstrap definitions deletes the Flux scopes too.
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
6. Crossplane's Renovate provider matcher/digest exception and active
   architecture listings are removed in 4A. Remove the `crossplane` namespace
   entry from Velero's `daily-infra.template.includedNamespaces`, remove the
   remaining retirement-scope references from README/CLAUDE, and reconcile.
   Keep Velero and its Backblaze integration: they are separate services.
7. Compare the live cluster against the captured inventory, including external
   resources, rather than relying on `kubectl get all` (which omits CRs, PVCs,
   and other resource types). Account for every remaining object or stored copy.

## References

- [Flux resource ownership transfer](https://fluxcd.io/flux/faq/#how-can-i-safely-move-resources-from-one-dir-to-another)
- [Flux pruning and deletion policy](https://fluxcd.io/flux/components/kustomize/kustomizations/#prune)
- [Crossplane 2.4 uninstall order and retained CRDs](https://docs.crossplane.io/v2.4/guides/uninstall-crossplane/)
