"use client";

import * as React from "react";
import {
  ArrowRight,
  Ban,
  GitBranch,
  Layers,
  Snowflake,
  Target,
  TriangleAlert,
  Unlink,
} from "lucide-react";
import { EmptyState } from "@/components/empty-state";
import { EntityRefDetail } from "@/components/entity-ref";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatRatio, formatTimestamp } from "@/lib/format";
import { summariseTopology, type TopologySummary } from "@/lib/topology";
import { entityKind, entityNamespace } from "@/lib/lifecycle";
import {
  isGraphEntityRef,
  isInvestigationScope,
  type ContextPackage,
  type EntityRef,
  type EvidenceItem,
  type IncidentStatus,
  type StatePath,
} from "@/lib/types";
import { cn } from "@/lib/utils";

export function ContextTab({
  contexts,
  evidence,
  incidentStatus,
  onFocusEvidence,
}: {
  contexts: ContextPackage[];
  evidence: EvidenceItem[];
  incidentStatus: IncidentStatus;
  onFocusEvidence: (evidenceId: string) => void;
}) {
  if (contexts.length === 0) {
    const terminal = incidentStatus === "FAILED" || incidentStatus === "PARTIAL";
    return (
      <EmptyState
        icon={Snowflake}
        title="No Frozen Context"
        tone={terminal ? "warning" : "neutral"}
        description={
          terminal
            ? `Localization never produced a Context for this Incident, which ended as ${incidentStatus}. Without a Frozen Context no analysis can run.`
            : "Localization has not frozen a Context yet. One appears here once the localization work item completes."
        }
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {contexts.map((context) => (
        <ContextPanel
          key={context.context_id}
          context={context}
          evidence={evidence}
          onFocusEvidence={onFocusEvidence}
        />
      ))}
    </div>
  );
}

function ContextPanel({
  context,
  evidence,
  onFocusEvidence,
}: {
  context: ContextPackage;
  evidence: EvidenceItem[];
  onFocusEvidence: (evidenceId: string) => void;
}) {
  const inPaths = React.useMemo(() => {
    const ids = new Set<string>();
    for (const path of context.state_paths) {
      for (const id of path.evidence_ids) ids.add(id);
    }
    return ids;
  }, [context.state_paths]);

  // Evidence cited by the Context but not attached to any path is surfaced
  // rather than dropped: it is still part of what the Agent was given.
  const outsideGraph = context.evidence_ids.filter((id) => !inPaths.has(id));
  // Evidence that was collected for the Incident but never frozen into this
  // Context is shown too, so the gap between "collected" and "analysed" is visible.
  const notInContext = evidence
    .map((item) => item.evidence_id)
    .filter((id) => !context.evidence_ids.includes(id));
  const scope = context.scope;
  const topology = React.useMemo(() => summariseTopology(context), [context]);

  return (
    <Card>
      <CardHeader className="gap-1.5 pb-2">
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="font-mono">{context.context_id}</CardTitle>
          <Badge tone="outline">
            <Snowflake aria-hidden="true" />
            frozen {formatTimestamp(context.frozen_at)}
          </Badge>
          <Badge tone={context.localization.strategy === "stategraph" ? "success" : "warning"}>
            <Target aria-hidden="true" />
            {context.localization.strategy}
          </Badge>
          <Badge
            tone={
              context.localization.context_completeness >= 0.8
                ? "success"
                : context.localization.context_completeness >= 0.5
                  ? "warning"
                  : "critical"
            }
          >
            completeness {formatRatio(context.localization.context_completeness)}
          </Badge>
        </div>
        <p className="text-xs text-muted-foreground">
          Narrowed{" "}
          <span className="tabular font-medium text-foreground">
            {context.localization.candidate_entities_before}
          </span>{" "}
          candidate entities to{" "}
          <span className="tabular font-medium text-foreground">
            {context.localization.candidate_entities_after}
          </span>
          .
        </p>
      </CardHeader>

      <CardContent className="flex flex-col gap-3 pt-0">
        <section>
          <SectionTitle icon={Target}>Seed and scope</SectionTitle>
          <dl className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
            <Row
              term="Seed entity"
              value={`${entityKind(context.source_entity)}/${context.source_entity.name}`}
            />
            <Row term="Namespace" value={entityNamespace(context.source_entity) ?? "—"} />
            {isInvestigationScope(scope) ? (
              <>
                <Row term="Seed entity IDs" value={scope.seed_entity_ids.join(", ")} />
                <Row term="Domains" value={scope.domains.join(", ") || "—"} />
                <Row term="Relation types" value={scope.relation_types.join(", ") || "—"} />
                <Row term="Max depth" value={String(scope.max_depth)} />
                <Row
                  term="Time window"
                  value={`${formatTimestamp(scope.time_window.start)} → ${formatTimestamp(scope.time_window.end)}`}
                />
                <Row term="Max entities" value={String(scope.max_entities)} />
              </>
            ) : (
              <>
                <Row term="Namespaces" value={scope.namespaces.join(", ")} />
                <Row
                  term="Metapaths"
                  value={scope.metapaths.map((path) => path.join(" → ")).join("  |  ") || "—"}
                />
                <Row
                  term="Time window"
                  value={`${formatTimestamp(scope.time_window.start)} → ${formatTimestamp(scope.time_window.end)}`}
                />
                <Row term="Max entities" value={String(scope.max_entities)} />
              </>
            )}
          </dl>
        </section>

        <section>
          <SectionTitle icon={GitBranch}>
            Topology ({context.state_paths.length} StateGraph path
            {context.state_paths.length === 1 ? "" : "s"})
          </SectionTitle>
          {context.state_paths.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No StateGraph path was attached to this Context.
            </p>
          ) : (
            <TopologyView
              topology={topology}
              paths={context.state_paths}
              onFocusEvidence={onFocusEvidence}
            />
          )}
        </section>

        <section>
          <SectionTitle icon={Layers}>
            Evidence in this Context ({context.evidence_ids.length})
          </SectionTitle>
          <div className="flex flex-wrap gap-1">
            {context.evidence_ids.map((id) => (
              <EvidenceChip
                key={id}
                id={id}
                onFocus={onFocusEvidence}
                known={evidence.some((item) => item.evidence_id === id)}
                highlight={context.recent_change_evidence_ids.includes(id)}
              />
            ))}
          </div>
          {context.recent_change_evidence_ids.length > 0 && (
            <p className="mt-1.5 text-[11px] text-muted-foreground">
              Highlighted items are recent-change Evidence.
            </p>
          )}

          {notInContext.length > 0 && (
            <div className="mt-2 rounded border border-border bg-surface-sunken px-2.5 py-2">
              <p className="flex items-center gap-1.5 text-[11px] font-medium">
                <Unlink className="size-3" aria-hidden="true" />
                {notInContext.length} collected Evidence item
                {notInContext.length === 1 ? " was" : "s were"} not frozen into this Context
              </p>
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                These exist for the Incident but were not part of what analysis received.
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {notInContext.map((id) => (
                  <EvidenceChip key={id} id={id} onFocus={onFocusEvidence} known />
                ))}
              </div>
            </div>
          )}

          {outsideGraph.length > 0 && (
            <div className="mt-2 rounded border border-status-warning/40 bg-status-warning-surface px-2.5 py-2">
              <p className="flex items-center gap-1.5 text-[11px] font-medium text-status-warning">
                <Unlink className="size-3" aria-hidden="true" />
                {outsideGraph.length} Evidence item
                {outsideGraph.length === 1 ? " is" : "s are"} not attached to any StateGraph path
              </p>
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                These were frozen into the Context but carry no graph relation, so they are
                listed here instead of being hidden.
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {outsideGraph.map((id) => (
                  <EvidenceChip
                    key={id}
                    id={id}
                    onFocus={onFocusEvidence}
                    known={evidence.some((item) => item.evidence_id === id)}
                  />
                ))}
              </div>
            </div>
          )}
        </section>

        {(context.missing_evidence.length > 0 || context.collector_failures.length > 0) && (
          <section className="grid gap-2 md:grid-cols-2">
            {context.missing_evidence.length > 0 && (
              <div className="rounded border border-border bg-surface-sunken px-2.5 py-2">
                <p className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                  <TriangleAlert className="size-3" aria-hidden="true" />
                  Missing Evidence
                </p>
                <ul className="mt-1 flex flex-col gap-1">
                  {context.missing_evidence.map((entry) => (
                    <li key={`${entry.source}:${entry.reason}`} className="text-[11px]">
                      <span className="font-mono font-medium">{entry.source}</span>
                      <span className="text-muted-foreground"> — {entry.reason}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {context.collector_failures.length > 0 && (
              <div className="rounded border border-status-critical/40 bg-status-critical-surface px-2.5 py-2">
                <p className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-status-critical">
                  <Ban className="size-3" aria-hidden="true" />
                  Collector failures
                </p>
                <ul className="mt-1 flex flex-col gap-1">
                  {context.collector_failures.map((entry) => (
                    <li key={`${entry.collector}:${entry.error}`} className="text-[11px]">
                      <span className="font-mono font-medium">{entry.collector}</span>
                      <span className="text-muted-foreground"> — {entry.error}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * One path rendered as a readable chain, e.g.
 * `frontend Service → CALLS → cartservice Service`.
 *
 * A force-directed graph would be harder to read at a glance and harder to
 * operate with a keyboard, so paths stay linear and scroll horizontally.
 */
function StatePathRow({
  path,
  onFocusEvidence,
}: {
  path: StatePath;
  onFocusEvidence: (evidenceId: string) => void;
}) {
  const [expanded, setExpanded] = React.useState(false);
  return (
    <div className="rounded border border-border">
      <div className="flex flex-wrap items-center gap-2 px-2.5 py-2">
        <span className="font-mono text-[11px] text-muted-foreground">{path.path_id}</span>
        <Button
          variant="ghost"
          size="xs"
          className="ml-auto"
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? "Hide entities" : "Show entities"}
        </Button>
      </div>

      <div className="scroll-x flex items-center gap-1.5 border-t border-border px-2.5 py-2.5">
        {path.entities.map((entity, index) => (
          <React.Fragment key={`${entity.name}-${index}`}>
            <EntityNode entity={entity} />
            {index < path.entities.length - 1 && (
              <span className="flex shrink-0 flex-col items-center gap-0.5 px-1">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                  {path.relations[index] ?? "RELATED"}
                </span>
                <ArrowRight className="size-3.5 text-muted-foreground" aria-hidden="true" />
              </span>
            )}
          </React.Fragment>
        ))}
      </div>

      {path.evidence_ids.length > 0 && (
        <div className="flex flex-wrap items-center gap-1 border-t border-border px-2.5 py-2">
          <span className="text-[11px] text-muted-foreground">Supporting Evidence:</span>
          {path.evidence_ids.map((id) => (
            <EvidenceChip key={id} id={id} onFocus={onFocusEvidence} known />
          ))}
        </div>
      )}

      {expanded && (
        <div className="grid gap-2 border-t border-border bg-surface-sunken px-2.5 py-2 md:grid-cols-2">
          {path.entities.map((entity, index) => (
            <div key={`${entity.name}-detail-${index}`} className="rounded border border-border bg-card p-2">
              <EntityRefDetail entity={entity} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function EntityNode({ entity }: { entity: EntityRef }) {
  const namespace = entityNamespace(entity);
  return (
    <span
      className={cn(
        "flex shrink-0 flex-col rounded border px-2 py-1",
        entity.exists
          ? "border-border bg-card"
          : "border-status-critical/50 bg-status-critical-surface",
      )}
    >
      <span className="font-mono text-xs font-medium">{entity.name}</span>
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {entityKind(entity)}
        {namespace ? ` · ${namespace}` : ""}
      </span>
      {isGraphEntityRef(entity) && (
        <span className="font-mono text-[10px] text-muted-foreground">{entity.entity_id}</span>
      )}
      {!entity.exists && (
        <span className="text-[10px] font-medium text-status-critical">not found</span>
      )}
    </span>
  );
}

function EvidenceChip({
  id,
  onFocus,
  known,
  highlight = false,
}: {
  id: string;
  onFocus: (evidenceId: string) => void;
  known: boolean;
  highlight?: boolean;
}) {
  if (!known) {
    return (
      <span
        className="rounded border border-dashed border-border px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
        title="Referenced by this Context but not present in the returned Evidence set."
      >
        {id} (not returned)
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={() => onFocus(id)}
      className={cn(
        "rounded border px-1.5 py-0.5 font-mono text-[11px] hover:bg-accent",
        highlight
          ? "border-status-info/50 bg-status-info-surface text-status-info"
          : "border-border",
      )}
    >
      {id}
    </button>
  );
}

function SectionTitle({
  icon: Icon,
  children,
}: {
  icon: typeof Target;
  children: React.ReactNode;
}) {
  return (
    <h3 className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
      <Icon className="size-3" aria-hidden="true" />
      {children}
    </h3>
  );
}

function Row({ term, value }: { term: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="min-w-28 shrink-0 text-muted-foreground">{term}</dt>
      <dd className="break-all font-mono">{value}</dd>
    </div>
  );
}


/**
 * Compact topology before the raw paths.
 *
 * A live Context routinely carries 20+ paths that differ only in their tail, so
 * the raw list reads as noise. Distinct entity-type shapes are shown once with
 * a count, and the complete paths stay one click away — progressive disclosure,
 * not removal.
 */
function TopologyView({
  topology,
  paths,
  onFocusEvidence,
}: {
  topology: TopologySummary;
  paths: StatePath[];
  onFocusEvidence: (evidenceId: string) => void;
}) {
  const [showAll, setShowAll] = React.useState(false);
  const listId = React.useId();

  return (
    <div className="flex flex-col gap-2">
      <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 rounded border border-border bg-surface-sunken px-3 py-2 text-xs sm:grid-cols-4">
        <Metric term="Entities" value={String(topology.entityCount)} />
        <Metric term="Paths" value={String(topology.pathCount)} />
        <Metric term="Evidence" value={String(topology.evidenceCount)} />
        <Metric term="Recent change" value={String(topology.recentChangeCount)} />
        <Metric term="Namespaces" value={topology.namespaces.join(", ") || "—"} />
        <Metric term="Strategy" value={topology.strategy} />
        <Metric term="Completeness" value={formatRatio(topology.completeness)} />
        <Metric
          term="Missing Evidence"
          value={String(topology.missingEvidenceCount)}
          tone={topology.missingEvidenceCount > 0 ? "warning" : undefined}
        />
      </dl>

      <ul className="flex flex-col gap-1">
        {topology.shapes.map((shape) => (
          <li
            key={`${shape.types.join(">")}|${shape.relations.join(">")}`}
            className="scroll-x flex items-center gap-1.5 rounded border border-border px-2.5 py-1.5"
          >
            <span className="tabular shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] font-semibold">
              ×{shape.count}
            </span>
            {shape.types.map((type, index) => (
              <React.Fragment key={`${type}-${index}`}>
                {index > 0 && (
                  <span className="flex shrink-0 items-center gap-1">
                    <span className="text-[9px] uppercase tracking-wide text-muted-foreground">
                      {shape.relations[index - 1]}
                    </span>
                    <ArrowRight className="size-3 text-muted-foreground" aria-hidden="true" />
                  </span>
                )}
                <span className="shrink-0 rounded border border-border bg-card px-1.5 py-0.5 font-mono text-[11px]">
                  {type}
                </span>
              </React.Fragment>
            ))}
            <span className="ml-auto shrink-0 text-[10px] text-muted-foreground">
              {shape.uniqueEvidenceCount} unique Evidence
            </span>
          </li>
        ))}
      </ul>

      <div>
        <Button
          variant="outline"
          size="xs"
          aria-expanded={showAll}
          aria-controls={listId}
          onClick={() => setShowAll((current) => !current)}
        >
          {showAll ? "Hide StateGraph paths" : `Show all ${paths.length} StateGraph paths`}
        </Button>
      </div>

      {showAll && (
        <div id={listId} className="flex flex-col gap-2">
          {paths.map((path) => (
            <StatePathRow key={path.path_id} path={path} onFocusEvidence={onFocusEvidence} />
          ))}
        </div>
      )}
    </div>
  );
}

function Metric({
  term,
  value,
  tone,
}: {
  term: string;
  value: string;
  tone?: "warning";
}) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        {term}
      </dt>
      <dd
        className={cn(
          "truncate font-mono text-xs",
          tone === "warning" && "text-status-warning",
        )}
      >
        {value}
      </dd>
    </div>
  );
}
