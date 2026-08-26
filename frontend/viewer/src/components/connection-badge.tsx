"use client";

import { CircleAlert, CircleDot, FlaskConical, Loader } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useViewerStatus, type ConnectionState } from "@/components/viewer-status";

const PRESENTATION: Record<
  ConnectionState,
  { label: string; tone: "success" | "critical" | "warning" | "neutral"; spin?: boolean }
> = {
  live: { label: "Live", tone: "success" },
  disconnected: { label: "Disconnected", tone: "critical" },
  demo: { label: "Demo Data", tone: "warning" },
  connecting: { label: "Connecting", tone: "neutral", spin: true },
};

const ICONS = {
  live: CircleDot,
  disconnected: CircleAlert,
  demo: FlaskConical,
  connecting: Loader,
} as const;

/** Names the data source in the header so a demo is never read as a live cluster. */
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
