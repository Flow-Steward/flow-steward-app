import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";
import { useInRouterContext, useNavigate } from "react-router";

import { ignoreAsyncResult } from "@/lib/utils/async";
import { appRouterPath } from "@/pages/extensions/extension-grid-links";

/** A link to a page of this app inside extension markdown: it opens in place through the router. */
function RoutedMarkdownLink({ children, href }: Readonly<{ children: ReactNode; href: string }>) {
  const navigate = useNavigate();
  const follow = (event: ReactMouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    ignoreAsyncResult(navigate(appRouterPath(href)));
  };
  return <a className="fs-link fs-markdown-link" href={href} onClick={follow}>{children}</a>;
}

export function MarkdownAppLink({ children, href }: Readonly<{ children: ReactNode; href: string }>) {
  return useInRouterContext()
    ? <RoutedMarkdownLink href={href}>{children}</RoutedMarkdownLink>
    : <a className="fs-link fs-markdown-link" href={href}>{children}</a>;
}
