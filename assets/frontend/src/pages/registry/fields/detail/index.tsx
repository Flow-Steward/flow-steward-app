import { useI18n } from "@/lib/i18n/useI18n";
import { PageContainer } from "@/components/shared/design-system";
import { RegistryDetailSurface } from "@/pages/registry/shared/catalog-detail-surface";

export function RegistryFieldsDetailPage() {
  const { t } = useI18n();
  return (
    <PageContainer width="standard">
      <RegistryDetailSurface
        endpoint="/api/fields"
        idKey="field_key"
        idParam="fieldKey"
        listPath="/registry/fields"
        readOnly
        readOnlyNotice={t("react.registry.fields_are_derived_from_workflow_skill_and_published_block_contracts_direct_mutations_are_disabled_to_avoid_br", "Fields are derived from workflow, skill, and published block contracts. Direct mutations are disabled to avoid breaking dependent runtime mappings.")}
        title={t("react.registry.fields", "Fields")}
      />
    </PageContainer>
  );
}
