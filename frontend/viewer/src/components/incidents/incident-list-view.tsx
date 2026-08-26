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
      applyFilters({ ...filters, statuses });
    },
    [applyFilters, filters],
  );

  const items = data?.items ?? [];
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

      <Card className="p-3">
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
