"use client";

import { Activity, CircleCheck, CircleX, ScanSearch } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Card } from "@/components/ui/card";
import type { IncidentStatus, IncidentSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

const IN_FLIGHT: IncidentStatus[] = ["RECEIVED", "COLLECTING", "LOCALIZING", "ANALYZING"];

interface SummaryCard {
  key: string;
  label: string;
  icon: LucideIcon;
  statuses: IncidentStatus[];
  accent: string;
}

const CARDS: SummaryCard[] = [
  {
    key: "active",
    label: "Active Incidents",
    icon: Activity,
    statuses: IN_FLIGHT,
    accent: "text-status-running",
  },
  {
    key: "analyzing",
    label: "Analyzing",
    icon: ScanSearch,
    statuses: ["ANALYZING"],
    accent: "text-status-running",
  },
  {
    key: "reported",
    label: "Reported",
    icon: CircleCheck,
    statuses: ["REPORTED"],
    accent: "text-status-success",
  },
  {
    key: "failed",
    label: "Failed",
    icon: CircleX,
    statuses: ["FAILED"],
    accent: "text-status-critical",
  },
];

/**
 * Counts are scoped to the Incidents currently loaded, and say so.
 *
 * The list API returns one bounded page and no totals, so a cluster-wide count
 * would have to be invented. Each card instead doubles as a filter shortcut,
 * which asks the server the question properly.
 */
export function SummaryCards({
  items,
  activeStatuses,
  onSelectStatuses,
}: {
  items: IncidentSummary[];
  activeStatuses: IncidentStatus[];
  onSelectStatuses: (statuses: IncidentStatus[]) => void;
}) {
  const sameSelection = (statuses: IncidentStatus[]) =>
    statuses.length === activeStatuses.length &&
    statuses.every((status) => activeStatuses.includes(status));

  return (
    <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
      {CARDS.map((card) => {
        const count = items.filter((item) => card.statuses.includes(item.status)).length;
        const isActive = sameSelection(card.statuses);
        const Icon = card.icon;
        return (
          <Card key={card.key} className={cn("p-0", isActive && "ring-1 ring-ring")}>
            <button
              type="button"
              aria-pressed={isActive}
              onClick={() => onSelectStatuses(isActive ? [] : card.statuses)}
              className="flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left hover:bg-accent"
            >
              <Icon className={cn("size-4 shrink-0", card.accent)} aria-hidden="true" />
              <span className="min-w-0">
                <span className="block text-[11px] uppercase tracking-wide text-muted-foreground">
                  {card.label}
                </span>
                <span className="tabular text-lg font-semibold leading-tight">{count}</span>
                <span className="ml-1.5 text-[11px] text-muted-foreground">
                  of {items.length} loaded
                </span>
              </span>
            </button>
          </Card>
        );
      })}
    </div>
  );
}
