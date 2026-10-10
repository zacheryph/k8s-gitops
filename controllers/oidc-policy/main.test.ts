import { test, expect } from "bun:test";
import { buildSecurityPolicy, OIDC_LABEL } from "./main";

const env = { issuer: "https://id.example.com", cookieDomain: "example.com" };

const parent = {
  apiVersion: "gateway.networking.k8s.io/v1",
  kind: "HTTPRoute",
  metadata: {
    name: "vaultwarden",
    namespace: "general",
    labels: { [OIDC_LABEL]: "vaultwarden-oidc-credentials" },
  },
};

test("generates a SecurityPolicy wired to the labeled secret", () => {
  const child = buildSecurityPolicy(parent, env)!;
  expect(child.metadata.name).toBe("vaultwarden-oidc");
  expect(child.metadata.namespace).toBe("general");
  expect(child.metadata.labels["source-httproute"]).toBe("general-vaultwarden");
  expect(child.spec.oidc).toEqual({
    cookieDomain: "example.com",
    scopes: ["openid", "profile", "email", "groups"],
    provider: { issuer: "https://id.example.com" },
    clientIDRef: { kind: "Secret", name: "vaultwarden-oidc-credentials" },
    clientSecret: { kind: "Secret", name: "vaultwarden-oidc-credentials" },
  });
  expect(child.spec.targetRefs).toEqual([
    { group: "gateway.networking.k8s.io", kind: "HTTPRoute", name: "vaultwarden" },
  ]);
});

test("returns null for unselected routes", () => {
  const unselected = {
    ...parent,
    metadata: { name: parent.metadata.name, namespace: parent.metadata.namespace },
  };
  expect(buildSecurityPolicy(unselected, env)).toBeNull();
});

test("throws on an empty label value", () => {
  const bad = {
    ...parent,
    metadata: { ...parent.metadata, labels: { [OIDC_LABEL]: "" } },
  };
  expect(() => buildSecurityPolicy(bad, env)).toThrow(/empty/);
});
