"use client";

import {
  Ban,
  CheckCircle2,
  CircleSlash,
  FileText,
  Info,
  Lightbulb,
  Lock,
  PauseCircle,
  ShieldQuestion,
  XCircle,
} from "lucide-react";
import { EmptyState } from "@/components/empty-state";
import { EntityRefLabel } from "@/components/entity-ref";
import { Notice } from "@/components/notices";
import { AgentRunStatusBadge, ConclusionBadge, conclusionLabel } from "@/components/status";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatMillis, formatRatio, formatTimestamp } from "@/lib/format";
import type {
  AgentRunAudit,
  Incident,
  IncidentWorkState,
  RcaHypothesis,
  ReportBundle,
} from "@/lib/types";
import { isWaitingForAgentRuntime } from "@/lib/work";
import { cn } from "@/lib/utils";
import { HypothesisChart } from "./hypothesis-chart";

const HYPOTHESIS_TONE: Record<
  RcaHypothesis["status"],
  "success" | "warning" | "info" | "neutral"
> = {
  supported: "success",
  competing: "warning",
  unresolved: "info",
  rejected: "neutral",
};

export function ReportTab({
  incident,
  reports,
  agentRuns,
  work,
  onFocusEvidence,
}: {
  incident: Incident;
  reports: ReportBundle[];
  agentRuns: AgentRunAudit[];
  work: IncidentWorkState | null;
  onFocusEvidence: (evidenceId: string) => void;
}) {
  const bundle = reports.at(0);

  if (!bundle) {
    return (
      <ReportUnavailable incident={incident} work={work} agentRuns={agentRuns} />
    );
  }

  const report = bundle.report;
  const run = agentRuns.find((item) => item.context_id === report.context_id) ?? agentRuns.at(0);
  const conclusion = conclusionLabel(report.status, report.root_cause !== null);
  const isAbstain = conclusion.label === "ABSTAIN";

  return (
    <div className="flex flex-col gap-3">
      <Card>
        <CardHeader className="gap-1.5 pb-2">
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle className="font-mono">{report.report_id}</CardTitle>
            <ConclusionBadge status={report.status} hasRootCause={report.root_cause !== null} />
            <Badge tone="outline">{report.path} path</Badge>
            <Badge tone="neutral" title="This Viewer and this Report take no action.">
              <Lock aria-hidden="true" />
              read-only
            </Badge>
            <span className="tabular ml-auto text-[11px] text-muted-foreground">
              generated {formatTimestamp(report.generated_at)}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">{conclusion.description}</p>
        </CardHeader>

        <CardContent className="flex flex-col gap-3 pt-0">
          {isAbstain && (
            // An abstention is the designed safe outcome of the Evidence gate,
            // so it is presented as information, never as a failed run.
            <Notice tone="info" icon={ShieldQuestion} title="The Agent abstained">
              No root cause is stated because the Evidence did not support one. This is the
              intended outcome of the Evidence gate, not an error. The missing Evidence
              listed below is what would be needed to conclude.
            </Notice>
          )}

          {report.root_cause ? (
            <section className="rounded border border-status-success/40 bg-status-success-surface px-3 py-2.5">
              <h3 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-status-success">
                <CheckCircle2 className="size-3" aria-hidden="true" />
                Root cause
              </h3>
              <p className="mt-1 text-sm leading-relaxed">{report.root_cause.summary}</p>
              <div className="mt-1.5">
                <EntityRefLabel entity={report.root_cause.entity} />
              </div>
              <EvidenceIdList
                label="Supporting Evidence"
                ids={report.root_cause.supporting_evidence_ids}
                onFocus={onFocusEvidence}
                tone="support"
              />
            </section>
          ) : (
            <section className="rounded border border-border bg-surface-sunken px-3 py-2.5">
              <h3 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                <CircleSlash className="size-3" aria-hidden="true" />
                Root cause
              </h3>
              <p className="mt-1 text-sm text-muted-foreground">
                No root cause was stated for this Incident.
              </p>
            </section>
          )}

          <section>
            <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              Ranked hypotheses ({report.hypotheses.length})
            </h3>
            {report.hypotheses.length > 1 && (
              <div className="mb-2 rounded border border-border p-2">
                <HypothesisChart hypotheses={report.hypotheses} />
              </div>
            )}
            <ol className="flex flex-col gap-2">
              {[...report.hypotheses]
                .sort((left, right) => left.rank - right.rank)
                .map((hypothesis) => (
                  <li key={hypothesis.rank}>
                    <HypothesisCard
                      hypothesis={hypothesis}
                      onFocusEvidence={onFocusEvidence}
                    />
                  </li>
                ))}
            </ol>
          </section>

          <div className="grid gap-2 md:grid-cols-2">
            <section className="rounded border border-border px-3 py-2.5">
              <h3 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                <Lightbulb className="size-3" aria-hidden="true" />
                Remediation suggestions
              </h3>
              <p className="mt-1 text-[11px] text-muted-foreground">
                Suggestions only. This Viewer cannot apply any of them.
              </p>
              {report.remediation.suggestions.length === 0 ? (
                <p className="mt-1.5 text-xs text-muted-foreground">None proposed.</p>
              ) : (
                <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-xs">
                  {report.remediation.suggestions.map((suggestion) => (
                    <li key={suggestion}>{suggestion}</li>
                  ))}
                </ul>
              )}
            </section>

            <section className="rounded border border-border px-3 py-2.5">
              <h3 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                Verification conditions
              </h3>
              <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-xs">
                {report.remediation.verification_conditions.map((condition) => (
                  <li key={condition}>{condition}</li>
                ))}
              </ul>
            </section>
          </div>

          {report.limitations.length > 0 && (
            <section className="rounded border border-border bg-surface-sunken px-3 py-2.5">
              <h3 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                <Info className="size-3" aria-hidden="true" />
                Limitations
              </h3>
              <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-xs">
                {report.limitations.map((limitation) => (
                  <li key={limitation}>{limitation}</li>
                ))}
              </ul>
            </section>
          )}
        </CardContent>
      </Card>

      <BudgetCard report={report} run={run} />
    </div>
  );
}

function HypothesisCard({
  hypothesis,
  onFocusEvidence,
}: {
  hypothesis: RcaHypothesis;
  onFocusEvidence: (evidenceId: string) => void;
}) {
  return (
    <div className="rounded border border-border px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="tabular text-xs font-semibold">#{hypothesis.rank}</span>
        <Badge tone={HYPOTHESIS_TONE[hypothesis.status]}>{hypothesis.status}</Badge>
        <span className="tabular text-[11px] text-muted-foreground">
          confidence {formatRatio(hypothesis.confidence)}
        </span>
        <div
          className="ml-auto h-1.5 w-24 overflow-hidden rounded-full bg-muted"
          role="img"
          aria-label={`Confidence ${formatRatio(hypothesis.confidence)}`}
        >
          <div
            className={cn(
              "h-full rounded-full",
              hypothesis.status === "supported" && "bg-status-success",
              hypothesis.status === "competing" && "bg-status-warning",
              hypothesis.status === "unresolved" && "bg-status-info",
              hypothesis.status === "rejected" && "bg-status-neutral",
            )}
            style={{ width: `${Math.round(hypothesis.confidence * 100)}%` }}
          />
        </div>
      </div>

      <p className="mt-1.5 text-sm leading-relaxed">{hypothesis.summary}</p>
      <div className="mt-1">
        <EntityRefLabel entity={hypothesis.entity} />
      </div>

      <EvidenceIdList
        label="Supporting"
        ids={hypothesis.supporting_evidence_ids}
        onFocus={onFocusEvidence}
        tone="support"
      />
      <EvidenceIdList
        label="Contradicting"
        ids={hypothesis.contradicting_evidence_ids}
        onFocus={onFocusEvidence}
        tone="contradict"
      />

      {hypothesis.missing_evidence.length > 0 && (
        <div className="mt-1.5">
          <p className="text-[11px] font-medium text-muted-foreground">Missing Evidence</p>
          <ul className="mt-0.5 flex list-disc flex-col gap-0.5 pl-4 text-[11px] text-muted-foreground">
            {hypothesis.missing_evidence.map((entry) => (
              <li key={entry}>{entry}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function EvidenceIdList({
  label,
  ids,
  onFocus,
  tone,
}: {
  label: string;
  ids: string[];
  onFocus: (evidenceId: string) => void;
  tone: "support" | "contradict";
}) {
  if (ids.length === 0) return null;
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1">
      <span className="text-[11px] text-muted-foreground">{label}:</span>
      {ids.map((id) => (
        <button
          key={id}
          type="button"
          onClick={() => onFocus(id)}
          title="Open this Evidence item"
          className={cn(
            "rounded border px-1.5 py-0.5 font-mono text-[11px] hover:bg-accent",
            tone === "support"
              ? "border-status-success/40 text-status-success"
              : "border-status-critical/40 text-status-critical",
          )}
        >
          {id}
        </button>
      ))}
    </div>
  );
}

function BudgetCard({
  report,
  run,
}: {
  report: ReportBundle["report"];
  run: AgentRunAudit | undefined;
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle>Analysis budget</CardTitle>
        <p className="text-xs text-muted-foreground">
          Run accounting only. Prompts and reasoning traces are never stored or exposed.
        </p>
      </CardHeader>
      <CardContent className="pt-0">
        {!report.budget.applicable ? (
          <p className="text-xs text-muted-foreground">
            No budget applied to this Report — it was produced by the deterministic path.
          </p>
        ) : (
          <>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-xs sm:grid-cols-3 lg:grid-cols-6">
              <Metric term="Model" value={run?.model ?? "not recorded"} />
              <Metric term="LLM calls" value={String(report.budget.llm_calls)} />
              <Metric term="Tool calls" value={String(report.budget.tool_calls)} />
              <Metric term="Tree depth" value={String(report.budget.tree_depth)} />
              <Metric term="Wall time" value={formatMillis(report.budget.wall_time_ms)} />
              <Metric
                term="Budget"
                value={report.budget.exhausted ? "exhausted" : "within limits"}
                tone={report.budget.exhausted ? "warning" : "normal"}
              />
            </dl>

            {run && (
              <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-border pt-2">
                <span className="font-mono text-[11px] text-muted-foreground">
                  {run.agent_run_id}
                </span>
                <AgentRunStatusBadge status={run.status} />
                <Badge tone="outline">{run.reason_code}</Badge>
                <Badge tone="outline">knowledge {run.knowledge_status}</Badge>
                <span className="tabular text-[11px] text-muted-foreground">
                  {run.usage.total_tokens.toLocaleString("en-US")} tokens ·{" "}
                  {run.usage.tool_calls}/{run.budget.max_tool_calls} tool calls ·{" "}
                  {formatMillis(run.usage.wall_time_ms)} of{" "}
                  {formatMillis(run.budget.max_wall_time_ms)}
                </span>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function Metric({
  term,
  value,
  tone = "normal",
}: {
  term: string;
  value: string;
  tone?: "normal" | "warning";
}) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{term}</dt>
      <dd
        className={cn(
          "tabular font-mono text-xs",
          tone === "warning" && "text-status-warning",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

/** Report absent: the message states the reason for *this* Incident's state. */
function ReportUnavailable({
  incident,
  work,
  agentRuns,
}: {
  incident: Incident;
  work: IncidentWorkState | null;
  agentRuns: AgentRunAudit[];
}) {
  if (isWaitingForAgentRuntime(incident, work)) {
    return (
      <EmptyState
        icon={PauseCircle}
        title="Agent runtime is disabled. Frozen Context is ready for analysis."
        description={
          <>
            The analysis work item is <strong>READY</strong> and pinned to Context{" "}
            <code className="font-mono">{work?.analysis?.context_id}</code>. No Agent runtime
            has claimed it, so no Report exists yet. This is a waiting state, not a failure.
          </>
        }
      />
    );
  }

  const failedRun = agentRuns.find((run) => run.status !== "SUCCEEDED");
  if (failedRun) {
    return (
      <EmptyState
        icon={Ban}
        title="Analysis ran but produced no Report"
        tone="warning"
        description={
          <>
            Agent run <code className="font-mono">{failedRun.agent_run_id}</code> ended as{" "}
            <strong>{failedRun.status}</strong> ({failedRun.reason_code}). No conclusion was
            stored.
          </>
        }
      />
    );
  }

  if (incident.status === "FAILED") {
    return (
      <EmptyState
        icon={XCircle}
        title="No Report — the Incident failed before analysis"
        tone="critical"
        description="The run ended as FAILED. Check the lifecycle stepper and Timeline for the stage that stopped it."
      />
    );
  }

  if (incident.status === "PARTIAL") {
    return (
      <EmptyState
        icon={CircleSlash}
        title="No Report — the Incident ended as PARTIAL"
        tone="warning"
        description="Collection or localization completed with gaps and analysis was not attempted. The Frozen Context tab lists the missing Evidence."
      />
    );
  }

  return (
    <EmptyState
      icon={FileText}
      title="No RCA Report yet"
      description={`This Incident is ${incident.status}. A Report appears here once analysis completes.`}
    />
  );
}
