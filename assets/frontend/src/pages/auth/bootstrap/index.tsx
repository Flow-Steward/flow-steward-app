import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useEffect, useState } from "react";

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { Link, Navigate, useNavigate } from "react-router";

import { OptionalLabel } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/lib/auth/useAuth";
import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { z } from "@/lib/validation/zod";
import { ignoreAsyncResult } from "@/lib/utils/async";

function getBootstrapschema(t: I18nContextValue["t"] = fallbackContext.t) {
  return z
  .object({
    email: z.email(t("react.auth.enter_a_valid_email", "Enter a valid email.")),
    displayName: z.string().trim().optional(),
    password: z.string().min(10, t("auth.common.password_too_short", "Password must be at least 10 characters.")),
    confirmPassword: z.string().min(10, t("react.auth.confirm_password_must_be_at_least_10_characters", "Confirm password must be at least 10 characters.")),
  })
  .refine((values) => values.password === values.confirmPassword, {
    message: t("auth.common.passwords_do_not_match", "Passwords do not match."),
    path: ["confirmPassword"],
  });
}

type BootstrapValues = z.infer<ReturnType<typeof getBootstrapschema>>;

type BootstrapResponse = {
  status: string;
};

function readSetupTokenFromHash(): string {
  return (
    new URLSearchParams(window.location.hash.replace(/^#/, "")).get("setup-token") ?? ""
  ).trim();
}

function clearUrlFragment(): void {
  if (!window.location.hash) {
    return;
  }
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${window.location.search}`,
  );
}

export function BootstrapPage() {
  const { t } = useI18n();
  const auth = useAuth();
  const navigate = useNavigate();
  const [message, setMessage] = useState("");
  // The token arrives in the URL fragment and is wiped from the address bar as soon as it
  // is read. A reload therefore loses it, so the page has to say so and offer a way back in
  // the UI instead of rendering a form whose submit can only 403.
  //
  // AC17-R48: reading it once, during the first render, was not enough. Pasting the printed
  // link into the address bar of a tab that already shows /bootstrap changes only the
  // fragment: the document is not reloaded and this component is not remounted, so the page
  // claimed the token was absent while the address bar still displayed it -- and the
  // fragment was never cleared either. The `hashchange` listener below covers that path, so
  // there is no state in which the URL carries a token and this field sits empty.
  const [setupToken, setSetupToken] = useState(readSetupTokenFromHash);

  useEffect(() => {
    const consumeFragmentToken = () => {
      const token = readSetupTokenFromHash();
      if (!token) {
        return;
      }
      // Set and clear unconditionally: re-pasting the *same* link would otherwise leave the
      // token in the address bar, because an identical state value skips the re-render that
      // the clearing effect below hangs off.
      setSetupToken(token);
      clearUrlFragment();
    };
    window.addEventListener("hashchange", consumeFragmentToken);
    return () => window.removeEventListener("hashchange", consumeFragmentToken);
  }, []);

  useEffect(() => {
    // Covers the token read during the first render, which cannot clear the fragment itself.
    if (!setupToken) {
      return;
    }
    clearUrlFragment();
  }, [setupToken]);

  const form = useForm<BootstrapValues>({
    defaultValues: {
      email: "",
      displayName: "",
      password: "",
      confirmPassword: "",
    },
    resolver: zodResolver(getBootstrapschema(t)),
  });

  if (auth.isAuthenticated) {
    return <Navigate replace to="/dashboard" />;
  }

  if (auth.status === "anonymous" && !auth.session?.bootstrap_required) {
    return <Navigate replace to="/login" />;
  }

  // The page cannot ask whether a token is required without disclosing whether one
  // exists, so it never claims either. It simply offers the field when no token has
  // reached the page, and lets the server decide.
  const setupTokenMissing = !setupToken;

  async function onSubmit(values: BootstrapValues) {
    setMessage("");

    try {
      await fetchJson<BootstrapResponse>("/api/auth/bootstrap", {
        method: "POST",
        ...(setupToken.trim()
          ? {
              headers: {
                "x-platform-setup-token": setupToken.trim(),
              },
            }
          : {}),
        body: JSON.stringify({
          email: values.email.trim(),
          password: values.password,
          display_name: values.displayName?.trim() || undefined,
        }),
      });
      await auth.refresh();
      ignoreAsyncResult(navigate("/login", { replace: true }));
    } catch (error) {
      if (error instanceof HttpError) {
        setMessage(error.message);
        return;
      }
      setMessage(error instanceof Error ? error.message : t("react.auth.unable_to_create_admin_user", "Unable to create admin user."));
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-[radial-gradient(circle_at_top,_rgba(34,197,94,0.18),_transparent_30%),linear-gradient(180deg,_#020617,_#0f172a)] px-6 py-10 text-white">
      <Card className="w-full max-w-3xl border-white/10 bg-slate-950/55 shadow-2xl shadow-slate-950/20">
        <CardHeader>
          <p className="text-xs font-medium uppercase tracking-[0.35em] text-emerald-200">{t("react.auth.first_user_setup", "First user setup")}</p>
          <CardTitle className="mt-2">{t("react.auth.create_the_initial_flow_steward_admin_account", "Create the initial Flow Steward admin account")}</CardTitle>
          <CardDescription>
            {t("react.auth.local_auth_bootstrap_is_active_because_no_users_exist_yet_create_the_first_admin_account_then_return_to_login", "Local auth bootstrap is active because no users exist yet. Create the first admin account, then return to login.")} </CardDescription>
        </CardHeader>

        <form
          className="grid gap-4 px-6 pb-6"
          onSubmit={(event) => {
            ignoreAsyncResult(form.handleSubmit(onSubmit)(event));
          }}
        >
          {setupTokenMissing ? (
            <div
              className="rounded-2xl border border-amber-300/30 bg-amber-300/10 px-4 py-3 text-sm text-amber-100"
              data-testid="bootstrap-setup-token-notice"
            >
              <p>
                {t("react.auth.this_page_was_opened_without_the_one_time_setup_token_if_the_installer_printed_a_link_open_that_link_otherwise", "This page was opened without the one-time setup token. If the installer printed a link, open that link. Otherwise paste the token below — it is the same value the installer stored for you.")} </p>
              <label className="mt-3 block">
                <span className="mb-2 block text-xs uppercase tracking-wide text-amber-200">
                  {t("react.auth.setup_token", "Setup token")} </span>
                <input
                  autoComplete="off"
                  className="w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none ring-0 placeholder:text-slate-500 focus:border-amber-200"
                  data-testid="bootstrap-setup-token-input"
                  onChange={(event) => setSetupToken(event.target.value)}
                  placeholder={t("react.auth.setup_token_2", "setup token")}
                  type="password"
                  value={setupToken}
                />
              </label>
            </div>
          ) : null}

          <label className="block">
            <span className="mb-2 block text-sm text-slate-300">{t("react.auth.admin_email", "Admin email")}</span>
            <input
              {...form.register("email")}
              className="w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none ring-0 placeholder:text-slate-500 focus:border-emerald-300"
              placeholder={t("react.auth.admin_example_com", "admin@example.com")}
              type="email"
            />
            <span className="mt-2 block text-sm text-rose-300">{form.formState.errors.email?.message}</span>
          </label>

          <label className="block">
            <span className="mb-2 block text-sm text-slate-300"><OptionalLabel>{t("react.auth.display_name", "Display name")}</OptionalLabel></span>
            <input
              {...form.register("displayName")}
              className="w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none ring-0 placeholder:text-slate-500 focus:border-emerald-300"
              placeholder={t("react.auth.platform_admin", "Platform Admin")}
              type="text"
            />
          </label>

          <label className="block">
            <span className="mb-2 block text-sm text-slate-300">{t("auth.common.password", "Password")}</span>
            <input
              {...form.register("password")}
              className="w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none ring-0 placeholder:text-slate-500 focus:border-emerald-300"
              placeholder={t("react.auth.at_least_10_characters", "At least 10 characters")}
              type="password"
            />
            <span className="mt-2 block text-sm text-rose-300">{form.formState.errors.password?.message}</span>
          </label>

          <label className="block">
            <span className="mb-2 block text-sm text-slate-300">{t("react.auth.confirm_password", "Confirm password")}</span>
            <input
              {...form.register("confirmPassword")}
              className="w-full rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-white outline-none ring-0 placeholder:text-slate-500 focus:border-emerald-300"
              placeholder={t("react.auth.repeat_password", "Repeat password")}
              type="password"
            />
            <span className="mt-2 block text-sm text-rose-300">
              {form.formState.errors.confirmPassword?.message}
            </span>
          </label>

          {message ? (
            <div className="rounded-2xl border border-rose-300/20 bg-rose-400/10 px-4 py-3 text-sm text-rose-100">
              {message}
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-3">
            <Button disabled={form.formState.isSubmitting} size="lg" type="submit">
              {form.formState.isSubmitting ? t("react.auth.creating_admin", "Creating admin...") : t("react.auth.create_admin_user", "Create admin user")}
            </Button>
            <Button asChild variant="ghost">
              <Link to="/login">{t("auth.common.back_to_login", "Back to login")}</Link>
            </Button>
          </div>
        </form>
      </Card>
    </main>
  );
}
