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
  CompositeController webhook written in TypeScript for Bun
  (`Bun.serve`). One `/sync` endpoint maps a parent HTTPRoute to a
  `gateway.envoyproxy.io/v1alpha1/SecurityPolicy` child, replicating the
  Kyverno template 1:1 (secret name from the label value, issuer +
  cookie domain from env, same scopes, same `source-httproute` label).
- **CompositeController** custom resource (in `platform/security/`) selects
  HTTPRoutes by label and adopts existing `-oidc` SecurityPolicies
  (same naming as Kyverno's generate, `InPlace` update strategy,
  `resyncPeriodSeconds: 600` ≈ Kyverno's background reconciliation).
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
2. After merge, trigger the workflow (it also runs automatically on the merge
   push since the controller dir changed).
3. Merge the automated image-bump PR → controller deploys.
4. CompositeController adopts the existing `<route>-oidc` SecurityPolicies
   (identical names) — no downtime or churn on the gateway.
5. Manual cleanup (repo has `prune: false`, so removals are not automated):
   - `flux suspend helmrelease kyverno -n kyverno` (optional, avoids re-install races)
   - `helm uninstall kyverno -n kyverno` then `kubectl delete ns kyverno`
   - delete the kyverno CRDs (`kubectl delete crd -l app.kubernetes.io/part-of=kyverno`)

## Tasks

- [x] Write Bun controller + tests (`controllers/oidc-policy/`)
- [x] Write GH Actions workflow with path filter + automated image-bump PR
- [x] Add `platform/security/oidc-policy-controller.yaml` (HelmRelease + CompositeController)
- [x] Remove `core/kyverno.yaml`, `platform/security/policy.yaml`
- [x] Remove kyverno from Velero `daily-infra` namespace list
- [x] Remove kyverno Grafana dashboard
- [x] Validate: `kubectl kustomize` both layers, kubeconform (schema-availability skips pre-existing)
