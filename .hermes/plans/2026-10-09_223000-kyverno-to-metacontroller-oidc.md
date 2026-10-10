# Replace Kyverno with a Metacontroller-managed OIDC SecurityPolicy controller

## Context

Kyverno was installed solely for one ClusterPolicy:
`httproute-oidc-security-policy` — a background generate policy that creates an
Envoy Gateway `SecurityPolicy` (OIDC) for every HTTPRoute labeled
`cluster.routine.sh/oidc-credentials`. Live-cluster inventory confirmed it is
the only Kyverno ClusterPolicy (no validate/mutate policies, no backup
generation policies in use).

## Design

- **New controller** (`controllers/oidc-policy/`): a Metacontroller
  DecoratorController webhook written in TypeScript for Bun
  (`Bun.serve`). One `/sync` endpoint maps an HTTPRoute to a
  `gateway.envoyproxy.io/v1alpha1/SecurityPolicy` attachment, replicating the
  Kyverno template (secret name from the label value, issuer +
  cookie domain from env, same `source-httproute` label). Scopes are now four
  separate entries; Kyverno's `[ openid profile email groups ]` was a single
  YAML string.
- **DecoratorController** custom resource (in `platform/security/`) watches
  **all** HTTPRoutes (no labelSelector, `ignoreStatusChanges: true`).
  Unlabeled routes get no attachment, so removing the label deletes the
  SecurityPolicy, matching Kyverno's `synchronize: true`. A labelSelector would
  stop syncing the route on label removal and orphan the policy.
  `InPlace` updates, `resyncPeriodSeconds: 600`.
- **Why not CompositeController**: it requires the parent to have
  `spec.selector` (HTTPRoutes don't) and overwrites the parent's entire
  `.status` each sync, which would fight Envoy Gateway over `status.parents`.
- **CI** (`.github/workflows/oidc-policy.yml`): triggered only on changes
  under `controllers/oidc-policy/` (plus the workflow file itself).
  Runs `bun test`, builds/pushes `ghcr.io/zacheryph/oidc-policy-controller`
  tagged `sha-<full-sha>`, then opens an automated PR pinning
  `platform/security/oidc-policy-controller.yaml` to
  `tag: sha-<sha>@sha256:<digest>`. Requires Actions to be enabled on the
  repo; action pins are version tags initially — Renovate
  (`helpers:pinGitHubActionDigests`) will open the digest-pinning PRs.

## Bootstrap sequence (merge order matters)

1. This PR: controller deployment points at a placeholder tag
   (`sha-bootstrap@sha256:0000…`); Flux will retry until the real image lands.
2. After merge the workflow runs on the merge push (the controller dir changed),
   pushes the image and opens an image-bump PR. GHCR packages start **private**:
   make `oidc-policy-controller` public in the package settings (or add an
   imagePullSecret) before the next step.
3. Merge the automated image-bump PR → controller deploys.
4. Uninstall Kyverno **before** touching its CRDs/ClusterPolicy (repo has
   `prune: false`). With `synchronize: true`, a running Kyverno can delete the
   generated SecurityPolicies when the ClusterPolicy goes away, which would drop
   OIDC from every route:
   - `flux suspend helmrelease kyverno -n kyverno`
   - `helm uninstall kyverno -n kyverno`
   - then `kubectl delete crd -l app.kubernetes.io/part-of=kyverno` and
     `kubectl delete ns kyverno`
5. Hand the existing policies to the controller. Metacontroller only manages
   attachments it created; the Kyverno-made `<route>-oidc` policies keep
   working but stay unmanaged (its create gets AlreadyExists, which it ignores).
   Recreate each one, nudging the route so the resync happens immediately
   (only a few seconds without OIDC per route):
   ```sh
   kubectl get httproute -A -l cluster.routine.sh/oidc-credentials \
     -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name}{"\n"}{end}' |
   while read -r ns name; do
     kubectl -n "$ns" delete securitypolicy "${name}-oidc" --ignore-not-found
     kubectl -n "$ns" annotate httproute "$name" --overwrite \
       oidc-policy.routine.sh/resync="$(date +%s)"
   done
   ```
   Verify each policy now has an `ownerReferences` entry pointing at its route.

## Tasks

- [x] Write Bun controller + tests (`controllers/oidc-policy/`)
- [x] Write GH Actions workflow with path filter + automated image-bump PR
- [x] Add `platform/security/oidc-policy-controller.yaml` (HelmRelease + DecoratorController)
- [x] Remove `core/kyverno.yaml`, `platform/security/policy.yaml`
- [x] Remove kyverno from Velero `daily-infra` namespace list
- [x] Remove kyverno Grafana dashboard
- [x] Validate: `kubectl kustomize` both layers, kubeconform (schema-availability skips pre-existing)
