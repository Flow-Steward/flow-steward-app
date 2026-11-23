import { useQuery } from "@tanstack/react-query";
import { Navigate } from "react-router";

import { buildProjectSectionNavigation, firstSectionPath } from "@/components/layout/project-section-navigation";
import { DsAlert, PageContainer } from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { useI18n } from "@/lib/i18n/useI18n";
import { queryKeys } from "@/lib/query/keys";

import { fetchOperatorSections } from "./api";

/** An operator's or viewer's landing page: the first section, or why there is none. */
export function OperatorSectionsHomePage() {
  const { t } = useI18n();
  const sections = useQuery({
    queryKey: queryKeys.operatorSections,
    queryFn: fetchOperatorSections,
    staleTime: 60_000,
  });
  if (sections.isLoading) {
    return <PageLoadingState label={t("react.operator.loading_menu", "Finding your sections")} />;
  }
  const first = firstSectionPath(buildProjectSectionNavigation(sections.data ?? [], { singleLabel: "" }));
  if (first) return <Navigate replace to={first} />;
  return (
    <PageContainer width="wide">
      <DsAlert title={t("react.operator.none_yet", "No sections yet")} tone="neutral">
        <p>
          {t(
            "react.operator.none_yet_detail",
            "No workflow is published for you yet. An administrator publishes one in its Workflow Settings and links it to your project.",
          )}
        </p>
      </DsAlert>
    </PageContainer>
  );
}
