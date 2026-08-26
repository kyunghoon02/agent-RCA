"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Siren, ShieldCheck } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { SkinToggle } from "@/components/skin-toggle";
import { ThemeToggle } from "@/components/theme-toggle";
import { cn } from "@/lib/utils";

interface RailItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Sub-routes that should keep this item active. */
  match: (pathname: string) => boolean;
}

const ITEMS: RailItem[] = [
  {
    href: "/incidents",
    label: "Incidents",
    icon: Siren,
    match: (pathname) => pathname.startsWith("/incidents"),
  },
];

/**
 * Persistent left rail.
 *
 * Holds the app mark, section navigation, and the two global display controls.
 * It renders on the dark `chrome` surface in every theme, which is how Foundry
 * separates application chrome from the data surface.
 */
export function AppRail() {
  const pathname = usePathname();

  return (
    <nav
      aria-label="Sections"
      className="sticky top-0 z-50 flex h-dvh w-12 shrink-0 flex-col items-center gap-1 border-r border-chrome-border bg-chrome py-2 text-chrome-foreground"
    >
      <Link
        href="/incidents"
        aria-label="Agent RCA — Incident Viewer"
        className="mb-1 flex size-8 items-center justify-center rounded text-chrome-foreground hover:bg-chrome-active"
      >
        <ShieldCheck className="size-4.5" aria-hidden="true" />
      </Link>

      <span aria-hidden="true" className="mb-1 h-px w-6 bg-chrome-border" />

      {ITEMS.map((item) => {
        const active = item.match(pathname);
        const Icon = item.icon;
        return (
          <Link
            key={item.href}
            href={item.href}
            title={item.label}
            aria-label={item.label}
            aria-current={active ? "page" : undefined}
            className={cn(
              "relative flex size-8 items-center justify-center rounded hover:bg-chrome-active",
              active && "bg-chrome-active text-foreground",
            )}
          >
            {/* Active state is an edge marker plus contrast, not colour alone. */}
            {active && (
              <span
                aria-hidden="true"
                className="absolute inset-y-1 left-[-6px] w-0.5 rounded-full bg-ring"
              />
            )}
            <Icon className="size-4" aria-hidden="true" />
          </Link>
        );
      })}

      <div className="mt-auto flex flex-col items-center gap-0.5">
        <SkinToggle className="text-chrome-foreground hover:bg-chrome-active hover:text-chrome-foreground" />
        <ThemeToggle className="text-chrome-foreground hover:bg-chrome-active hover:text-chrome-foreground" />
      </div>
    </nav>
  );
}
