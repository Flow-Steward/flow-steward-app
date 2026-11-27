import { useContext, useMemo } from "react";

import type { AuthSession } from "@/lib/auth/AuthProvider";
import { AuthContext } from "@/lib/auth/context";

/** An entitled feature as the interface sees it (ADR 0082). */
export type FeatureState = {
  /** The flag is on and the server named where the feature's API lives. */
  enabled: boolean;
  /** The feature's base URL; empty while the feature is not enabled. */
  endpoint: string;
};

const DISABLED: FeatureState = { enabled: false, endpoint: "" };

/**
 * A feature is offered only when the bootstrap both turns its flag on and names
 * its endpoint: a flag without an endpoint, or an endpoint without a flag, shows
 * nothing.
 */
export function featureState(
  session: Pick<AuthSession, "feature_flags" | "feature_endpoints"> | null | undefined,
  name: string,
): FeatureState {
  if (session?.feature_flags?.[name] !== true) return DISABLED;
  const endpoint = (session.feature_endpoints?.[name] ?? "").trim();
  return endpoint ? { enabled: true, endpoint } : DISABLED;
}

/**
 * The current session's state of one feature. Reads the auth context directly,
 * so outside an AuthProvider the feature is simply not enabled.
 */
export function useFeature(name: string): FeatureState {
  const session = useContext(AuthContext)?.session;
  return useMemo(() => featureState(session, name), [session, name]);
}

/**
 * The features a page's `required_entitlement` names: one feature, or a list of
 * which any one opens the page (ADR 0082). Names compare in lower case.
 */
export function requiredEntitlements(value: unknown): string[] {
  const items = Array.isArray(value) ? value : [value];
  return items
    .map((item) => (typeof item === "string" ? item.trim().toLowerCase() : ""))
    .filter((item) => item !== "");
}

/**
 * Whether the account holds what a page requires: nothing named, or any one of
 * the named features switched on in the bootstrap's feature flags.
 */
export function holdsAnyEntitlement(
  flags: Record<string, boolean> | null | undefined,
  required: unknown,
): boolean {
  const features = requiredEntitlements(required);
  if (features.length === 0) return true;
  const held = new Set(
    Object.entries(flags ?? {})
      .filter(([, enabled]) => enabled === true)
      .map(([name]) => name.trim().toLowerCase()),
  );
  return features.some((feature) => held.has(feature));
}
