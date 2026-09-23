# OpenClaw external exposure (webhooks + native apps + Control UI)

## Goal

Expose the OpenClaw gateway externally at `openclaw.${CLUSTER_DOMAIN}` so that:

- external services can deliver inbound webhooks (`POST /hooks/...`),
- the native desktop/mobile apps can connect over the Gateway WebSocket,
- the browser Control UI is reachable.

## Security model (why no proxy-level OIDC)

OpenClaw has no OIDC support. Its gateway auth modes are shared-secret token,
password, trusted-proxy identity headers, and Tailscale Serve identity. The
native apps authenticate with the gateway token
(`connect.params.auth.token` / `gateway.remote.token`) plus one-time device
pairing (per-device tokens with scopes). Webhooks use a dedicated `hooks.token`.
The Kyverno-generated Envoy OIDC SecurityPolicy (`cluster.routine.sh/oidc-credentials`
label) is browser-redirect auth — it would break WebSocket upgrades from native
apps and webhook POSTs, so the route deliberately omits the label. OpenClaw's
own token/pairing auth is the access boundary.

## Changes

- `services/automation/openclaw.yaml`
  - `OPENCLAW_GATEWAY_TOKEN` added to the ExternalSecret (1Password item
    `openclaw`, field `gateway_token`; read natively by the gateway).
  - `config.raw.gateway.controlUi.allowedOrigins` set for the public origin.
  - `config.raw.hooks` enabled at `/hooks` with `token: ${OPENCLAW_HOOKS_TOKEN}`
    (cluster-secrets substitution — upstream `hooks.token` has no SecretRef/env
    source). Restricted to agent `main`.
  - `config.forcePaths` gains `hooks` so self-config can never edit webhook
    ingress.
  - `security.networkPolicy.allowedIngressNamespaces: [gateway]` admits only
    the Envoy gateway namespace.
  - Standalone HTTPRoute `openclaw.${CLUSTER_DOMAIN}` → Service `openclaw:18789`
    (nginx gateway-proxy sidecar), external parentRef, `dns.routine.sh/external`
    opt-in annotation. Canvas/preview port intentionally unrouted.

## Setup before merge

1. 1Password item `openclaw`: add field `gateway_token` — generate with
   `openclaw doctor --generate-gateway-token` or any long random string.
2. `scripts/cluster-secrets set OPENCLAW_HOOKS_TOKEN` — dedicated long random
   value, distinct from the gateway token (security audit flags reuse).

## Verification

- `curl https://openclaw.${CLUSTER_DOMAIN}/hooks/...` with and without the hook
  token (401/403 without).
- `openclaw gateway health` from a workstation pointed at
  `wss://openclaw.${CLUSTER_DOMAIN}` with the gateway token.
- First app pairing via `openclaw dashboard` single-use pairing link.
