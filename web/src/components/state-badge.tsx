"use client";

import { Badge } from "@/components/ui/badge";
import { Tooltip } from "@/components/ui/tooltip";
import { stateMeta } from "@/lib/states";
import { cn } from "@/lib/utils";

/**
 * The lease state, everywhere it appears, rendered the same way.
 *
 * The blurb behind it is the state machine's own docstring. Somebody who meets
 * `deprovisioning` for the first time in a table at 2am should be able to find out what it
 * means without leaving the row.
 */
export function StateBadge({
  state,
  className,
  showBlurb = true,
}: {
  state: string;
  className?: string;
  showBlurb?: boolean;
}) {
  const meta = stateMeta(state);
  const badge = (
    <Badge tone={meta.tone} dot className={cn("cursor-default", className)}>
      {meta.label}
    </Badge>
  );
  if (!showBlurb) return badge;
  return (
    <Tooltip content={meta.blurb}>
      <span className="inline-flex">{badge}</span>
    </Tooltip>
  );
}
