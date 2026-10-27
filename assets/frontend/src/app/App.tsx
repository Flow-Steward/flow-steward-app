import { AppRouter } from "@/app/router";
import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";

export function App() {
  return (
    <>
      <AppRouter />
      <RequestLoadingIndicator />
    </>
  );
}
