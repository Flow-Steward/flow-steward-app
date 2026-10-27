import { useMemo, useState } from "react";

import { type DatasetResource, type DatasetSchemaSnapshot } from "./dataset-page-helpers";

export function useResourceSelection(schemaSnapshot: DatasetSchemaSnapshot) {
  const [resourceSearch, setResourceSearch] = useState("");
  const [selectedResources, setSelectedResources] = useState<Record<string, string[] | null>>({});
  const filteredResources = useMemo(() => {
    const query = resourceSearch.trim().toLowerCase();
    if (!query) return schemaSnapshot.resources;
    return schemaSnapshot.resources.filter((resource) =>
      resource.resource_id.toLowerCase().includes(query)
      || resource.columns.some((column) => column.name.toLowerCase().includes(query)),
    );
  }, [resourceSearch, schemaSnapshot.resources]);

  function toggleResource(resource: DatasetResource, enabled: boolean) {
    setSelectedResources((current) => {
      if (!enabled) {
        const next = { ...current };
        delete next[resource.resource_id];
        return next;
      }
      return { ...current, [resource.resource_id]: null };
    });
  }

  function setAllColumns(resource: DatasetResource, allColumns: boolean) {
    setSelectedResources((current) => ({
      ...current,
      [resource.resource_id]: allColumns ? null : resource.columns.map((column) => column.name),
    }));
  }

  function toggleColumn(resource: DatasetResource, columnName: string, checked: boolean) {
    setSelectedResources((current) => {
      const currentColumns = current[resource.resource_id];
      const nextColumns = new Set(
        Array.isArray(currentColumns) ? currentColumns : resource.columns.map((column) => column.name),
      );
      if (checked) nextColumns.add(columnName);
      else nextColumns.delete(columnName);
      return { ...current, [resource.resource_id]: Array.from(nextColumns) };
    });
  }

  return {
    resourceSearch, setResourceSearch, selectedResources, setSelectedResources,
    filteredResources, toggleResource, setAllColumns, toggleColumn,
    selectedResourceCount: Object.keys(selectedResources).length,
  };
}
