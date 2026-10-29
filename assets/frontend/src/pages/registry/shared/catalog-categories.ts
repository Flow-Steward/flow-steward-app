import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/http/client";

/** One entry of the category list Core shares with the workflow, extension and MCP catalogs. */
export type CatalogCategory = { id: string; label: string };

export const CATALOG_CATEGORIES_QUERY_KEY = ["registry", "catalog-categories"] as const;

function categoriesFromPayload(payload: unknown): CatalogCategory[] {
  if (!Array.isArray(payload)) return [];
  return payload.flatMap((row) => {
    if (!row || typeof row !== "object") return [];
    const { id, label } = row as Record<string, unknown>;
    if (typeof id !== "string" || !id) return [];
    return [{ id, label: typeof label === "string" && label ? label : id }];
  });
}

/** Agents, tools and skills file under these categories and nothing else. */
export function useCatalogCategories() {
  const query = useQuery({
    queryKey: CATALOG_CATEGORIES_QUERY_KEY,
    queryFn: async () => categoriesFromPayload(await fetchJson<unknown>("/api/catalog-categories")),
    staleTime: Infinity,
  });
  return useMemo(() => {
    const categories = query.data ?? [];
    const labels = new Map(categories.map((category) => [category.id, category.label]));
    return {
      loading: query.isLoading,
      options: categories.map((category) => ({ value: category.id, label: category.label })),
      labelOf: (id: string) => labels.get(id) ?? id,
    };
  }, [query.data, query.isLoading]);
}
