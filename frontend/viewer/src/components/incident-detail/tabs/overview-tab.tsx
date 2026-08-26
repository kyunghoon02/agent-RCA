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
import { formatTimestamp } from "@/lib/format";
import type { IncidentDetail } from "@/lib/types";

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

      <Card>
        <CardHeader className="pb-2">
          <CardTitle>Provider collection results</CardTitle>
          <p className="text-xs text-muted-foreground">
            Every Provider the Viewer knows about. Providers this Incident never ran are
            listed as not configured rather than hidden.
          </p>
        </CardHeader>
        <CardContent className="pt-0">
          <ul className="divide-y divide-border">
            {rows.map(({ collector, status }) => {
              const descriptor = describeCollector(collector);
              return (
                <li
                  key={collector}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 first:pt-0"
                >
                  <div className="min-w-56 flex-1">
                    <p className="text-sm font-medium">{descriptor.label}</p>
                    <p className="text-[11px] text-muted-foreground">
                      {descriptor.description}
                    </p>
                  </div>

                  {status ? (
                    <>
                      <CollectorStatusBadge status={status.status} />
                      <span className="tabular text-[11px] text-muted-foreground">
                        {status.attempts} attempt{status.attempts === 1 ? "" : "s"}
                      </span>
                      <span className="tabular min-w-44 text-[11px] text-muted-foreground">
                        {status.started_at ? formatTimestamp(status.started_at) : "not started"}
                        {status.ended_at ? ` → ${formatTimestamp(status.ended_at).slice(11)}` : ""}
                      </span>
                      {status.error && (
                        <span className="font-mono text-[11px] text-status-critical">
                          {status.error}
                        </span>
                      )}
                    </>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                      <MinusCircle className="size-3" aria-hidden="true" />
                      not configured for this Incident
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </CardContent>
      </Card>

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
