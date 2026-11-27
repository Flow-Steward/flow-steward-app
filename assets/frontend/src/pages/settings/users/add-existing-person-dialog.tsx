import { useEffect, useState } from "react";

import { useQuery } from "@tanstack/react-query";

import { DsAlert, DsDialog, DsRadio, RequiredLabel } from "@/components/shared/design-system";
import { DsSearchInput } from "@/components/ui/search-input";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  addAccountMember,
  inviteAccountMember,
  searchAccountMemberCandidates,
  type AccountMemberCandidate,
} from "@/pages/settings/shared/api";

import { UserRoleField } from "./user-dialogs";

/** Characters typed before the search runs; one letter would list everyone. */
const MIN_QUERY_LENGTH = 2;
const SEARCH_DELAY_MS = 250;

/** A full email address: only such a query can be sent as an invitation. */
function looksLikeEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
}

function useSettledValue(value: string, delayMs: number): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}

/**
 * Add a person to the account the administrator acts in (ADR 0085). People of the
 * accounts the administrator manages are found and added directly; anyone else is
 * invited by their exact email address and joins only if they accept.
 */
export function AddExistingPersonDialog({
  onAdded,
  onClose,
  onInvited,
  open,
}: Readonly<{
  onAdded: (person: AccountMemberCandidate) => void;
  onClose: () => void;
  /** The invitation was sent; ``link`` is the one-time link when no email sent it. */
  onInvited: (message: string, link: string) => void;
  open: boolean;
}>) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [person, setPerson] = useState<AccountMemberCandidate | null>(null);
  const [role, setRole] = useState("viewer");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const settledQuery = useSettledValue(query.trim(), SEARCH_DELAY_MS);
  const searching = settledQuery.length >= MIN_QUERY_LENGTH;
  const candidates = useQuery({
    queryKey: ["settings", "users", "candidates", settledQuery],
    queryFn: () => searchAccountMemberCandidates(settledQuery),
    enabled: open && searching,
  });

  useEffect(() => {
    if (!open) {
      setQuery("");
      setPerson(null);
      setRole("viewer");
      setError("");
    }
  }, [open]);

  const people = candidates.data || [];
  const chosen = people.length === 1 ? people[0] : person;
  // Nobody of the managed accounts matched a full email address: invite instead.
  const inviting = !chosen && searching && candidates.isSuccess && !people.length && looksLikeEmail(query);

  function close() {
    if (!saving) onClose();
  }

  async function submit(event: React.SubmitEvent<HTMLFormElement>, target: AccountMemberCandidate | null) {
    event.preventDefault();
    if (!target && !inviting) {
      setError(t("react.settings.choose_a_person", "Choose a person."));
      return;
    }
    setSaving(true);
    setError("");
    try {
      if (target) {
        await addAccountMember(target.id, role);
        onAdded(target);
      } else {
        // The server answers the same whether or not the person exists.
        const result = await inviteAccountMember(query, role);
        onInvited(
          result.message || t("react.settings.if_this_person_exists_they_will_get_an_invitation", "If this person exists, they will get an invitation."),
          String(result.invitation_link || "").trim(),
        );
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("react.settings.the_person_could_not_be_added", "The person could not be added."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DsDialog
      busy={saving}
      closeLabel={t("react.settings.close_add_existing_person_dialog", "Close add existing person dialog")}
      description={t("react.settings.add_or_invite_person_description", "Find people of the companies you manage by name or email. Anyone else is invited by their full email address and joins after they accept.")}
      eyebrow={t("react.settings.users_access", "Users & access")}
      footer={
        <>
          <button className="fs-button" disabled={saving} onClick={close} type="button">
            {t("common.cancel", "Cancel")} </button>
          <Button
            busy={saving}
            busyLabel={t("react.settings.saving", "Saving…")}
            disabled={!chosen && !inviting}
            form="add-existing-person"
            type="submit"
            variant="primary"
          >
            {inviting
              ? t("react.settings.send_invitation", "Send invitation")
              : t("react.settings.add_to_this_company", "Add to this company")}
          </Button>
        </>
      }
      onClose={close}
      open={open}
      title={t("react.settings.add_an_existing_person", "Add an existing person")}
    >
      <form
        aria-label={t("react.settings.add_an_existing_person", "Add an existing person")}
        autoComplete="off"
        className="fs-dialog-form"
        id="add-existing-person"
        noValidate
        onSubmit={(event) => ignoreAsyncResult(submit(event, chosen))}
      >
        <div className="fs-field">
          <RequiredLabel>{t("react.settings.person", "Person")}</RequiredLabel>
          <DsSearchInput
            aria-label={t("react.settings.find_a_person", "Find a person")}
            onValueChange={(value) => { setQuery(value); setPerson(null); }}
            placeholder={t("react.settings.name_or_email", "Name or email address")}
            value={query}
          />
          {searching && candidates.isSuccess && !people.length ? (
            <span className="fs-field-helper">
              {inviting
                ? t("react.settings.invite_by_email_helper", "No one of your companies has this address. Send an invitation: they join after they accept.")
                : t("react.settings.no_one_found_type_full_email_to_invite", "No one found. Type their full email address to invite them, or add them as a new user.")}{" "}
            </span>
          ) : null}
          {people.length === 1 ? (
            // One match is the person: nothing to choose between.
            <span className="fs-user-cell" data-testid="existing-person-match">
              <span className="fs-user-copy">
                <strong>{people[0].display_name || people[0].email}</strong>
                <span>{people[0].email}</span>
              </span>
            </span>
          ) : null}
          {people.length > 1 ? (
            <fieldset className="fs-field">
              <legend className="fs-field-label">{t("react.settings.matching_people", "Matching people")}</legend>
              <div className="fs-role-grid">
                {people.map((candidate) => (
                  <DsRadio
                    checked={person?.id === candidate.id}
                    key={candidate.id}
                    name="existing-person"
                    onChange={() => setPerson(candidate)}
                    value={candidate.id}
                  >
                    <strong>{candidate.display_name || candidate.email}</strong>
                    <small>{candidate.email}</small>
                  </DsRadio>
                ))}
              </div>
            </fieldset>
          ) : null}
        </div>
        <fieldset className="fs-field">
          <legend className="fs-field-label fs-required-label">
            {t("react.settings.role_in_this_company", "Role in this company")}<span aria-hidden="true" className="fs-required">*</span>
          </legend>
          <UserRoleField name="existing-person-role" onChange={setRole} role={role} />
        </fieldset>
        {error ? (
          <DsAlert tone="danger" title={t("react.settings.the_person_could_not_be_added", "The person could not be added.")}>
            {error}
          </DsAlert>
        ) : null}
      </form>
    </DsDialog>
  );
}
