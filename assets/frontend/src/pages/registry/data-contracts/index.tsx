import { useI18n } from "@/lib/i18n/useI18n";
import { RegistryCatalogSurface } from "@/pages/registry/shared/catalog-surface";

export function RegistryDataContractsPage() {
  const { t } = useI18n();
  return (
    <RegistryCatalogSurface
      description={t("react.registry.data_contracts_are_now_managed_from_react_while_preserving_current_backend_validation", "Data contracts are now managed from React while preserving current backend validation.")}
      endpoint="/api/data-contracts"
      idKey="contract_id"
      listQueryKey={["registry", "data-contracts"]}
      listGridId="contracts-grid"
      readOnlyNotice={t("react.registry.data_contracts_are_generated_and_synchronized_from_registry_definitions_direct_edits_are_disabled_so_workflow_", "Data contracts are generated and synchronized from registry definitions. Direct edits are disabled so workflow and runtime schemas stay consistent.")}
      readOnly
      secondaryLinks={[
        { label: t("react.registry.view_field_catalog", "View Field Catalog"), to: "/registry/fields" },
        { label: t("react.registry.view_published_blocks", "View Published Blocks"), to: "/registry/published-blocks" },
      ]}
      title={t("react.registry.data_contracts", "Data contracts")}
    />
  );
}
