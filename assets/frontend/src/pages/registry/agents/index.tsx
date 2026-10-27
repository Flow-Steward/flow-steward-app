import { useI18n } from "@/lib/i18n/useI18n";
import { RegistryCatalogSurface } from "@/pages/registry/shared/catalog-surface";

export function RegistryAgentsPage() {
  const { t } = useI18n();
  return (
    <RegistryCatalogSurface
      createLabel={t("react.registry.agent", "Agent")}
      description={t("react.registry.manage_registry_agents_directly_from_react_with_json_first_parity_against_the_existing_admin_contracts", "Manage registry agents directly from React with JSON-first parity against the existing admin contracts.")}
      endpoint="/api/agents"
      idKey="agent_id"
      listQueryKey={["registry", "agents"]}
      listGridId="agents-grid"
      title={t("react.registry.agents_catalog", "Agents catalog")}
    />
  );
}
