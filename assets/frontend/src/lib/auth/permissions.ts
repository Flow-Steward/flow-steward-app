import type { AuthUser } from "@/lib/auth/AuthProvider";

export function hasRole(user: AuthUser | undefined, role: string) {
  return Boolean(user?.roles.some((candidate) => candidate.toLowerCase() === role.toLowerCase()));
}
