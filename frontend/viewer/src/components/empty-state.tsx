import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * The Viewer shows a great many "nothing here yet" states, and each one has to
 * explain *why* rather than imply something is broken. `tone` selects between a
 * neutral wait and a real problem.
 */
export function EmptyState({
  icon: Icon,
  title,
  description,
  tone = "neutral",
  children,
  className,
}: {
  icon: LucideIcon;
  title: string;
  description?: React.ReactNode;
  tone?: "neutral" | "warning" | "critical";
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed px-6 py-10 text-center",
        tone === "neutral" && "border-border bg-surface-sunken",
        tone === "warning" && "border-status-warning/40 bg-status-warning-surface",
        tone === "critical" && "border-status-critical/40 bg-status-critical-surface",
        className,
      )}
    >
      <Icon
        aria-hidden="true"
        className={cn(
          "size-5",
          tone === "neutral" && "text-muted-foreground",
          tone === "warning" && "text-status-warning",
          tone === "critical" && "text-status-critical",
        )}
      />
      <p className="text-sm font-medium">{title}</p>
      {description && (
        <div className="max-w-xl text-xs leading-relaxed text-muted-foreground">
          {description}
        </div>
      )}
      {children}
    </div>
  );
}
