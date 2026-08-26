"use client";

import Link from "next/link";
import { TriangleAlert } from "lucide-react";
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
import { entityNamespace } from "@/lib/lifecycle";
import type { IncidentSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

const COLUMNS = [
  "Severity",
  "Status",
  "Alert",
  "Source entity",
  "Namespace",
  "Triggered",
  "Updated",
  "Collector problems",
];

export function IncidentTable({
  items,
  selectedId,
  onSelect,
}: {
  items: IncidentSummary[];
  selectedId: string | null;
  onSelect: (incidentId: string) => void;
}) {
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
        {items.map((item) => {
          const namespace = entityNamespace(item.source_entity);
          const selected = item.incident_id === selectedId;
          return (
            <TableRow
              key={item.incident_id}
              data-selected={selected}
              // Selection survives a refresh: rows are keyed by Incident ID, so
              // a poll that inserts new Incidents does not reset the choice.
              onClick={() => onSelect(item.incident_id)}
              className={cn(selected && "bg-accent")}
            >
              <TableCell>
                <SeverityBadge severity={item.severity} />
              </TableCell>
              <TableCell>
                <IncidentStatusBadge status={item.status} />
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
              <TableCell className="text-xs text-muted-foreground">
                {namespace ?? "—"}
              </TableCell>
              <TableCell className="tabular text-xs text-muted-foreground">
                {formatTimestamp(item.triggered_at)}
              </TableCell>
              <TableCell className="tabular text-xs">
                {formatTimestamp(item.updated_at)}
              </TableCell>
              <TableCell className="tabular">
                {item.collector_problem_count > 0 ? (
                  <span className="inline-flex items-center gap-1 text-xs font-medium text-status-warning">
                    <TriangleAlert className="size-3" aria-hidden="true" />
                    {item.collector_problem_count}
                  </span>
                ) : (
                  <span className="text-xs text-muted-foreground">0</span>
                )}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
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
