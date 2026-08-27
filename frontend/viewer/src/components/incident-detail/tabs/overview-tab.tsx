"use client";

import {
  Database,
  FileText,
  GitBranch,
  Layers,
  MinusCircle,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { CollectorStatusBadge } from "@/components/status";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { collectorRows, describeCollector } from "@/lib/collectors";
import { cn } from "@/lib/utils";
import { formatTimestamp } from "@/lib/format";
import type { CollectorStatus, IncidentDetail } from "@/lib/types";

function CountCard({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: LucideIcon;
  label: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <Card>
      <CardContent className="flex items-center gap-3 px-3 py-3">
        <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="min-w-0">
          <p className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</p>
          <p className="tabular text-lg font-semibold leading-tight">{value}</p>
          {hint && <p className="text-[11px] text-muted-foreground">{hint}</p>}
        </div>
      </CardContent>
    </Card>
  );
}

export function OverviewTab({ detail }: { detail: IncidentDetail }) {
  const pathCount = detail.contexts.reduce(
    (total, context) => total + context.state_paths.length,
    0,
  );
  const report = detail.reports.at(0);
  const rows = collectorRows(detail.incident.collector_statuses);

  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <CountCard icon={Database} label="Evidence" value={detail.evidence.length} />
        <CountCard icon={Layers} label="Frozen Contexts" value={detail.contexts.length} />
        <CountCard
          icon={GitBranch}
          label="StateGraph paths"
          value={pathCount}
          hint={detail.contexts.length === 0 ? "no Context frozen" : undefined}
        />
        <CountCard
          icon={FileText}
          label="RCA Report"
          value={report ? "1" : "none"}
          hint={report ? `${report.report.status} · ${report.report.path} path` : "not generated"}
        />
      </div>

      <ProviderResults rows={rows} />

      <Card>
        <CardHeader className="pb-2">
          <CardTitle>Alert</CardTitle>
        </CardHeader>
        <CardContent className="pt-0">
          <dl className="grid gap-x-6 gap-y-1.5 text-xs sm:grid-cols-2">
            <Row term="Alert name" value={detail.incident.alert.name} />
            <Row term="Fingerprint" value={detail.incident.alert.fingerprint} />
            <Row term="Deduplication key" value={detail.incident.deduplication_key} />
            <Row term="Source" value={detail.incident.source} />
            <Row
              term="Baseline window"
              value={`${formatTimestamp(detail.incident.window.baseline_start)} → ${formatTimestamp(detail.incident.window.incident_start)}`}
            />
            <Row
              term="Incident window"
              value={`${formatTimestamp(detail.incident.window.incident_start)} → ${
                detail.incident.window.incident_end
                  ? formatTimestamp(detail.incident.window.incident_end)
                  : "open"
              }`}
            />
          </dl>

          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <LabelledMap title="Labels" entries={detail.incident.alert.labels} />
            <LabelledMap title="Annotations" entries={detail.incident.alert.annotations} />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function Row({ term, value }: { term: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="min-w-32 shrink-0 text-muted-foreground">{term}</dt>
      <dd className="break-all font-mono">{value}</dd>
    </div>
  );
}

function LabelledMap({
  title,
  entries,
}: {
  title: string;
  entries: Record<string, string>;
}) {
  const keys = Object.keys(entries).sort();
  return (
    <div>
      <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </p>
      {keys.length === 0 ? (
        <p className="text-xs text-muted-foreground">None recorded.</p>
      ) : (
        <dl className="flex flex-col gap-0.5 rounded border border-border bg-surface-sunken p-2">
          {keys.map((key) => (
            <div key={key} className="flex items-baseline gap-2 text-[11px]">
              <dt className="shrink-0 font-mono text-muted-foreground">{key}</dt>
              <dd className="break-all font-mono">{entries[key]}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}


type ProviderBucket = "succeeded" | "partial" | "failed" | "unconfigured";

const BUCKET_META: Record<
  ProviderBucket,
  { label: string; tone: string; surface: string }
> = {
  succeeded: {
    label: "Succeeded",
    tone: "text-status-success",
    surface: "border-status-success/40",
  },
  partial: {
    label: "Partial",
    tone: "text-status-warning",
    surface: "border-status-warning/40",
  },
  failed: {
    label: "Failed",
    tone: "text-status-critical",
    surface: "border-status-critical/40",
  },
  unconfigured: {
    label: "Not configured",
    tone: "text-muted-foreground",
    surface: "border-border",
  },
};

function bucketFor(status: CollectorStatus | null): ProviderBucket {
  if (!status) return "unconfigured";
  if (status.status === "SUCCEEDED") return "succeeded";
  if (status.status === "PARTIAL") return "partial";
  if (status.status === "SKIPPED" || status.status === "PENDING") return "unconfigured";
  if (status.status === "RUNNING") return "partial";
  return "failed";
}

/**
 * Providers grouped by outcome.
 *
 * "Not configured" deliberately renders as a muted one-line summary rather than
 * a full row per Provider: a Provider that was never wired up is not a failure
 * and must not compete for attention with one that broke.
 */
function ProviderResults({
  rows,
}: {
  rows: { collector: string; status: CollectorStatus | null }[];
}) {
  const buckets: Record<ProviderBucket, typeof rows> = {
    succeeded: [],
    partial: [],
    failed: [],
    unconfigured: [],
  };
  for (const row of rows) buckets[bucketFor(row.status)].push(row);

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle>Provider results</CardTitle>
        <p className="text-xs text-muted-foreground">
          Grouped by outcome. Providers this Incident never ran are listed as not
          configured rather than hidden.
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 pt-0">
        {(["failed", "partial", "succeeded"] as ProviderBucket[]).map((bucket) => {
          const entries = buckets[bucket];
          if (entries.length === 0) return null;
          const meta = BUCKET_META[bucket];
          return (
            <section key={bucket} className={cn("rounded border px-2.5 py-2", meta.surface)}>
              <h3
                className={cn(
                  "text-[10px] font-semibold uppercase tracking-wider",
                  meta.tone,
                )}
              >
                {meta.label} ({entries.length})
              </h3>
              <ul className="mt-1 flex flex-col gap-1">
                {entries.map(({ collector, status }) => {
                  const descriptor = describeCollector(collector);
                  return (
                    <li key={collector} className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <span className="min-w-40 text-xs font-medium">{descriptor.label}</span>
                      {status && <CollectorStatusBadge status={status.status} />}
                      {status && (
                        <span className="tabular text-[10px] text-muted-foreground">
                          {status.attempts} attempt{status.attempts === 1 ? "" : "s"}
                          {status.ended_at ? ` · ${formatTimestamp(status.ended_at)}` : ""}
                        </span>
                      )}
                      {status?.error && (
                        <span className="font-mono text-[10px] text-status-critical">
                          {status.error}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          );
        })}

        {buckets.unconfigured.length > 0 && (
          <p className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
            <MinusCircle className="size-3 shrink-0" aria-hidden="true" />
            <span className="font-medium">Not configured:</span>
            {buckets.unconfigured
              .map(({ collector }) => describeCollector(collector).label)
              .join(", ")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
