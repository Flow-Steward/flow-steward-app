import { WorkflowsDetailPage } from "@/pages/workflows/detail";

export function WorkflowsBuilderPage() {
  return (
    <div className="fixed inset-0 z-[var(--fs-z-navigation)] overflow-hidden bg-[var(--fs-bg-canvas)] text-[var(--fs-text-primary)]">
      <WorkflowsDetailPage embedded surface="builder" />
    </div>
  );
}
