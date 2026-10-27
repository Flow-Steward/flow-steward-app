import { type ReactNode } from "react";
import { ExternalLink } from "lucide-react";
import { useI18n } from "@/lib/i18n/useI18n";
import { CatalogIcon, type CatalogIconSize } from "@/components/shared/catalog-browse";
import { mcpCatalogIconUrl } from "./api";

/** The service's icon, served by Flow Steward after it verifies the catalog digest. */
export function McpCatalogIcon({
  itemId,
  hasIcon,
  title,
  size = "card",
}: Readonly<{ itemId: string; hasIcon: boolean; title: string; size?: CatalogIconSize }>) {
  return (
    <CatalogIcon
      kind="mcp"
      name={title}
      size={size}
      src={hasIcon ? mcpCatalogIconUrl(itemId) : ""}
    />
  );
}

export function ExternalAnchor({ href, children }: Readonly<{ href: string; children: ReactNode }>) {
  const { t } = useI18n();
  // `.fs-catalog-source` carries the catalog's external-link face (brand colour,
  // icon size, inline alignment) wherever the link sits.
  return (
    <span className="fs-catalog-source">
      <a href={href} rel="noreferrer" target="_blank">
        {children}
        <ExternalLink aria-hidden="true" />
        <span className="fs-sr-only"> {t("react.extensions.opens_in_a_new_tab", "(opens in a new tab)")}</span>
      </a>
    </span>
  );
}
