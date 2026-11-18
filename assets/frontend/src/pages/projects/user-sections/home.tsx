import { useQuery } from "@tanstack/react-query";
import { Navigate } from "react-router";

import {
  buildProjectSectionNavigation,
  fetchSectionNavigationProjects,
  firstSectionPath,
} from "@/components/layout/project-section-navigation";
import { DsAlert, PageContainer } from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { useI18n } from "@/lib/i18n/useI18n";
import { queryKeys } from "@/lib/query/keys";

/** An operator's or viewer's landing page: the first section, or why there is none. */
export function SectionsHomePage() {
  const { t } = useI18n();
  const projects = useQuery({
    queryKey: queryKeys.sectionNavigation,
    queryFn: fetchSectionNavigationProjects,
    staleTime: 60_000,
  });
  if (projects.isLoading) {
    return <PageLoadingState label={t("react.user_sections.loading_menu", "Finding your sections")} />;
  }
  const first = firstSectionPath(
    buildProjectSectionNavigation(projects.data ?? [], { singleLabel: "" }),
  );
  if (first) return <Navigate replace to={first} />;
  return (
    <PageContainer width="wide">
      <DsAlert title={t("react.user_sections.none_yet", "No sections yet")} tone="neutral">
        <p>
          {t(
            "react.user_sections.none_yet_detail",
            "An administrator has not set up a section for you. Ask them to add one to a project.",
          )}
        </p>
      </DsAlert>
    </PageContainer>
  );
}
