"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";

/**
 * Cursor pagination.
 *
 * The query contract issues opaque forward cursors bound to the active filters,
 * so there is no page count and no jumping to page N. Going back replays the
 * cursor stack this component's owner keeps.
 */
export function CursorPagination({
  page,
  loadedCount,
  hasPrevious,
  hasNext,
  onPrevious,
  onNext,
  disabled,
}: {
  page: number;
  loadedCount: number;
  hasPrevious: boolean;
  hasNext: boolean;
  onPrevious: () => void;
  onNext: () => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border-t border-border px-1 py-2">
      <p className="text-xs text-muted-foreground">
        Page <span className="tabular font-medium text-foreground">{page}</span> ·{" "}
        <span className="tabular">{loadedCount}</span> Incident
        {loadedCount === 1 ? "" : "s"} on this page
        {!hasNext && page > 1 ? " · end of results" : ""}
      </p>
      <div className="flex items-center gap-1.5">
        <Button
          variant="outline"
          size="sm"
          onClick={onPrevious}
          disabled={disabled || !hasPrevious}
        >
          <ChevronLeft className="size-3.5" aria-hidden="true" />
          Previous
        </Button>
        <Button variant="outline" size="sm" onClick={onNext} disabled={disabled || !hasNext}>
          Next
          <ChevronRight className="size-3.5" aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
}
