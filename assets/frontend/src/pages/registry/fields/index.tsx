import { useI18n } from "@/lib/i18n/useI18n";
import { RegistryCatalogSurface } from "@/pages/registry/shared/catalog-surface";

export function RegistryFieldsPage() {
  const { t } = useI18n();
  return (
    <RegistryCatalogSurface
      description={t("react.registry.field_catalog_entries_are_derived_from_registry_contracts_and_available_for_inspection", "Field catalog entries are derived from registry contracts and available for inspection.")}
      endpoint="/api/fields"
      idKey="field_key"
      listQueryKey={["registry", "fields"]}
      listGridId="fields-grid"
      readOnlyNotice={t("react.registry.fields_are_derived_from_workflow_skill_and_published_block_contracts_direct_edits_are_disabled_to_avoid_breaki", "Fields are derived from workflow, skill, and published block contracts. Direct edits are disabled to avoid breaking dependent runtime mappings.")}
      readOnly
      secondaryLinks={[
        { label: t("react.registry.view_data_contracts", "View Data Contracts"), to: "/registry/data-contracts" },
        { label: t("react.registry.view_published_blocks", "View Published Blocks"), to: "/registry/published-blocks" },
      ]}
      title={t("react.registry.fields", "Fields")}
    />
  );
}
