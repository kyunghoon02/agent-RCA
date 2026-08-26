"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronRight, ExternalLink } from "lucide-react";
import { ConnectionBadge } from "@/components/connection-badge";
import { useViewerStatus } from "@/components/viewer-status";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { deepLinks, environmentLabel } from "@/lib/config";
import { formatTimestamp } from "@/lib/format";

interface Crumb {
  label: string;
  href?: string;
}

/** Route-derived trail. The rail says which section; this says where within it. */
function crumbsFor(pathname: string): Crumb[] {
  const segments = pathname.split("/").filter(Boolean);
  if (segments[0] !== "incidents") return [{ label: "Agent RCA" }];
  const crumbs: Crumb[] = [
    { label: "Incidents", href: segments.length > 1 ? "/incidents" : undefined },
  ];
  if (segments[1]) crumbs.push({ label: decodeURIComponent(segments[1]) });
  return crumbs;
}

export function AppHeader() {
  const { lastUpdatedAt } = useViewerStatus();
  const pathname = usePathname();
  const crumbs = crumbsFor(pathname);
  const links = deepLinks();

  return (
    <header className="sticky top-0 z-40 border-b border-chrome-border bg-chrome text-chrome-foreground">
      <div className="flex h-10 items-center gap-2 px-3">
        <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1">
          <ol className="flex min-w-0 items-center gap-1">
            {crumbs.map((crumb, index) => (
              <li key={crumb.label} className="flex min-w-0 items-center gap-1">
                {index > 0 && (
                  <ChevronRight
                    className="size-3 shrink-0 opacity-50"
                    aria-hidden="true"
                  />
                )}
                {crumb.href ? (
                  <Link
                    href={crumb.href}
                    className="truncate rounded text-xs hover:underline"
                  >
                    {crumb.label}
                  </Link>
                ) : (
                  <span
                    aria-current={index === crumbs.length - 1 ? "page" : undefined}
                    className="truncate font-mono text-xs font-medium text-white/90"
                  >
                    {crumb.label}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </nav>

        <Separator orientation="vertical" className="h-4 bg-chrome-border" />

        <Badge tone="outline" className="border-chrome-border uppercase">
          {environmentLabel()}
        </Badge>
        <Badge
          tone="outline"
          className="border-chrome-border"
          title="This Viewer issues read requests only."
        >
          Read-only
        </Badge>

        <div className="ml-auto flex items-center gap-2">
          <span className="tabular hidden text-[11px] opacity-70 sm:inline">
            Last refresh{" "}
            {lastUpdatedAt ? formatTimestamp(new Date(lastUpdatedAt).toISOString()) : "—"}
          </span>

          <ConnectionBadge />

          {links.length > 0 && (
            <>
              <Separator orientation="vertical" className="h-4 bg-chrome-border" />
              <nav aria-label="External tools" className="flex items-center gap-0.5">
                {links.map((link) => (
                  <a
                    key={link.label}
                    href={link.href}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="inline-flex items-center gap-1 rounded px-1.5 py-1 text-[11px] hover:bg-chrome-active"
                  >
                    {link.label}
                    <ExternalLink className="size-3" aria-hidden="true" />
                  </a>
                ))}
              </nav>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
