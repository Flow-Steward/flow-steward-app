import type { PropsWithChildren } from "react";

import { QueryClientProvider } from "@tanstack/react-query";

import { AuthProvider } from "@/lib/auth/AuthProvider";
import { I18nProvider } from "@/lib/i18n/I18nProvider";
import { createQueryClient } from "@/lib/query/client";

const queryClient = createQueryClient();

export function AppProviders({ children }: Readonly<PropsWithChildren>) {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <I18nProvider>{children}</I18nProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
