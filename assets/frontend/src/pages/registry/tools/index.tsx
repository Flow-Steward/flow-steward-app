import { useI18n } from "@/lib/i18n/useI18n";
import { RegistryCatalogSurface } from "@/pages/registry/shared/catalog-surface";

export function RegistryToolsPage() {
  const { t } = useI18n();
  return (
    <RegistryCatalogSurface
      createLabel={t("react.registry.tool", "Tool")}
      description={t("react.registry.tools_registry_is_now_react_native_and_calls_the_existing_api_tools_control_plane_apis", "Tools registry is now React-native and calls the existing `/api/tools` control-plane APIs.")}
      endpoint="/api/tools"
      idKey="tool_id"
      listQueryKey={["registry", "tools"]}
      listGridId="tools-grid"
      title={t("react.registry.tools_catalog", "Tools catalog")}
    />
  );
}
