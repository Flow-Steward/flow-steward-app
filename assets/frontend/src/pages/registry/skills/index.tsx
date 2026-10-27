import { useI18n } from "@/lib/i18n/useI18n";
import { RegistryCatalogSurface } from "@/pages/registry/shared/catalog-surface";

export function RegistrySkillsPage() {
  const { t } = useI18n();
  return (
    <RegistryCatalogSurface
      createLabel={t("trash.type.registry_skill", "Skill")}
      description={t("react.registry.skill_contracts_and_metadata_are_editable_from_the_react_registry_surface", "Skill contracts and metadata are editable from the React registry surface.")}
      endpoint="/api/skills"
      idKey="skill_id"
      listQueryKey={["registry", "skills"]}
      listGridId="skills-grid"
      title={t("react.registry.skills_catalog", "Skills catalog")}
    />
  );
}
