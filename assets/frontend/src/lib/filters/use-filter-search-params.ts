import { useCallback, useEffect, useMemo, useState } from "react";
import { createSearchParams, useLocation, useNavigate, type SetURLSearchParams } from "react-router";

import { textFilterSchema } from "./filter-schemas";
import { readFilterState, useFilterStorageKey, writeFilterState } from "./use-filter-state";
import { useLatestSearchParams, type SearchParamsUpdate } from "@/lib/routing/use-latest-search-params";
import { ignoreAsyncResult } from "@/lib/utils/async";

function selectedParams(params: URLSearchParams, keys: readonly string[]): URLSearchParams {
  return new URLSearchParams(Array.from(params).filter(([key]) => keys.includes(key)));
}

/** Only allowlisted filters are remembered; dialogs, actions and page numbers stay transient. */
export function useLatestFilterSearchParams(
  scope: string,
  keys: readonly string[],
  enabled = true,
): [URLSearchParams, SearchParamsUpdate] {
  const storageKey = useFilterStorageKey(`query/${scope}`);
  const key = enabled ? storageKey : null;
  const location = useLocation();
  const navigate = useNavigate();
  const [params, update] = useLatestSearchParams();
  const source = params.toString();
  const load = (replaceSelection = false) => {
    const restored = new URLSearchParams(source);
    if (enabled && replaceSelection) keys.forEach((name) => restored.delete(name));
    // A link with any explicit filter specifies the complete selection, including empty values.
    if (enabled && (replaceSelection || !keys.some((name) => params.has(name)))) {
      const saved = readFilterState(key, textFilterSchema, "");
      selectedParams(new URLSearchParams(saved), keys).forEach((value, name) => restored.append(name, value));
    }
    return { key, identity: storageKey, scope, source, restored: restored.toString(), restoreOnEnable: !enabled && replaceSelection };
  };
  const [snapshot, setSnapshot] = useState(() => load());
  // Inspect the incoming URL before reseeding: one link may also activate the tab.
  const explicitSelectionChanged = source !== snapshot.source && keys.some((name) => params.has(name))
    && selectedParams(params, keys).toString() !== selectedParams(new URLSearchParams(snapshot.source), keys).toString();
  let current = snapshot;
  if (snapshot.key !== key || snapshot.identity !== storageKey || snapshot.scope !== scope) {
    const sameScope = snapshot.scope === scope;
    const identityChanged = sameScope && snapshot.identity !== null && storageKey !== null && snapshot.identity !== storageKey;
    current = load((identityChanged || (sameScope && snapshot.restoreOnEnable)) && !explicitSelectionChanged);
    setSnapshot(current);
  }
  const effectiveSource = source === current.source ? current.restored : source;
  // Consume the initial restore once: Back and Clear must not resurrect it.
  if (source !== current.source) {
    // A new explicit link in the new account wins over a pending identity restore.
    // Removing keys while inactive is also how a tab hides its filters from the URL.
    setSnapshot({ ...current, source, restored: source, restoreOnEnable: current.restoreOnEnable && !explicitSelectionChanged });
  }
  const effectiveParams = useMemo(() => new URLSearchParams(effectiveSource), [effectiveSource]);
  const selection = selectedParams(effectiveParams, keys).toString();
  useEffect(() => { if (enabled) writeFilterState(key, selection); }, [enabled, key, selection]);
  useEffect(() => {
    if (effectiveSource === source) return;
    ignoreAsyncResult(navigate({ pathname: location.pathname, search: effectiveSource ? `?${effectiveSource}` : "", hash: location.hash }, { replace: true, state: location.state }));
  }, [effectiveSource, source, location.pathname, location.hash, location.state, navigate]);
  return [effectiveParams, update];
}

/** The normal router setter interface for pages that already own URL-backed filters. */
export function useFilterSearchParams(scope: string, keys: readonly string[], enabled = true): [URLSearchParams, SetURLSearchParams] {
  const [params, update] = useLatestFilterSearchParams(scope, keys, enabled);
  const setParams = useCallback<SetURLSearchParams>((next, options) => {
    update((latest) => createSearchParams(typeof next === "function" ? next(latest) : next), { push: !options?.replace });
  }, [update]);
  return [params, setParams];
}
