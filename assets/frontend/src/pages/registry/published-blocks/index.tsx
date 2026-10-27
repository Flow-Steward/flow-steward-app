import { useI18n } from "@/lib/i18n/useI18n";
import { RegistryCatalogSurface } from "@/pages/registry/shared/catalog-surface";

export function RegistryPublishedBlocksPage() {
  const { t } = useI18n();
  return (
    <RegistryCatalogSurface
      description={t("react.registry.published_blocks_are_now_managed_in_react_with_json_first_contract_fidelity", "Published blocks are now managed in React with JSON-first contract fidelity.")}
      endpoint="/api/published-blocks"
      idKey="block_key"
      listQueryKey={["registry", "published-blocks"]}
      listGridId="published-blocks-grid"
      readOnlyNotice={t("react.registry.published_blocks_are_runtime_facing_contract_snapshots_direct_edits_are_disabled_so_executor_mappings_and_work", "Published blocks are runtime-facing contract snapshots. Direct edits are disabled so executor mappings and workflow references stay stable.")}
      readOnly
      secondaryLinks={[
        { label: t("react.registry.view_field_catalog", "View Field Catalog"), to: "/registry/fields" },
        { label: t("react.registry.view_data_contracts", "View Data Contracts"), to: "/registry/data-contracts" },
      ]}
      title={t("react.registry.published_blocks_variant_b99dc4bf", "Published blocks")}
    />
  );
}
