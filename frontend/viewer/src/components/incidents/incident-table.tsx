"use client";

import Link from "next/link";
import { ChevronRight, TriangleAlert } from "lucide-react";
import { EntityRefLabel } from "@/components/entity-ref";
import { IncidentStatusBadge, SeverityBadge } from "@/components/status";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { formatTimestamp } from "@/lib/format";
import {
  groupRepeatedIncidents,
  reportAvailability,
  summariseRepeatGroup,
} from "@/lib/incident-list";
import { Badge } from "@/components/ui/badge";
import * as React from "react";
import { entityNamespace } from "@/lib/lifecycle";
import type { IncidentSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

const COLUMNS = [
  "Severity",
  "Pipeline",
  "RCA Report",
  "Alert",
  "Source entity",
  "Namespace",
  "Updated",
  "Collector problems",
];

export function IncidentTable({
  items,
  selectedId,
  onSelect,
  collapseRepeats = true,
}: {
  items: IncidentSummary[];
  selectedId: string | null;
  onSelect: (incidentId: string) => void;
  collapseRepeats?: boolean;
}) {
  const rows = React.useMemo(
    () =>
      collapseRepeats
        ? groupRepeatedIncidents(items)
        : items.map((item) => ({ kind: "single" as const, key: item.incident_id, item })),
    [items, collapseRepeats],
  );

  return (
    <Table>
      <caption className="sr-only">
        Incidents ordered by last update, most recent first.
      </caption>
      <TableHeader>
        <TableRow>
          {COLUMNS.map((column) => (
            <TableHead key={column} scope="col">
              {column}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) =>
          row.kind === "single" ? (
            <IncidentRow
              key={row.key}
              item={row.item}
              selected={row.item.incident_id === selectedId}
              onSelect={onSelect}
            />
          ) : (
            <RepeatGroup
              key={row.key}
              items={row.items}
              selectedId={selectedId}
              onSelect={onSelect}
            />
          ),
        )}
      </TableBody>
    </Table>
  );
}

/**
 * A run of re-fires of the same alert on the same entity, folded to one row.
 * Expanding reveals every member; nothing is hidden from the operator.
 */
function RepeatGroup({
  items,
  selectedId,
  onSelect,
}: {
  items: IncidentSummary[];
  selectedId: string | null;
  onSelect: (incidentId: string) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const latest = items[0];
  const namespace = entityNamespace(latest.source_entity);
  const summary = summariseRepeatGroup(items);

  return (
    <>
      <TableRow className="bg-surface-sunken/60">
        <TableCell>
          <SeverityBadge severity={latest.severity} />
        </TableCell>
        <TableCell>
          {/*
           * A run can span several lifecycle statuses at once, so the newest
           * member's badge would misrepresent the group.
           */}
          {summary.isMixed ? (
            <MixedStatusCell summary={summary} />
          ) : (
            <IncidentStatusBadge status={latest.status} />
          )}
        </TableCell>
        <TableCell>
          {summary.isMixed ? (
            <span className="tabular whitespace-nowrap text-[11px]">
              <span
                className={
                  summary.reportAvailableCount > 0
                    ? "font-medium text-status-success"
                    : "text-muted-foreground"
                }
              >
                {summary.reportAvailableCount} report
                {summary.reportAvailableCount === 1 ? "" : "s"}
              </span>
              <span className="text-muted-foreground"> / {summary.total} runs</span>
            </span>
          ) : (
            <ReportCell status={latest.status} />
          )}
        </TableCell>
        <TableCell className="max-w-[22rem]">
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((current) => !current)}
            className="flex items-center gap-1.5 text-left font-medium hover:underline"
          >
            <ChevronRight
              aria-hidden="true"
              className={cn("size-3 shrink-0 transition-transform", open && "rotate-90")}
            />
            <span className="truncate">{latest.alert_name}</span>
          </button>
          <span className="text-[11px] text-muted-foreground">
            {items.length} repeated runs on this entity
          </span>
        </TableCell>
        <TableCell className="max-w-[18rem] truncate">
          <EntityRefLabel entity={latest.source_entity} showNamespace={false} />
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">{namespace ?? "—"}</TableCell>
        <TableCell className="tabular text-xs">
          {formatTimestamp(latest.updated_at)}
        </TableCell>
        <TableCell className="tabular">
          {/* The total across the run, matching how a single row reads. */}
          <CollectorProblemCell count={summary.collectorProblemTotal} />
        </TableCell>
      </TableRow>
      {open &&
        items.map((item) => (
          <IncidentRow
            key={item.incident_id}
            item={item}
            selected={item.incident_id === selectedId}
            onSelect={onSelect}
            nested
          />
        ))}
    </>
  );
}

/** Compact per-status breakdown for a run that is not homogeneous. */
function MixedStatusCell({
  summary,
}: {
  summary: ReturnType<typeof summariseRepeatGroup>;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <Badge tone="neutral">Mixed</Badge>
      <span className="tabular text-[10px] leading-tight text-muted-foreground">
        {summary.statusCounts
          .map((entry) => `${entry.count} ${entry.status}`)
          .join(" · ")}
      </span>
    </div>
  );
}

/** Shared by single rows and repeat groups so both read identically. */
function CollectorProblemCell({ count }: { count: number }) {
  if (count > 0) {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-medium text-status-warning">
        <TriangleAlert className="size-3" aria-hidden="true" />
        {count}
      </span>
    );
  }
  return <span className="text-xs text-muted-foreground">0</span>;
}

function ReportCell({ status }: { status: IncidentSummary["status"] }) {
  const availability = reportAvailability(status);
  if (availability === "AVAILABLE") {
    return <Badge tone="success">Report</Badge>;
  }
  if (availability === "NOT_AVAILABLE") {
    return <Badge tone="outline">None</Badge>;
  }
  return (
    <Badge tone="neutral" title="Pipeline has not reached REPORTED yet">
      Pending
    </Badge>
  );
}

function IncidentRow({
  item,
  selected,
  onSelect,
  nested = false,
}: {
  item: IncidentSummary;
  selected: boolean;
  onSelect: (incidentId: string) => void;
  nested?: boolean;
}) {
  const namespace = entityNamespace(item.source_entity);
  return (
    <TableRow
      data-selected={selected}
      // Selection survives a refresh: rows are keyed by Incident ID, so a poll
      // that inserts new Incidents does not reset the choice.
      onClick={() => onSelect(item.incident_id)}
      className={cn(selected && "bg-accent")}
    >
      <TableCell className={cn(nested && "pl-6")}>
        <SeverityBadge severity={item.severity} />
      </TableCell>
      <TableCell>
        <IncidentStatusBadge status={item.status} />
      </TableCell>
      <TableCell>
        <ReportCell status={item.status} />
      </TableCell>
      <TableCell className="max-w-[22rem]">
        <Link
          href={`/incidents/${item.incident_id}`}
          className="block truncate font-medium hover:underline"
          onClick={() => onSelect(item.incident_id)}
        >
          {item.alert_name}
        </Link>
        <span className="font-mono text-[11px] text-muted-foreground">
          {item.incident_id}
        </span>
      </TableCell>
      <TableCell className="max-w-[18rem] truncate">
        <EntityRefLabel entity={item.source_entity} showNamespace={false} />
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">{namespace ?? "—"}</TableCell>
      <TableCell className="tabular text-xs">{formatTimestamp(item.updated_at)}</TableCell>
      <TableCell className="tabular">
        <CollectorProblemCell count={item.collector_problem_count} />
      </TableCell>
    </TableRow>
  );
}

export function IncidentTableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {COLUMNS.map((column) => (
            <TableHead key={column} scope="col">
              {column}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {Array.from({ length: rows }, (_, index) => (
          <TableRow key={index}>
            {COLUMNS.map((column) => (
              <TableCell key={column}>
                <Skeleton className="h-4 w-full" />
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
