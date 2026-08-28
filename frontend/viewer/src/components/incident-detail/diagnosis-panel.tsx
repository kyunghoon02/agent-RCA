"use client";

import {
  CheckCircle2,
  CircleDashed,
  Hourglass,
  Loader,
  XCircle,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { AgentRuntimeBadge, RcaOutcomeBadge } from "@/components/status";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EntityRefLabel } from "@/components/entity-ref";
import { formatTimestamp } from "@/lib/format";
import { reportEvidenceIds, reportMissingEvidence } from "@/lib/report-refs";
import type { Diagnosis, DiagnosisTone } from "@/lib/diagnosis";
import type { Incident, IncidentWorkState, ReportBundle } from "@/lib/types";
import { cn } from "@/lib/utils";

const TONE: Record<
  DiagnosisTone,
  { icon: LucideIcon; accent: string; rail: string; spin?: boolean }
> = {
  waiting: {
    icon: Hourglass,
    accent: "text-status-info",
    rail: "bg-status-info",
  },
  active: {
    icon: Loader,
    accent: "text-status-running",
    rail: "bg-status-running",
    spin: true,
  },
  resolved: {
    icon: CheckCircle2,
    accent: "text-status-success",
    rail: "bg-status-success",
  },
  caution: {
    icon: CircleDashed,
    accent: "text-status-warning",
    rail: "bg-status-warning",
  },
  failed: { icon: XCircle, accent: "text-status-critical", rail: "bg-status-critical" },
};

function Fact({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        {label}
      </dt>
      <dd className="mt-0.5 truncate font-mono text-xs">{children}</dd>
    </div>
  );
}

/**
 * The first thing the detail page answers: is there a diagnosis, and if not,
 * what is the pipeline waiting on.
 *
 * Root cause, cited Evidence and timing are rendered only when a stored Report
 * exists. With no Report there is deliberately nothing that could be mistaken
 * for a conclusion.
 */
export function DiagnosisPanel({
  incident,
  work,
  reports,
  diagnosis,
  onOpenReport,
  onFocusEvidence,
}: {
  incident: Incident;
  work: IncidentWorkState | null;
  reports: readonly ReportBundle[];
  diagnosis: Diagnosis;
  onOpenReport: () => void;
  onFocusEvidence: (evidenceId: string) => void;
}) {
  const tone = TONE[diagnosis.tone];
  const Icon = tone.icon;
  const bundle = reports.at(0);
  const analysis = work?.analysis ?? null;
  const contextId = analysis?.context_id ?? null;
  const citedIds = bundle ? reportEvidenceIds(bundle.report) : [];
  const missing = bundle ? reportMissingEvidence(bundle.report) : [];

  return (
    <Card className="relative overflow-hidden">
      <span aria-hidden="true" className={cn("absolute inset-y-0 left-0 w-0.5", tone.rail)} />
      <div className="flex flex-col gap-3 px-4 py-3 pl-5">
        <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
          <Icon
            className={cn(
              "mt-0.5 size-5 shrink-0",
              tone.accent,
              tone.spin && "animate-[spin_2s_linear_infinite]",
            )}
            aria-hidden="true"
          />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-sm font-semibold">{diagnosis.title}</h2>
              <RcaOutcomeBadge outcome={diagnosis.outcome} />
              <AgentRuntimeBadge awaiting={diagnosis.awaitingAgentRuntime} />
            </div>
            <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted-foreground">
              {diagnosis.description}
            </p>
          </div>
          {bundle && (
            <Button variant="outline" size="sm" onClick={onOpenReport}>
              Open RCA Report
            </Button>
          )}
        </div>

        {/*
         * Pipeline, work and outcome are shown as three separate fields on
         * purpose: ANALYZING is a location in the pipeline, READY is a queue
         * state, and neither is a conclusion.
         */}
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 border-t border-border pt-2.5 sm:grid-cols-3 lg:grid-cols-5">
          <Fact label="Pipeline">{incident.status}</Fact>
          <Fact label="Analysis work">
            {analysis ? analysis.state : "not enqueued"}
          </Fact>
          <Fact label="Frozen Context">{contextId ?? "—"}</Fact>
          <Fact label="RCA Report">
            {bundle ? bundle.report.report_id : "Not available"}
          </Fact>
          <Fact label="Last update">{formatTimestamp(incident.updated_at)}</Fact>
        </dl>

        {bundle && (
          <div className="border-t border-border pt-2.5">
            {bundle.report.root_cause ? (
              <>
                <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Root cause
                </p>
                <p className="mt-1 text-sm leading-relaxed">
                  {bundle.report.root_cause.summary}
                </p>
                {bundle.report.root_cause.cause_id && (
                  <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                    {bundle.report.root_cause.cause_id}
                  </p>
                )}
                <div className="mt-1.5">
                  <EntityRefLabel entity={bundle.report.root_cause.entity} />
                </div>
              </>
            ) : (
              <p className="text-xs text-muted-foreground">
                No root cause was recorded for this Incident.
              </p>
            )}

            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <Badge tone="outline">
                {citedIds.length} Evidence cited
              </Badge>
              <Badge tone={missing.length > 0 ? "warning" : "outline"}>
                {missing.length} Evidence missing
              </Badge>
              <span className="tabular text-[11px] text-muted-foreground">
                reported {formatTimestamp(bundle.report.generated_at)}
              </span>
            </div>

            {citedIds.length > 0 && (
              <div className="mt-1.5 flex flex-wrap items-center gap-1">
                <span className="text-[11px] text-muted-foreground">Cited:</span>
                {citedIds.slice(0, 8).map((id) => (
                  <button
                    key={id}
                    type="button"
                    onClick={() => onFocusEvidence(id)}
                    className="rounded border border-border px-1.5 py-0.5 font-mono text-[11px] hover:bg-accent"
                  >
                    {id}
                  </button>
                ))}
                {citedIds.length > 8 && (
                  <span className="text-[11px] text-muted-foreground">
                    +{citedIds.length - 8} more
                  </span>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
