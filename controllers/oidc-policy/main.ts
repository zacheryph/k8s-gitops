// OIDC SecurityPolicy controller — Metacontroller DecoratorController webhook.
// Watches every HTTPRoute and, for those labeled
// `cluster.routine.sh/oidc-credentials`, attaches an Envoy Gateway
// SecurityPolicy enabling OIDC authn on the route. Routes without the label get
// no attachment, so removing the label makes Metacontroller delete the policy.

export const OIDC_LABEL = "cluster.routine.sh/oidc-credentials";

// DecoratorController v1 hook request: the watched HTTPRoute arrives as `object`.
export interface HookRequest {
  object: {
    apiVersion: string;
    kind: string;
    metadata: { name: string; namespace: string; labels?: Record<string, string> };
  };
}

export interface ChildObject {
  apiVersion: string;
  kind: string;
  metadata: {
    name: string;
    namespace: string;
    labels: Record<string, string>;
  };
  spec: Record<string, unknown>;
}

/**
 * Build the Envoy SecurityPolicy child for an HTTPRoute parent.
 * Returns null when the route should have no generated child.
 * Throws when the route is selected but misconfigured (bad label value).
 */
export function buildSecurityPolicy(
  parent: HookRequest["object"],
  env: { issuer: string; cookieDomain: string },
): ChildObject | null {
  const secretName = parent.metadata.labels?.[OIDC_LABEL];
  if (secretName === undefined) return null; // unlabeled: no policy (deletes any existing one)
  if (!secretName) {
    throw new Error(
      `label ${OIDC_LABEL} is empty; expected the name of the Secret holding the OIDC client credentials`,
    );
  }
  return {
    apiVersion: "gateway.envoyproxy.io/v1alpha1",
    kind: "SecurityPolicy",
    metadata: {
      name: `${parent.metadata.name}-oidc`,
      namespace: parent.metadata.namespace,
      labels: {
        "source-httproute": `${parent.metadata.namespace}-${parent.metadata.name}`,
      },
    },
    spec: {
      targetRefs: [
        {
          group: "gateway.networking.k8s.io",
          kind: "HTTPRoute",
          name: parent.metadata.name,
        },
      ],
      oidc: {
        cookieDomain: env.cookieDomain,
        scopes: ["openid", "profile", "email", "groups"],
        provider: { issuer: env.issuer },
        clientIDRef: { kind: "Secret", name: secretName },
        clientSecret: { kind: "Secret", name: secretName },
      },
    },
  };
}

/** DecoratorController sync hook: desired attachments for one HTTPRoute. */
export function sync(
  hook: HookRequest,
  env: { issuer: string; cookieDomain: string },
): { attachments: ChildObject[] } {
  const child = buildSecurityPolicy(hook.object, env);
  return { attachments: child ? [child] : [] };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function serve(env: { issuer: string; cookieDomain: string }): void {
  Bun.serve({
    port: 8080,
    async fetch(req): Promise<Response> {
      const url = new URL(req.url);
      switch (url.pathname) {
        case "/healthz":
        case "/readyz":
          return new Response("ok");
        case "/sync":
          if (req.method !== "POST") {
            return jsonResponse({ error: "method not allowed" }, 405);
          }
          try {
            return jsonResponse(sync((await req.json()) as HookRequest, env));
          } catch (err) {
            console.error("sync failed:", err);
            return jsonResponse({ error: String(err) }, 500);
          }
        default:
          return jsonResponse({ error: "not found" }, 404);
      }
    },
  });
}

if (import.meta.main) {
  const env = {
    issuer: process.env.OIDC_ISSUER_URL ?? "",
    cookieDomain: process.env.OIDC_COOKIE_DOMAIN ?? "",
  };
  if (!env.issuer || !env.cookieDomain) {
    console.error("OIDC_ISSUER_URL and OIDC_COOKIE_DOMAIN must be set");
    process.exit(1);
  }
  serve(env);
}
