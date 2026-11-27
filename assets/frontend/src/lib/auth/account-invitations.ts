import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

/**
 * What an invitation link invites the signed-in person to (ADR 0085). The link's
 * token is the authority: whoever opens it while signed in answers it, once.
 */
export type InvitationPreview = {
  accountName: string;
  role: string;
  invitedBy: string;
  expiresAt: string;
};

export type InvitationDecision = {
  status: string;
  accountId: string;
  accountName: string;
  role: string;
  /** Accepting moved the person out of the account their session was in: sign in again. */
  signInAgain: boolean;
};

const INVITATION_PATH = /^\/invitations\/([^/?#]+)\/?$/;

/** The token of an invitation link path (`/invitations/<token>`), or "". */
export function invitationTokenFromPath(path: string | null | undefined): string {
  const match = INVITATION_PATH.exec(String(path || "").split("?")[0] || "");
  if (!match) return "";
  try {
    return decodeURIComponent(match[1]).trim();
  } catch {
    return "";
  }
}

function text(value: unknown): string {
  return firstStringFromUnknownValue([value, ""]).trim();
}

/** Read the invitation the link names. Tokens go in the body, never in an API URL. */
export async function previewInvitation(token: string): Promise<InvitationPreview> {
  const payload = await fetchJson<Record<string, unknown>>("/api/auth/invitations/preview", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
  return {
    accountName: text(payload?.account_name),
    role: text(payload?.role),
    invitedBy: text(payload?.invited_by),
    expiresAt: text(payload?.expires_at),
  };
}

async function decide(path: "accept" | "decline", body: Record<string, string>): Promise<InvitationDecision> {
  const payload = await fetchJson<Record<string, unknown>>(`/api/auth/invitations/${path}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  return {
    status: text(payload?.status),
    accountId: text(payload?.account_id),
    accountName: text(payload?.account_name),
    role: text(payload?.role),
    signInAgain: payload?.sign_in_again === true,
  };
}

/** Join with the role the person was shown; refused when the invitation changed. */
export function acceptInvitation(token: string, role: string): Promise<InvitationDecision> {
  return decide("accept", { token, role });
}

/** Decline the invitation; no membership is created. */
export function declineInvitation(token: string): Promise<InvitationDecision> {
  return decide("decline", { token });
}
