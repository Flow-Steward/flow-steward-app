import { useEffect, useState, type ReactNode } from "react";

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router";

import { DsAlert, DsCard, DsPageHeader, PageContainer } from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { Button } from "@/components/ui/button";
import {
  acceptInvitation,
  declineInvitation,
  previewInvitation,
  type InvitationPreview,
} from "@/lib/auth/account-invitations";
import { RequireAuth } from "@/lib/auth/guards";
import { useAuth } from "@/lib/auth/useAuth";
import { isHttpError } from "@/lib/http/errors";
import type { I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { formatDate } from "@/lib/time/date-time-format";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { roleLabel } from "@/pages/settings/shared/oauth-copy";

import "@/pages/settings/shared/settings.css";

type Translate = I18nContextValue["t"];

function InvitationShell({ children, description, title }: Readonly<{ children: ReactNode; description: string; title: string }>) {
  const { t } = useI18n();
  return (
    <main className="min-h-screen px-6 py-10">
      <PageContainer width="standard">
        <div className="fs-form-block fs-panel-stack">
          <DsPageHeader description={description} eyebrow={t("react.invitations.eyebrow", "Invitation")} title={title} />
          {children}
        </div>
      </PageContainer>
    </main>
  );
}

function invalidNotice(error: unknown, t: Translate): { title: string; copy: string } {
  if (isHttpError(error) && error.status === 409) {
    return {
      title: t("react.invitations.changed_title", "This invitation has changed"),
      copy: t("react.invitations.changed_copy", "The company changed the invitation. Open the newest link you received."),
    };
  }
  if (isHttpError(error) && (error.status === 404 || error.status === 400 || error.status === 422)) {
    return {
      title: t("react.invitations.invalid_title", "This invitation is no longer valid"),
      copy: t("react.invitations.invalid_copy", "It was answered, cancelled or has expired. Ask the company's administrator for a new one."),
    };
  }
  return {
    title: t("react.invitations.failed_title", "The invitation could not be opened"),
    copy: error instanceof Error && error.message ? error.message : t("react.invitations.failed_copy", "Check the connection and try again."),
  };
}

function InvitationAnswer({ invitation, token }: Readonly<{ invitation: InvitationPreview; token: string }>) {
  const { t } = useI18n();
  const auth = useAuth();
  const navigate = useNavigate();
  const [pending, setPending] = useState<"accept" | "decline" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [declined, setDeclined] = useState(false);
  const company = invitation.accountName;
  const role = roleLabel(invitation.role, t);

  async function answer(accept: boolean) {
    if (pending) return;
    setPending(accept ? "accept" : "decline");
    setError(null);
    try {
      if (!accept) {
        await declineInvitation(token);
        setDeclined(true);
        return;
      }
      const joined = await acceptInvitation(token, invitation.role);
      if (joined.signInAgain) {
        // Joining moved the person out of the account this session was in, so the
        // session ended: say so on the sign-in page instead of failing later.
        const message = t("react.invitations.sign_in_again", "You joined {company}. Sign in again to open it.", { company });
        try {
          await auth.refresh();
        } catch {
          // The session is gone; the sign-in page follows either way.
        }
        await navigate(`/login?flash=${encodeURIComponent(message)}`, { replace: true });
        return;
      }
      try {
        // Land in the company just joined; a session pinned to its account by an
        // external sign-in stays where it is.
        await auth.switchAccount(joined.accountId);
      } catch {
        await auth.refresh();
      }
      await navigate("/", { replace: true });
    } catch (caught) {
      setError(caught);
    } finally {
      setPending(null);
    }
  }

  if (declined) {
    return (
      <InvitationShell
        description={t("react.invitations.declined_description", "You did not join {company}.", { company })}
        title={t("react.invitations.declined_title", "Invitation declined")}
      >
        <div className="fs-footer-actions">
          <Link className="fs-button" to="/">{t("react.invitations.continue", "Continue")}</Link>
        </div>
      </InvitationShell>
    );
  }

  const notice = error ? invalidNotice(error, t) : null;
  return (
    <InvitationShell
      description={t("react.invitations.description", "You join only if you accept. You can decline instead.")}
      title={t("react.invitations.title", "Join {company}?", { company })}
    >
      <DsCard title={t("react.invitations.details_title", "The invitation")}>
        <dl className="fs-field">
          <dt className="fs-field-label">{t("react.invitations.company", "Company")}</dt>
          <dd>{company}</dd>
          <dt className="fs-field-label">{t("react.invitations.role", "Your role there")}</dt>
          <dd>{role}</dd>
          {invitation.invitedBy ? (
            <>
              <dt className="fs-field-label">{t("react.invitations.invited_by", "Invited by")}</dt>
              <dd>{invitation.invitedBy}</dd>
            </>
          ) : null}
          {invitation.expiresAt ? (
            <>
              <dt className="fs-field-label">{t("react.invitations.valid_until", "Valid until")}</dt>
              <dd>{formatDate(invitation.expiresAt)}</dd>
            </>
          ) : null}
        </dl>
      </DsCard>
      {notice ? <DsAlert title={notice.title} tone="danger">{notice.copy}</DsAlert> : null}
      <div className="fs-footer-actions">
        <Button busy={pending === "decline"} busyLabel={t("react.oauth.sending", "Sending…")} disabled={Boolean(pending)} onClick={() => ignoreAsyncResult(answer(false))} type="button" variant="secondary">
          {t("react.invitations.decline", "Decline")}
        </Button>
        <Button busy={pending === "accept"} busyLabel={t("react.oauth.sending", "Sending…")} disabled={Boolean(pending)} onClick={() => ignoreAsyncResult(answer(true))} type="button" variant="primary">
          {t("react.invitations.accept", "Join {company} as {role}", { company, role })}
        </Button>
      </div>
    </InvitationShell>
  );
}

function InvitationRequest({ token }: Readonly<{ token: string }>) {
  const { t } = useI18n();
  const query = useQuery({
    queryKey: ["auth", "invitation", token],
    queryFn: () => previewInvitation(token),
    retry: false,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });
  if (query.isLoading) {
    return <PageLoadingState label={t("react.invitations.loading", "Opening the invitation")} standalone />;
  }
  if (query.isError || !query.data) {
    const notice = invalidNotice(query.error, t);
    return (
      <InvitationShell description={notice.copy} title={notice.title}>
        <div className="fs-footer-actions">
          <Link className="fs-button" to="/">{t("react.invitations.continue", "Continue")}</Link>
        </div>
      </InvitationShell>
    );
  }
  return <InvitationAnswer invitation={query.data} token={token} />;
}

/**
 * The page of an invitation link (ADR 0085): the signed-in person sees the company,
 * the role and who invited them, and joins or declines. Signing in comes first; a
 * person with a role nowhere signs in only to answer this link.
 */
export function InvitationPage() {
  const { token = "" } = useParams();

  useEffect(() => {
    // The page carries a one-time token; keep it out of referrers.
    const meta = document.createElement("meta");
    meta.name = "referrer";
    meta.content = "no-referrer";
    document.head.appendChild(meta);
    return () => meta.remove();
  }, []);

  return (
    <RequireAuth>
      <InvitationRequest token={token.trim()} />
    </RequireAuth>
  );
}
