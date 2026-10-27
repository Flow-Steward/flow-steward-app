import { useI18n } from "@/lib/i18n/useI18n";
import type { ReactNode } from "react";

import { ArrowUpRight } from "lucide-react";
import { NavLink } from "react-router";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

type PlaceholderLink = {
  href: string;
  label: string;
};

type PagePlaceholderProps = {
  eyebrow: string;
  title: string;
  description: string;
  status: string;
  details?: string[];
  aside?: ReactNode;
  links?: PlaceholderLink[];
};

export function PagePlaceholder({
  eyebrow,
  title,
  description,
  status,
  details = [],
  aside,
  links = [],
}: Readonly<PagePlaceholderProps>) {
  const { t } = useI18n();
  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
      <Card className="border-white/10 bg-slate-950/50 shadow-2xl shadow-slate-950/20">
        <CardHeader>
          <p className="text-xs font-medium uppercase tracking-[0.3em] text-cyan-200">{eyebrow}</p>
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="inline-flex items-center rounded-full border border-cyan-300/30 bg-cyan-400/10 px-3 py-1 text-xs font-medium text-cyan-100">
            {t("react.shared.status", "Status:")} {status}
          </div>

          {details.length > 0 ? (
            <div className="grid gap-3 md:grid-cols-2">
              {details.map((detail) => (
                <div key={detail} className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm text-slate-200">
                  {detail}
                </div>
              ))}
            </div>
          ) : null}

          {links.length > 0 ? (
            <div className="flex flex-wrap gap-3">
              {links.map((link) => (
                <NavLink
                  key={link.href}
                  className="inline-flex items-center gap-2 rounded-full border border-white/10 px-4 py-2 text-sm text-slate-100 transition hover:border-cyan-300 hover:text-white"
                  to={link.href}
                >
                  {link.label}
                  <ArrowUpRight className="h-4 w-4" />
                </NavLink>
              ))}
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card className="border-white/10 bg-white/6">
        <CardHeader>
          <p className="text-xs font-medium uppercase tracking-[0.3em] text-slate-400">{t("react.shared.page_status", "Page status")}</p>
          <CardTitle>{t("react.shared.what_to_do_next", "What to do next")}</CardTitle>
          <CardDescription>
            {t("react.shared.this_screen_is_not_part_of_the_active_route_map", "This screen is not part of the active route map.")} </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm text-slate-300">
          <p>{t("react.shared.use_the_primary_navigation_or_a_canonical_link_to_continue", "Use the primary navigation or a canonical link to continue.")}</p>
          <p>{t("react.shared.old_deep_links_and_removed_action_urls_are_intentionally_unavailable_in_this_release", "Old deep links and removed action URLs are intentionally unavailable in this release.")}</p>
          {aside}
        </CardContent>
      </Card>
    </div>
  );
}
