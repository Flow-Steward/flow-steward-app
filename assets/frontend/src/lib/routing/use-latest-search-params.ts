import { useCallback, useEffect, useMemo, useRef } from "react";

import { useLocation, useNavigate } from "react-router";

import { ignoreAsyncResult } from "@/lib/utils/async";

/** Change the query string from the latest one; it never sees a stale snapshot. */
export type SearchParamsUpdate = (
  update: (latest: URLSearchParams) => URLSearchParams,
  options?: { push?: boolean; hash?: string },
) => void;

/**
 * The query string, and a writer that always starts from the latest one.
 *
 * Router's own functional updater is handed the params of the last render, so
 * two writes before React renders again (two quick facet clicks) both start
 * from the same snapshot and the second drops the first. Here every write
 * starts from the last one not yet in the URL. The hash is kept unless a write
 * names a new one, because a tab lives in it.
 */
export function useLatestSearchParams(): [URLSearchParams, SearchParamsUpdate] {
  const location = useLocation();
  const navigate = useNavigate();
  const params = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const pendingRef = useRef<{ search: string; hash: string } | null>(null);

  // Once the router renders a new location, it is the latest one: writes
  // queued before that render have all landed in it, and a write made
  // elsewhere on the page (a tab switch) supersedes a pending one here.
  useEffect(() => {
    pendingRef.current = null;
  }, [location.key, location.search, location.hash]);

  const update = useCallback<SearchParamsUpdate>(
    (change, options) => {
      const latest = pendingRef.current ?? { search: location.search.replace(/^\?/, ""), hash: location.hash };
      const search = change(new URLSearchParams(latest.search)).toString();
      const hash = options?.hash ?? latest.hash;
      if (search === latest.search && hash === latest.hash) return;
      pendingRef.current = { search, hash };
      ignoreAsyncResult(
        navigate({ pathname: location.pathname, search: search ? `?${search}` : "", hash }, { replace: !options?.push }),
      );
    },
    [location.pathname, location.search, location.hash, navigate],
  );

  return [params, update];
}
