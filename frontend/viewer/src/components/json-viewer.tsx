"use client";

import * as React from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Collapsible reader for the structured `facts` and `details` objects.
 *
 * It renders whatever the API returned and nothing else — no lazy fetch, no
 * expansion of references. Values are printed as text, so nothing in the data
 * can be interpreted as markup.
 */
export function JsonViewer({
  value,
  defaultOpen = false,
  label = "facts",
  className,
}: {
  value: unknown;
  defaultOpen?: boolean;
  label?: string;
  className?: string;
}) {
  const [open, setOpen] = React.useState(defaultOpen);
  const contentId = React.useId();
  const isEmpty =
    value === null ||
    value === undefined ||
    (typeof value === "object" && Object.keys(value as object).length === 0);

  if (isEmpty) {
    return (
      <p className={cn("text-xs text-muted-foreground", className)}>
        No {label} were recorded.
      </p>
    );
  }

  return (
    <div className={cn("rounded border border-border", className)}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={() => setOpen((current) => !current)}
        className="flex w-full items-center gap-1.5 rounded-t px-2 py-1.5 text-left text-xs font-medium text-muted-foreground hover:bg-accent hover:text-foreground"
      >
        <ChevronRight
          aria-hidden="true"
          className={cn("size-3 transition-transform", open && "rotate-90")}
        />
        {label}
        <span className="ml-auto text-[11px] font-normal text-muted-foreground/70">
          {open ? "collapse" : "expand"}
        </span>
      </button>
      {open && (
        <pre
          id={contentId}
          className="scroll-x max-h-80 overflow-y-auto border-t border-border bg-surface-sunken px-3 py-2 font-mono text-[11px] leading-relaxed"
        >
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </div>
  );
}
