import { CircleAlert, FlaskConical, History, Scissors } from "lucide-react";
import { cn } from "@/lib/utils";
import type { TruncationFlags } from "@/lib/types";

type Tone = "info" | "warning" | "critical";

const TONE_CLASSES: Record<Tone, string> = {
  info: "border-status-info/40 bg-status-info-surface text-status-info",
  warning: "border-status-warning/40 bg-status-warning-surface text-status-warning",
  critical: "border-status-critical/40 bg-status-critical-surface text-status-critical",
};

export function Notice({
  tone,
  icon: Icon,
  title,
  children,
  className,
  role = "status",
}: {
  tone: Tone;
  icon: typeof CircleAlert;
  title: string;
  children?: React.ReactNode;
  className?: string;
  role?: "status" | "alert";
}) {
  return (
    <div
      role={role}
      className={cn(
        "flex items-start gap-2 rounded-md border px-3 py-2 text-xs",
        TONE_CLASSES[tone],
        className,
      )}
    >
      <Icon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
      <div className="min-w-0">
        <p className="font-medium">{title}</p>
        {children && <div className="mt-0.5 text-foreground/80">{children}</div>}
      </div>
    </div>
  );
}

/** Shown whenever the fixture adapter is active, on every screen. */
export function DemoDataNotice() {
  return (
    <Notice
      tone="warning"
      icon={FlaskConical}
      title="Demo Data — no Viewer API is configured"
    >
      Every Incident, Evidence item and Report below comes from a fixed local
      fixture set. Nothing here reflects a running cluster. Set
      <code className="mx-1 font-mono">NEXT_PUBLIC_VIEWER_API_BASE_URL</code>
      to read a live Viewer API.
    </Notice>
  );
}

/** The request failed and there is no earlier payload to fall back on. */
export function DisconnectedNotice({ error }: { error: Error }) {
  return (
    <Notice tone="critical" icon={CircleAlert} title="Viewer API is unreachable" role="alert">
      {error.message}
    </Notice>
  );
}

/** The request failed but earlier data is still on screen. */
export function StaleDataNotice({
  error,
  lastUpdatedAt,
}: {
  error: Error;
  lastUpdatedAt: number | null;
}) {
  return (
    <Notice tone="warning" icon={History} title="Showing the last successful result" role="alert">
      The most recent refresh failed ({error.message}). These values were read
      {lastUpdatedAt
        ? ` at ${new Date(lastUpdatedAt).toISOString().slice(11, 19)}Z`
        : " earlier"}{" "}
      and may no longer be current.
    </Notice>
  );
}

const TRUNCATION_LABELS: Record<keyof TruncationFlags, string> = {
  evidence: "Evidence",
  contexts: "Frozen Contexts",
  reports: "Reports",
  agent_runs: "Agent runs",
  audit_events: "Audit events",
  timeline: "Timeline events",
};

/** The detail bundle hit the Viewer's bounded limits and is not the whole record. */
export function TruncationNotice({ truncated }: { truncated: TruncationFlags }) {
  const affected = (Object.keys(TRUNCATION_LABELS) as (keyof TruncationFlags)[]).filter(
    (key) => truncated[key],
  );
  if (affected.length === 0) return null;

  return (
    <Notice tone="warning" icon={Scissors} title="This result is truncated">
      The Viewer caps each artifact list, and these reached the cap:{" "}
      {affected.map((key) => TRUNCATION_LABELS[key]).join(", ")}. What is shown is a
      bounded subset, not the complete record.
    </Notice>
  );
}
