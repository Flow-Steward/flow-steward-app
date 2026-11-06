import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "@/design-system/01_tokens/themes.css";
import "@/components/shared/design-system.css";
import "./report-viewer.css";

import { ReportViewerApp } from "./report-viewer-app";

const container = document.getElementById("root");
if (container) {
  createRoot(container).render(
    <StrictMode>
      <ReportViewerApp />
    </StrictMode>,
  );
}
