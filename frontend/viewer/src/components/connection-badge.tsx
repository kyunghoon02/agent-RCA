"use client";

import { CircleAlert, CircleDot, FlaskConical, Loader } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useViewerStatus, type ConnectionState } from "@/components/viewer-status";

const PRESENTATION: Record<
  ConnectionState,
  { label: string; tone: "success" | "critical" | "warning" | "neutral"; spin?: boolean }
> = {
  live: { label: "API Live", tone: "success" },
  disconnected: { label: "API Disconnected", tone: "critical" },
  demo: { label: "Demo Data", tone: "warning" },
  connecting: { label: "API Connecting", tone: "neutral", spin: true },
};

const ICONS = {
  live: CircleDot,
  disconnected: CircleAlert,
  demo: FlaskConical,
  connecting: Loader,
} as const;

/**
 * Reachability of the Viewer query API — and nothing else.
 *
 * This must never be read as "the Agent runtime is up". Agent availability is
 * a property of the analysis work queue and is reported on the Incident detail
 * page, not here.
 */
export function ConnectionBadge() {
  const { connection } = useViewerStatus();
  const presentation = PRESENTATION[connection];
  const Icon = ICONS[connection];
  return (
    <Badge tone={presentation.tone} className="px-2 py-1">
      <Icon
        aria-hidden="true"
        className={presentation.spin ? "animate-[spin_2s_linear_infinite]" : undefined}
      />
      <span>{presentation.label}</span>
    </Badge>
  );
}
