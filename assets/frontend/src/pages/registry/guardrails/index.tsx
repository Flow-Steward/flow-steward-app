import { useI18n } from "@/lib/i18n/useI18n";
import { RegistryCatalogSurface } from "@/pages/registry/shared/catalog-surface";

export function RegistryGuardrailsPage() {
  const { t } = useI18n();
  return (
    <RegistryCatalogSurface
      createLabel={t("react.registry.guardrail", "Guardrail")}
      description={t("react.registry.guardrail_policy_entries_can_be_listed_and_managed_directly_from_react", "Guardrail policy entries can be listed and managed directly from React.")}
      endpoint="/api/guardrails"
      idKey="id"
      listQueryKey={["registry", "guardrails"]}
      listGridId="guardrails-grid"
      title={t("react.registry.guardrails_catalog", "Guardrails catalog")}
    />
  );
}
