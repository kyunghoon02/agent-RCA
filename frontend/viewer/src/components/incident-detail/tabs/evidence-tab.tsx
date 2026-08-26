"use client";

import * as React from "react";
import {
  CircleDashed,
  Database,
  EyeOff,
  LayoutGrid,
  Rows3,
  ShieldAlert,
  TriangleAlert,
} from "lucide-react";
import { EmptyState } from "@/components/empty-state";
import { EntityRefLabel } from "@/components/entity-ref";
import { JsonViewer } from "@/components/json-viewer";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  distinctValues,
  evidenceCompleteness,
  insufficiencyDetail,
  type EvidenceCompleteness,
} from "@/lib/evidence";
import { formatRatio, formatTimestamp, shortHash } from "@/lib/format";
import { entityNamespace } from "@/lib/lifecycle";
import type { EvidenceItem } from "@/lib/types";
import { cn } from "@/lib/utils";

const ALL = "__all__";

const COMPLETENESS_BADGE: Record<
  EvidenceCompleteness,
  { label: string; tone: "success" | "warning" | "critical" }
> = {
  complete: { label: "COMPLETE", tone: "success" },
  partial: { label: "PARTIAL", tone: "warning" },
  insufficient: { label: "INSUFFICIENT_DATA", tone: "critical" },
};

export function EvidenceTab({
  evidence,
  focusedEvidenceId,
}: {
  evidence: EvidenceItem[];
  focusedEvidenceId: string | null;
}) {
  const [layout, setLayout] = React.useState<"cards" | "table">("cards");
  const [source, setSource] = React.useState(ALL);
  const [kind, setKind] = React.useState(ALL);
  const [quality, setQuality] = React.useState(ALL);
  const [subject, setSubject] = React.useState("");

  const sources = React.useMemo(() => distinctValues(evidence, "source"), [evidence]);
  const kinds = React.useMemo(() => distinctValues(evidence, "kind"), [evidence]);

  const filtered = React.useMemo(
    () =>
      evidence.filter((item) => {
        if (source !== ALL && item.source !== source) return false;
        if (kind !== ALL && item.kind !== kind) return false;
        if (quality !== ALL && evidenceCompleteness(item) !== quality) return false;
        if (subject.trim()) {
          const needle = subject.trim().toLowerCase();
          const haystack = [
            item.subject.name,
            entityNamespace(item.subject) ?? "",
            item.evidence_id,
          ]
            .join(" ")
            .toLowerCase();
          if (!haystack.includes(needle)) return false;
        }
        return true;
      }),
    [evidence, source, kind, quality, subject],
  );

  // A deep link from the Report tab must not be hidden by a stale filter.
  React.useEffect(() => {
    if (!focusedEvidenceId) return;
    setSource(ALL);
    setKind(ALL);
    setQuality(ALL);
    setSubject("");
    const node = document.getElementById(`evidence-${focusedEvidenceId}`);
    node?.scrollIntoView({ block: "center" });
  }, [focusedEvidenceId]);

  if (evidence.length === 0) {
    return (
      <EmptyState
        icon={Database}
        title="No Evidence was stored for this Incident"
        description="Collection either has not run yet or produced nothing. The Overview tab shows each Provider's result and error code."
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <Card className="p-3">
        <div className="flex flex-wrap items-end gap-2">
          <FilterSelect
            id="evidence-source"
            label="Source"
            value={source}
            onChange={setSource}
            options={sources}
            allLabel="All sources"
          />
          <FilterSelect
            id="evidence-kind"
            label="Kind"
            value={kind}
            onChange={setKind}
            options={kinds}
            allLabel="All kinds"
          />
          <FilterSelect
            id="evidence-quality"
            label="Quality"
            value={quality}
            onChange={setQuality}
            options={["complete", "partial", "insufficient"]}
            allLabel="Any quality"
          />
          <div className="flex flex-col gap-1">
            <Label htmlFor="evidence-subject">Subject</Label>
            <Input
              id="evidence-subject"
              value={subject}
              placeholder="checkoutservice"
              onChange={(event) => setSubject(event.target.value)}
              className="w-52"
            />
          </div>

          <div className="ml-auto flex items-center gap-1">
            <Button
              variant={layout === "cards" ? "secondary" : "outline"}
              size="sm"
              aria-pressed={layout === "cards"}
              onClick={() => setLayout("cards")}
            >
              <LayoutGrid className="size-3.5" aria-hidden="true" />
              Cards
            </Button>
            <Button
              variant={layout === "table" ? "secondary" : "outline"}
              size="sm"
              aria-pressed={layout === "table"}
              onClick={() => setLayout("table")}
            >
              <Rows3 className="size-3.5" aria-hidden="true" />
              Table
            </Button>
          </div>
        </div>
        <p className="mt-2 text-[11px] text-muted-foreground">
          Showing {filtered.length} of {evidence.length} Evidence items.
        </p>
      </Card>

      {filtered.length === 0 ? (
        <EmptyState
          icon={CircleDashed}
          title="No Evidence matches these filters"
          description="Clear the source, kind, quality or subject filter to see the full set."
        />
      ) : layout === "cards" ? (
        <div className="flex flex-col gap-2">
          {filtered.map((item) => (
            <EvidenceCard
              key={item.evidence_id}
              item={item}
              focused={item.evidence_id === focusedEvidenceId}
            />
          ))}
        </div>
      ) : (
        <Card>
          <EvidenceTable items={filtered} focusedEvidenceId={focusedEvidenceId} />
        </Card>
      )}
    </div>
  );
}

function FilterSelect({
  id,
  label,
  value,
  onChange,
  options,
  allLabel,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (next: string) => void;
  options: string[];
  allLabel: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id}>{label}</Label>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger id={id} className="w-44">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>{allLabel}</SelectItem>
          {options.map((option) => (
            <SelectItem key={option} value={option}>
              {option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function CompletenessBadge({ item }: { item: EvidenceItem }) {
  const level = evidenceCompleteness(item);
  const presentation = COMPLETENESS_BADGE[level];
  return (
    <Badge tone={presentation.tone}>
      {level !== "complete" && <TriangleAlert aria-hidden="true" />}
      {presentation.label}
    </Badge>
  );
}

/**
 * States plainly why an Evidence item establishes less than it appears to,
 * including the series that were expected but never returned.
 */
function InsufficientDataPanel({ item }: { item: EvidenceItem }) {
  const { reason, detail, missingSeries, observed } = insufficiencyDetail(item);
  const level = evidenceCompleteness(item);
  return (
    <div
      className={cn(
        "rounded border px-2.5 py-2 text-xs",
        level === "insufficient"
          ? "border-status-critical/40 bg-status-critical-surface"
          : "border-status-warning/40 bg-status-warning-surface",
      )}
    >
      <p className="flex items-center gap-1.5 font-medium">
        <TriangleAlert className="size-3.5" aria-hidden="true" />
        {level === "insufficient"
          ? "Insufficient data — this Evidence supports no conclusion"
          : "Partial data — treat these values as a lower bound"}
      </p>
      <dl className="mt-1.5 flex flex-col gap-1">
        <div className="flex min-w-0 gap-2">
          <dt className="min-w-20 shrink-0 text-muted-foreground">Cause</dt>
          <dd className="min-w-0 break-words font-mono">{reason ?? "not reported"}</dd>
        </div>
        {detail && (
          <div className="flex min-w-0 gap-2">
            <dt className="min-w-20 shrink-0 text-muted-foreground">Detail</dt>
            <dd className="min-w-0 break-words">{detail}</dd>
          </div>
        )}
        {observed.length > 0 && (
          <div className="flex min-w-0 gap-2">
            <dt className="min-w-20 shrink-0 text-muted-foreground">Counts</dt>
            <dd className="tabular flex min-w-0 flex-wrap gap-x-3">
              {observed.map((entry) => (
                <span key={entry.label}>
                  {entry.label}: <strong>{entry.value}</strong>
                </span>
              ))}
            </dd>
          </div>
        )}
        {missingSeries.length > 0 && (
          <div className="flex min-w-0 gap-2">
            <dt className="min-w-20 shrink-0 text-muted-foreground">Missing series</dt>
            <dd className="min-w-0">
              <ul className="flex flex-col gap-0.5">
                {missingSeries.map((series) => (
                  <li key={series} className="scroll-x font-mono text-[11px]">
                    {series}
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        )}
      </dl>
    </div>
  );
}

function EvidenceCard({ item, focused }: { item: EvidenceItem; focused: boolean }) {
  const level = evidenceCompleteness(item);
  return (
    <Card
      id={`evidence-${item.evidence_id}`}
      className={cn("scroll-mt-24", focused && "ring-2 ring-ring")}
    >
      <CardContent className="flex flex-col gap-2 px-3 py-3">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <span className="max-w-full break-all font-mono text-xs font-medium">
            {item.evidence_id}
          </span>
          <Badge tone="outline">{item.source}</Badge>
          <Badge tone="outline">{item.kind}</Badge>
          <CompletenessBadge item={item} />
          {item.redactions.length > 0 && (
            <Badge tone="neutral" title="Fields removed before storage">
              <EyeOff aria-hidden="true" />
              {item.redactions.length} redacted
            </Badge>
          )}
          <span className="tabular ml-auto text-[11px] text-muted-foreground">
            observed {formatTimestamp(item.observed_at)}
          </span>
        </div>

        <EntityRefLabel entity={item.subject} />

        <p className="text-sm leading-relaxed">{item.summary}</p>

        {level !== "complete" && <InsufficientDataPanel item={item} />}

        <dl className="grid gap-x-6 gap-y-1 text-[11px] sm:grid-cols-2">
          <ProvenanceRow term="Provider" value={item.provenance.provider} />
          <ProvenanceRow
            term="Window"
            value={`${formatTimestamp(item.window.start)} → ${formatTimestamp(item.window.end)}`}
          />
          <ProvenanceRow term="Query" value={item.provenance.query} wrap />
          <ProvenanceRow term="Locator" value={item.provenance.locator} wrap />
          <ProvenanceRow term="Content hash" value={shortHash(item.provenance.content_hash)} />
          <ProvenanceRow
            term="Quality"
            value={`freshness ${item.quality.freshness} · completeness ${formatRatio(
              item.quality.completeness,
            )} · confidence ${formatRatio(item.quality.confidence)}`}
          />
        </dl>

        {item.redactions.length > 0 && (
          <div className="rounded border border-border bg-surface-sunken px-2.5 py-2">
            <p className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
              <ShieldAlert className="size-3" aria-hidden="true" />
              Redacted before storage — values are not retained and cannot be shown
            </p>
            <ul className="mt-1 flex flex-col gap-0.5">
              {item.redactions.map((path) => (
                <li key={path} className="scroll-x font-mono text-[11px]">
                  {path}
                </li>
              ))}
            </ul>
          </div>
        )}

        <JsonViewer value={item.facts} label="facts" />
      </CardContent>
    </Card>
  );
}

function ProvenanceRow({
  term,
  value,
  wrap = false,
}: {
  term: string;
  value: string;
  wrap?: boolean;
}) {
  return (
    <div className="flex min-w-0 items-baseline gap-2">
      <dt className="min-w-24 shrink-0 text-muted-foreground">{term}</dt>
      <dd className={cn("min-w-0 flex-1 font-mono", wrap ? "break-all" : "truncate")}>
        {value}
      </dd>
    </div>
  );
}

function EvidenceTable({
  items,
  focusedEvidenceId,
}: {
  items: EvidenceItem[];
  focusedEvidenceId: string | null;
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Evidence ID</TableHead>
          <TableHead scope="col">Source</TableHead>
          <TableHead scope="col">Kind</TableHead>
          <TableHead scope="col">Subject</TableHead>
          <TableHead scope="col">Observed</TableHead>
          <TableHead scope="col">Quality</TableHead>
          <TableHead scope="col">Summary</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((item) => (
          <TableRow
            key={item.evidence_id}
            id={`evidence-${item.evidence_id}`}
            className={cn(
              "scroll-mt-24",
              item.evidence_id === focusedEvidenceId && "bg-accent",
            )}
          >
            <TableCell className="font-mono text-xs">{item.evidence_id}</TableCell>
            <TableCell className="text-xs">{item.source}</TableCell>
            <TableCell className="text-xs">{item.kind}</TableCell>
            <TableCell className="max-w-56 truncate">
              <EntityRefLabel entity={item.subject} showNamespace={false} />
            </TableCell>
            <TableCell className="tabular text-[11px] text-muted-foreground">
              {formatTimestamp(item.observed_at)}
            </TableCell>
            <TableCell>
              <CompletenessBadge item={item} />
            </TableCell>
            <TableCell className="max-w-96 truncate text-xs">{item.summary}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
