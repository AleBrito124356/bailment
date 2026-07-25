"use client";

import { Tooltip } from "@/components/ui/tooltip";
import { useSlowTick } from "@/lib/clock";
import { formatAbsolute, formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * "4 minutes ago", with the exact local timestamp on hover.
 *
 * Relative for scanning, absolute for citing in an incident. Both, because an operator
 * reading a timeline wants the first and an operator writing a post-mortem needs the
 * second, and making either one a second click is a small tax paid many times.
 *
 * The relative form is measured against the broker's clock, corrected by the offset
 * `/stats` reports. See src/lib/clock.ts.
 */
export function Timestamp({
  value,
  className,
  prefix,
}: {
  value: string | null | undefined;
  className?: string;
  prefix?: string;
}) {
  // Re-render every half minute so the page ages while it sits open.
  useSlowTick();

  if (!value) return <span className={cn("text-muted-foreground", className)}>—</span>;

  return (
    <Tooltip content={formatAbsolute(value)}>
      <time
        dateTime={value}
        className={cn("cursor-default underline decoration-dotted underline-offset-4", className)}
      >
        {prefix ? `${prefix} ` : ""}
        {formatRelative(value)}
      </time>
    </Tooltip>
  );
}
