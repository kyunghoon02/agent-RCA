"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowLeft, Clock, Database, FileText, Layers, LayoutDashboard } from "lucide-react";
import { EmptyState } from "@/components/empty-state";
import { EntityRefLabel } from "@/components/entity-ref";
import {
  DemoDataNotice,
  DisconnectedNotice,
  StaleDataNotice,
  TruncationNotice,
} from "@/components/notices";
import { IncidentStatusBadge, SeverityBadge } from "@/components/status";
import { usePublishViewerStatus } from "@/components/viewer-status";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { getViewerAdapter, ViewerApiError } from "@/lib/adapter";
import { DEFAULT_POLL_INTERVAL_MS } from "@/lib/config";
import { formatTimestamp } from "@/lib/format";
import { usePollingResource } from "@/lib/hooks/use-polling-resource";
import { deriveLifecycle, entityNamespace, reachedStatusesFromTimeline } from "@/lib/lifecycle";
import type { IncidentDetail, IncidentWorkState } from "@/lib/types";
import { RefreshControls } from "@/components/incidents/refresh-controls";
import { LifecycleStepper } from "./lifecycle-stepper";
import { WorkQueueCards } from "./work-queue-cards";
import { ContextTab } from "./tabs/context-tab";
import { EvidenceTab } from "./tabs/evidence-tab";
import { OverviewTab } from "./tabs/overview-tab";
import { ReportTab } from "./tabs/report-tab";
import { TimelineTab } from "./tabs/timeline-tab";

const TABS = [
  { value: "overview", label: "Overview", icon: LayoutDashboard },
  { value: "evidence", label: "Evidence", icon: Database },
  { value: "context", label: "Frozen Context", icon: Layers },
  { value: "report", label: "RCA Report", icon: FileText },
  { value: "timeline", label: "Timeline", icon: Clock },
] as const;

export function IncidentDetailView({ incidentId }: { incidentId: string }) {
  const adapter = React.useMemo(() => getViewerAdapter(), []);
  const [polling, setPolling] = React.useState(true);
  const [intervalMs, setIntervalMs] = React.useState(DEFAULT_POLL_INTERVAL_MS);
  const [tab, setTab] = React.useState<string>("overview");
  const [focusedEvidenceId, setFocusedEvidenceId] = React.useState<string | null>(null);

  const detailFetcher = React.useCallback(
    (signal: AbortSignal) => adapter.getIncidentDetail(incidentId, signal),
    [adapter, incidentId],
  );
  const workFetcher = React.useCallback(
    (signal: AbortSignal) => adapter.getIncidentWorkState(incidentId, signal),
    [adapter, incidentId],
  );

  const detail = usePollingResource<IncidentDetail>({
    fetcher: detailFetcher,
    fetchKey: `detail:${incidentId}`,
    intervalMs: polling ? intervalMs : 0,
  });

  const work = usePollingResource<IncidentWorkState>({
    fetcher: workFetcher,
    fetchKey: `work:${incidentId}`,
    intervalMs: polling ? intervalMs : 0,
  });

  usePublishViewerStatus({
    mode: adapter.mode,
    error: detail.error,
    isStale: detail.isStale,
    lastUpdatedAt: detail.lastUpdatedAt,
  });

  /** Evidence IDs are cross-referenced everywhere; one click jumps to the item. */
  const focusEvidence = React.useCallback((evidenceId: string) => {
    setFocusedEvidenceId(evidenceId);
    setTab("evidence");
  }, []);

  const refreshAll = React.useCallback(() => {
    detail.refresh();
    work.refresh();
  }, [detail, work]);

  if (detail.isLoading) return <DetailSkeleton />;

  if (!detail.data) {
    const notFound =
      detail.error instanceof ViewerApiError && detail.error.kind === "not-found";
    return (
      <div className="flex flex-col gap-3">
        <BackLink />
        {notFound ? (
          <EmptyState
            icon={FileText}
            title={`Incident ${incidentId} was not found`}
            description="It may have been removed, or the identifier may be wrong."
          />
        ) : (
          detail.error && <DisconnectedNotice error={detail.error} />
        )}
      </div>
    );
  }

  const data = detail.data;
  const incident = data.incident;
  const namespace = entityNamespace(incident.source_entity);
  const lifecycle = deriveLifecycle(
    incident.status,
    reachedStatusesFromTimeline(data.timeline),
  );

  return (
    <div className="flex min-w-0 flex-col gap-3 overflow-x-hidden">
      <BackLink />

      {adapter.mode === "fixture" && <DemoDataNotice />}
      {detail.isStale && detail.error && (
        <StaleDataNotice error={detail.error} lastUpdatedAt={detail.lastUpdatedAt} />
      )}
      <TruncationNotice truncated={data.truncated} />

      <Card className="p-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="font-mono text-sm font-semibold">{incident.incident_id}</h1>
              <SeverityBadge severity={incident.severity} />
              <IncidentStatusBadge status={incident.status} />
            </div>
            <p className="mt-1 text-sm font-medium">{incident.alert.name}</p>
            <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <EntityRefLabel entity={incident.source_entity} showNamespace={false} />
              <span>namespace {namespace ?? "—"}</span>
              <span className="tabular">
                triggered {formatTimestamp(incident.triggered_at)}
              </span>
              <span className="tabular">updated {formatTimestamp(incident.updated_at)}</span>
            </div>
          </div>
          <RefreshControls
            polling={polling}
            onPollingChange={setPolling}
            intervalMs={intervalMs}
            onIntervalChange={setIntervalMs}
            onRefresh={refreshAll}
            isFetching={detail.isFetching || work.isFetching}
          />
        </div>

        <div className="mt-4">
          <LifecycleStepper steps={lifecycle} />
        </div>
      </Card>

      <WorkQueueCards
        incident={incident}
        work={work.data}
        isLoading={work.isLoading}
      />

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          {TABS.map(({ value, label, icon: Icon }) => (
            <TabsTrigger key={value} value={value}>
              <Icon className="size-3.5" aria-hidden="true" />
              {label}
              <TabCount value={value} detail={data} />
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="overview">
          <OverviewTab detail={data} />
        </TabsContent>
        <TabsContent value="evidence">
          <EvidenceTab evidence={data.evidence} focusedEvidenceId={focusedEvidenceId} />
        </TabsContent>
        <TabsContent value="context">
          <ContextTab
            contexts={data.contexts}
            evidence={data.evidence}
            incidentStatus={incident.status}
            onFocusEvidence={focusEvidence}
          />
        </TabsContent>
        <TabsContent value="report">
          <ReportTab
            incident={incident}
            reports={data.reports}
            agentRuns={data.agent_runs}
            work={work.data}
            onFocusEvidence={focusEvidence}
          />
        </TabsContent>
        <TabsContent value="timeline">
          <TimelineTab timeline={data.timeline} onFocusEvidence={focusEvidence} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function TabCount({ value, detail }: { value: string; detail: IncidentDetail }) {
  const counts: Record<string, number | null> = {
    overview: null,
    evidence: detail.evidence.length,
    context: detail.contexts.length,
    report: detail.reports.length,
    timeline: detail.timeline.length,
  };
  const count = counts[value];
  if (count === null || count === undefined) return null;
  return <span className="tabular text-[10px] text-muted-foreground">{count}</span>;
}

function BackLink() {
  return (
    <Button asChild variant="ghost" size="sm" className="w-fit -ml-1">
      <Link href="/incidents">
        <ArrowLeft className="size-3.5" aria-hidden="true" />
        All Incidents
      </Link>
    </Button>
  );
}

function DetailSkeleton() {
  return (
    <div className="flex flex-col gap-3">
      <Skeleton className="h-7 w-32" />
      <Card className="p-3">
        <Skeleton className="h-5 w-64" />
        <Skeleton className="mt-2 h-4 w-96" />
        <Skeleton className="mt-4 h-12 w-full" />
      </Card>
      <div className="grid gap-2 md:grid-cols-3">
        {Array.from({ length: 3 }, (_, index) => (
          <Skeleton key={index} className="h-48" />
        ))}
      </div>
      <Skeleton className="h-9 w-96" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}
