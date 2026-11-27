import { useQuery } from "@tanstack/react-query";

import { fetchJson } from "@/lib/http/client";

/**
 * The owner of the acting company (ADR 0085, "Company owner"): the person who keeps
 * the ultimate rights there. The owner offers ownership to another active
 * administrator, who accepts or declines it.
 *
 * - `GET /api/auth/users/owner` → {@link AccountOwnerState}
 * - `POST /api/auth/users/owner/transfer` `{ user_id }`: the owner offers it
 * - `POST /api/auth/users/owner/transfer/cancel`: the owner withdraws the offer
 * - `POST /api/auth/users/owner/transfer/accept|decline` `{ transfer_id }`: the recipient answers
 */

export const ACCOUNT_OWNER_QUERY_KEY = ["settings", "users", "owner"] as const;

export type OwnerTransfer = {
  id: string;
  from_user_id: string;
  from_name: string;
  to_user_id: string;
  to_name: string;
  expires_at: string | null;
};

export type AccountOwnerState = {
  owner_user_id: string;
  owner_name: string;
  /** The signed-in person owns the acting company. */
  is_owner: boolean;
  pending_transfer: OwnerTransfer | null;
  /** The open offer is made to the signed-in person. */
  transfer_is_for_me: boolean;
};

export async function fetchAccountOwner(): Promise<AccountOwnerState> {
  const payload = await fetchJson<Partial<AccountOwnerState>>("/api/auth/users/owner");
  return {
    owner_user_id: String(payload.owner_user_id || ""),
    owner_name: String(payload.owner_name || ""),
    is_owner: Boolean(payload.is_owner),
    pending_transfer: payload.pending_transfer?.id ? payload.pending_transfer : null,
    transfer_is_for_me: Boolean(payload.transfer_is_for_me),
  };
}

/** The owner of the acting company and the open offer. */
export function useAccountOwner(enabled: boolean) {
  return useQuery({ queryKey: ACCOUNT_OWNER_QUERY_KEY, queryFn: fetchAccountOwner, enabled });
}

export async function offerOwnershipTransfer(userId: string) {
  return fetchJson<{ status: string; transfer: OwnerTransfer }>("/api/auth/users/owner/transfer", {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export async function cancelOwnershipTransfer() {
  return fetchJson<{ status: string }>("/api/auth/users/owner/transfer/cancel", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function answerOwnershipTransfer(transferId: string, accept: boolean) {
  return fetchJson<{ status: string }>(
    `/api/auth/users/owner/transfer/${accept ? "accept" : "decline"}`,
    { method: "POST", body: JSON.stringify({ transfer_id: transferId }) },
  );
}
