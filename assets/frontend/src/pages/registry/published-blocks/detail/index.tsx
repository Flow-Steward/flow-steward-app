import { useI18n } from "@/lib/i18n/useI18n";
import { PageContainer } from "@/components/shared/design-system";
import { RegistryDetailSurface } from "@/pages/registry/shared/catalog-detail-surface";

export function RegistryPublishedBlocksDetailPage() {
  const { t } = useI18n();
  return (
    <PageContainer width="standard">
      <RegistryDetailSurface
        endpoint="/api/published-blocks"
        idKey="block_key"
        idParam="blockKey"
        listPath="/registry/published-blocks"
        readOnly
        readOnlyNotice={t("react.registry.published_blocks_are_runtime_facing_contract_snapshots_direct_mutations_are_disabled_so_executor_mappings_and_", "Published blocks are runtime-facing contract snapshots. Direct mutations are disabled so executor mappings and workflow references stay stable.")}
        title={t("react.registry.published_blocks", "Published Blocks")}
      />
    </PageContainer>
  );
}
