# Forgejo Runner Implementation Plan

> **For Hermes:** Implemented in `services/development` as a Flux OCI HelmRelease.

**Goal:** Deploy a Forgejo Actions runner so Forgejo can execute CI jobs. The runner and all job pods run in a dedicated `forgejo-runner` namespace for isolation from the main `development` workload.

**Architecture:** Use the upstream `forgejo-runner` chart from the plugin author (`oci://git.erwanleboucher.dev/eleboucher/charts/forgejo-runner`, chart v12.10.9). The chart runs the Forgejo runner (v12 fork with Kubernetes backend support) alongside its native gRPC k8s plugin as a native sidecar (initContainer with `restartPolicy: Always`; requires Kubernetes 1.29+, cluster is on 1.36). Each CI job is created as an ephemeral pod in the `forgejo-runner` namespace via in-cluster ServiceAccount — no Docker-in-Docker, no privileged containers.

- Runner daemon fetches jobs over the internal forgejo-http service; registration UUID/token come from `cluster-secrets` (`FORGEJO_RUNNER_UUID`, `FORGEJO_RUNNER_TOKEN`) substituted into a plain Secret manifest (same pattern as `opnsense-exporter`).
- Chart RBAC grants the runner's ServiceAccount jobs/pods permissions only inside its own namespace.
- Labels exposed to workflows: `ubuntu-latest` and `default`, running `ghcr.io/bjw-s-labs/forgejo-runner:ubuntu-24.04` job pods, digest-pinned, capped at 2 concurrent jobs with resource limits.
- No HTTPRoute: the runner makes only outbound connections (fetch + cache proxy on localhost).

**Tech Stack:** Flux OCIRepository + HelmRelease, eleboucher forgejo-runner chart, bjw-s runner-k8s-plugin sidecar.

---

### Task 1: Register the upstream OCI chart

**Files:**
- Create: `services/development/forgejo-runner-repo.yaml`

Add an OCIRepository named `forgejo-runner` pointing to `oci://git.erwanleboucher.dev/eleboucher/charts/forgejo-runner`, pinned to chart version `12.10.9`.

### Task 2: Add the namespace

**Files:**
- Modify: `services/development/forgejo-runner.yaml`

Add a Namespace `forgejo-runner`. It is not `privileged`-labeled: nothing here needs elevated pods.

### Task 3: Registration Secret

**Files:**
- Create: `services/development/forgejo-runner-secret.yaml`

Plain Secret `forgejo-runner-registration` in namespace `forgejo-runner` with keys `uuid` / `token` populated through `${FORGEJO_RUNNER_UUID}` / `${FORGEJO_RUNNER_TOKEN}` cluster-secrets substitution.

### Task 4: HelmRelease

**Files:**
- Modify: `services/development/forgejo-runner.yaml`

Configure:
- `forgejo.url`: internal service URL for the existing Forgejo instance
- `forgejo.existingSecret`: `forgejo-runner-registration`
- runner image `registry.erwanleboucher.dev/eleboucher/runner:12.13.0`
- `runnerConfig` with plugin socket `unix:///plugin/forgejo-runner-k8s.sock`, plugin options targeting the release namespace, cache enabled, capacity 2
- `podSpecs` default podspec with digest-pinned job image and cpu/memory requests+limits
- runner resources requests/limits
- keep chart defaults for RBAC (namespaced Role/RoleBinding), non-root securityContext, reloader annotation

### Task 5: Wire into Kustomization

**Files:**
- Modify: `services/development/kustomization.yaml`

Add `forgejo-runner.yaml`, `forgejo-runner-secret.yaml`, and `forgejo-runner-repo.yaml` to resources.

### Task 6: Validate and inspect

Run:
```bash
kubectl kustomize services/development/ >/dev/null && echo OK
git diff --check && git diff --stat
```

Post-merge verification:
```bash
flux reconcile kustomization services-development --with-source
flux get helmreleases -n forgejo-runner
kubectl -n forgejo-runner get pods   # runner Ready 2/2 (runner + plugin sidecar)
# trigger a test workflow in any repo:
#   .forgejo/workflows/ci.yaml with runs-on: ubuntu-latest
```

Note: after merging, create an image pull secret only if ghcr rate limits become a problem — not needed initially since both images are public.
