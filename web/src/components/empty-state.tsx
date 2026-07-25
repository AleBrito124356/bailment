import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * What a page looks like when there is genuinely nothing to show.
 *
 * A fresh install has no leases, no approvals and no reconcile history, and all three of
 * those screens have to look *finished* rather than broken. The rule this component
 * enforces: an empty state states the fact, explains why it is a normal fact, and offers
 * the next action. No shrugging illustrations, no "Oops!", no exclamation marks.
 */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 px-6 py-14 text-center",
        className,
      )}
    >
      {Icon ? (
        <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-muted/50">
          <Icon className="h-4 w-4 text-muted-foreground" />
        </div>
      ) : null}
      <div className="space-y-1.5">
        <p className="text-sm font-medium text-foreground">{title}</p>
        {description ? (
          <div className="mx-auto max-w-md text-sm leading-relaxed text-muted-foreground">
            {description}
          </div>
        ) : null}
      </div>
      {action ? <div className="pt-1">{action}</div> : null}
    </div>
  );
}
