import { useI18n } from "@/lib/i18n/useI18n";
import { PageContainer } from "@/components/shared/design-system";
import { RegistryDetailSurface } from "@/pages/registry/shared/catalog-detail-surface";

export function RegistryDataContractsDetailPage() {
  const { t } = useI18n();
  return (
    <PageContainer width="standard">
      <RegistryDetailSurface
        endpoint="/api/data-contracts"
        idKey="contract_id"
        idParam="contractId"
        listPath="/registry/data-contracts"
        readOnly
        readOnlyNotice={t("react.registry.data_contracts_are_generated_and_synchronized_from_registry_definitions_direct_mutations_are_disabled_so_workf", "Data contracts are generated and synchronized from registry definitions. Direct mutations are disabled so workflow and runtime schemas stay consistent.")}
        title={t("registry.nav.data_contracts", "Data Contracts")}
      />
    </PageContainer>
  );
}
