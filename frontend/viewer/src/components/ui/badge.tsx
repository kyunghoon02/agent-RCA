import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium leading-none whitespace-nowrap [&_svg]:size-3 [&_svg]:shrink-0",
  {
    variants: {
      tone: {
        neutral:
          "border-transparent bg-status-neutral-surface text-status-neutral",
        critical:
          "border-transparent bg-status-critical-surface text-status-critical",
        warning:
          "border-transparent bg-status-warning-surface text-status-warning",
        info: "border-transparent bg-status-info-surface text-status-info",
        success:
          "border-transparent bg-status-success-surface text-status-success",
        running:
          "border-transparent bg-status-running-surface text-status-running",
        outline: "border-border bg-transparent text-muted-foreground",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}

export { Badge, badgeVariants };
