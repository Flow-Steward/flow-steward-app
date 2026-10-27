import { useI18n } from "@/lib/i18n/useI18n";
import { ChevronRight } from "lucide-react";

import { DsCheckbox } from "@/components/shared/design-system";

import { type DatasetResource } from "./dataset-page-helpers";

type ResourceSelectionListProps = {
  resources: DatasetResource[];
  selectedResources: Record<string, string[] | null>;
  onToggleResource: (resource: DatasetResource, selected: boolean) => void;
  onSetAllColumns: (resource: DatasetResource, allColumns: boolean) => void;
  onToggleColumn: (resource: DatasetResource, columnName: string, selected: boolean) => void;
};

export function ResourceSelectionList({ resources, selectedResources, onToggleResource, onSetAllColumns, onToggleColumn }: Readonly<ResourceSelectionListProps>) {
  const { t } = useI18n();
  return resources.map((resource) => {
    const selectedColumns = selectedResources[resource.resource_id];
    const selected = resource.resource_id in selectedResources;
    const allColumnsSelected = selected && selectedColumns === null;
    return (
      <details className="fs-disclosure" key={resource.resource_id}>
        <summary>
          <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
          {/* Selecting a resource must not also toggle its disclosure. */}
          <span
            className="flex items-center"
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
            role="presentation"
          >
            <DsCheckbox checked={selected} onChange={(checked) => onToggleResource(resource, checked)}>
              {resource.resource_id}
            </DsCheckbox>
          </span>
          <span className="fs-field-helper">
            {resource.resource_type || t("react.registry.resource_variant_5de95319", "resource")} · {resource.columns.length} {" "}{t("react.registry.columns", "columns")}</span>
        </summary>
        <div className="fs-disclosure-body">
          {selected ? (
            <>
              <DsCheckbox checked={allColumnsSelected} onChange={(checked) => onSetAllColumns(resource, checked)}>
                {t("react.registry.all_columns", "All columns")}</DsCheckbox>
              {!allColumnsSelected ? (
                <div className="grid gap-2 md:grid-cols-2">
                  {resource.columns.map((column) => (
                    <DsCheckbox
                      checked={Array.isArray(selectedColumns) ? selectedColumns.includes(column.name) : true}
                      key={column.name}
                      onChange={(checked) => onToggleColumn(resource, column.name, checked)}
                    >
                      {column.name}
                    </DsCheckbox>
                  ))}
                </div>
              ) : null}
            </>
          ) : (
            <span className="fs-field-helper">{t("react.registry.select_this_resource_to_choose_its_columns", "Select this resource to choose its columns.")}</span>
          )}
        </div>
      </details>
    );
  });
}
