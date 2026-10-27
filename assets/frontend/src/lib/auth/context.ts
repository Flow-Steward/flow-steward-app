import { createContext } from "react";

import type { AuthSession } from "@/lib/auth/AuthProvider";

export type AuthContextValue = {
  error: string | null;
  isAuthenticated: boolean;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  switchAccount: (accountId: string) => Promise<void>;
  session: AuthSession | null;
  status: "loading" | "authenticated" | "anonymous";
};

export const AuthContext = createContext<AuthContextValue | null>(null);
