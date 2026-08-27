"use client";

import * as React from "react";
import { Inbox, SearchX } from "lucide-react";
import { EmptyState } from "@/components/empty-state";
import {
  DemoDataNotice,
  DisconnectedNotice,
  StaleDataNotice,
} from "@/components/notices";
import { usePublishViewerStatus } from "@/components/viewer-status";
import { Card } from "@/components/ui/card";
import { getViewerAdapter } from "@/lib/adapter";
import { DEFAULT_PAGE_SIZE, DEFAULT_POLL_INTERVAL_MS } from "@/lib/config";
import { usePollingResource } from "@/lib/hooks/use-polling-resource";
import type { IncidentListQuery, IncidentListResult, IncidentStatus } from "@/lib/types";
import {
  hasCollectorProblems,
  hasGroupableRepeats,
  QUICK_FILTERS,
  type QuickFilter,
} from "@/lib/incident-list";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CursorPagination } from "./pagination";
import { EMPTY_FILTERS, IncidentFilters, type FilterState } from "./incident-filters";
import { IncidentTable, IncidentTableSkeleton } from "./incident-table";
import { RefreshControls } from "./refresh-controls";
import { SummaryCards } from "./summary-cards";

function buildQuery(filters: FilterState, cursor: string | null): IncidentListQuery {
  return {
    schema_version: "1.0.0",
    statuses: filters.statuses,
    severities: filters.severities,
    namespace: filters.namespace.trim() || null,
    search: filters.search.trim() || null,
    limit: DEFAULT_PAGE_SIZE,
    cursor,
  };
}

export function IncidentListView() {
  const adapter = React.useMemo(() => getViewerAdapter(), []);

  const [filters, setFilters] = React.useState<FilterState>(EMPTY_FILTERS);
  const [cursorStack, setCursorStack] = React.useState<(string | null)[]>([null]);
  const [polling, setPolling] = React.useState(true);
  const [intervalMs, setIntervalMs] = React.useState(DEFAULT_POLL_INTERVAL_MS);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [quickFilterId, setQuickFilterId] = React.useState<string | null>(null);
  const [collapseRepeats, setCollapseRepeats] = React.useState(true);

  const cursor = cursorStack[cursorStack.length - 1] ?? null;
  const query = React.useMemo(() => buildQuery(filters, cursor), [filters, cursor]);

  const fetcher = React.useCallback(
    (signal: AbortSignal) => adapter.listIncidents(query, signal),
    [adapter, query],
  );

  // Pausing zeroes the interval rather than disabling the resource, so the
  // first load still happens and the rows already on screen are kept.
  const { data, error, isLoading, isFetching, isStale, lastUpdatedAt, refresh } =
    usePollingResource<IncidentListResult>({
      fetcher,
      fetchKey: JSON.stringify(query),
      intervalMs: polling ? intervalMs : 0,
      enabled: true,
    });

  usePublishViewerStatus({
    mode: adapter.mode,
    error,
    isStale,
    lastUpdatedAt,
  });

  /** Changing filters starts a new cursor chain: old cursors are filter-bound. */
  const applyFilters = React.useCallback((next: FilterState) => {
    setFilters(next);
    setCursorStack([null]);
  }, []);

  const applyStatuses = React.useCallback(
    (statuses: IncidentStatus[]) => {
      setQuickFilterId(null);
      applyFilters({ ...filters, statuses });
    },
    [applyFilters, filters],
  );

  const toggleQuickFilter = React.useCallback(
    (filter: QuickFilter) => {
      const next = quickFilterId === filter.id ? null : filter.id;
      setQuickFilterId(next);
      const statuses = next && !filter.clientOnly ? filter.statuses : [];
      applyFilters({ ...filters, statuses });
    },
    [applyFilters, filters, quickFilterId],
  );

  const activeQuick = QUICK_FILTERS.find((f) => f.id === quickFilterId) ?? null;
  const rawItems = data?.items ?? [];
  // Only the client-only filter is applied after fetching; status filters are
  // sent to the API so pagination stays correct.
  const items = activeQuick?.clientOnly
    ? rawItems.filter(hasCollectorProblems)
    : rawItems;
  /*
   * The control is only meaningful when this page actually holds a foldable
   * run. Live Kubernetes entities often arrive without `cluster_id` or `uid`,
   * and grouping on namespace/name would merge unrelated Incidents — so the
   * control is disabled and says why rather than appearing to do nothing.
   */
  const groupable = React.useMemo(() => hasGroupableRepeats(items), [items]);

  const hasFilters =
    filters.statuses.length > 0 ||
    filters.severities.length > 0 ||
    Boolean(filters.namespace) ||
    Boolean(filters.search);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-base font-semibold tracking-tight">Incidents</h1>
          <p className="text-xs text-muted-foreground">
            Stored Incidents ordered by last update. This view issues read queries only.
          </p>
        </div>
        <RefreshControls
          polling={polling}
          onPollingChange={setPolling}
          intervalMs={intervalMs}
          onIntervalChange={setIntervalMs}
          onRefresh={refresh}
          isFetching={isFetching}
        />
      </div>

      {adapter.mode === "fixture" && <DemoDataNotice />}
      {error && !isStale && <DisconnectedNotice error={error} />}
      {isStale && error && <StaleDataNotice error={error} lastUpdatedAt={lastUpdatedAt} />}

      <SummaryCards
        items={items}
        activeStatuses={filters.statuses}
        onSelectStatuses={applyStatuses}
      />

      <Card className="flex flex-col gap-2 p-3">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            Quick filters
          </span>
          {QUICK_FILTERS.map((filter) => (
            <Button
              key={filter.id}
              type="button"
              variant={quickFilterId === filter.id ? "secondary" : "outline"}
              size="xs"
              aria-pressed={quickFilterId === filter.id}
              title={filter.description}
              onClick={() => toggleQuickFilter(filter)}
            >
              {filter.label}
              {filter.clientOnly && (
                <Badge tone="outline" className="ml-1">
                  page
                </Badge>
              )}
            </Button>
          ))}
          <Button
            type="button"
            variant={collapseRepeats && groupable ? "secondary" : "outline"}
            size="xs"
            disabled={!groupable}
            aria-pressed={groupable && collapseRepeats}
            aria-describedby={groupable ? undefined : "collapse-repeats-note"}
            title={
              groupable
                ? "Fold consecutive re-fires of the same alert on the same entity into one row."
                : "Unavailable: no Incident on this page has a stable source Entity identity to group on."
            }
            className="ml-auto"
            onClick={() => setCollapseRepeats((current) => !current)}
          >
            Collapse repeats
          </Button>
          {!groupable && (
            <span
              id="collapse-repeats-note"
              className="w-full text-[11px] text-muted-foreground"
            >
              Collapse repeats is unavailable on this page: grouping needs a stable
              source Entity identity (graph <code className="font-mono">entity_id</code>,
              or Kubernetes <code className="font-mono">cluster_id</code> +{" "}
              <code className="font-mono">uid</code>), and these Incidents do not carry
              one. Grouping on namespace and name alone would merge unrelated Incidents.
            </span>
          )}
        </div>
        <IncidentFilters value={filters} onChange={applyFilters} />
      </Card>

      <Card>
        {isLoading ? (
          <IncidentTableSkeleton />
        ) : items.length === 0 ? (
          <div className="p-4">
            {hasFilters ? (
              <EmptyState
                icon={SearchX}
                title="No Incidents match these filters"
                description="Adjust or clear the status, severity, namespace and search filters to widen the query."
              />
            ) : (
              <EmptyState
                icon={Inbox}
                title="No Incidents are stored"
                description="Nothing has been received yet. Incidents appear here once the receiver accepts an alert."
              />
            )}
          </div>
        ) : (
          <>
            <IncidentTable
              items={items}
              selectedId={selectedId}
              onSelect={setSelectedId}
              collapseRepeats={collapseRepeats && groupable}
            />
            <CursorPagination
              page={cursorStack.length}
              loadedCount={items.length}
              hasPrevious={cursorStack.length > 1}
              hasNext={Boolean(data?.next_cursor)}
              disabled={isFetching}
              onPrevious={() => setCursorStack((stack) => stack.slice(0, -1))}
              onNext={() =>
                setCursorStack((stack) =>
                  data?.next_cursor ? [...stack, data.next_cursor] : stack,
                )
              }
            />
          </>
        )}
      </Card>
    </div>
  );
}
