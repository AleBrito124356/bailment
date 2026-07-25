"use client";

import { useCountdown } from "@/lib/clock";
import { formatCountdown } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * A ticking lease countdown.
 *
 * `seconds` is the broker's own `seconds_remaining`, and `anchorMs` is when that number
 * arrived (the query cache's `dataUpdatedAt`). The component subtracts elapsed local time
 * from the broker's number; it never compares a local clock to `expires_at`. See
 * src/lib/clock.ts for why that distinction is load-bearing rather than fussy.
 *
 * Overdue renders as a negative, in amber. A lease whose deadline passed two minutes ago
 * and whose resource is still up is a real state -- `EXPIRED` means "destroy this", not
 * "this is gone" -- and rounding it up to `00:00` would hide exactly the gap this project
 * exists to show.
 */
export function Countdown({
  seconds,
  anchorMs,
  warnBelow = 0,
  className,
}: {
  seconds: number | null | undefined;
  anchorMs: number;
  /** Below this many seconds the number turns amber. Usually the path's warn window. */
  warnBelow?: number;
  className?: string;
}) {
  const remaining = useCountdown(seconds, anchorMs);

  if (remaining === null) {
    return <span className={cn("text-muted-foreground", className)}>—</span>;
  }

  const overdue = remaining < 0;
  const warning = !overdue && warnBelow > 0 && remaining <= warnBelow;

  return (
    <span
      className={cn(
        "tabular",
        overdue || warning ? "text-warn" : "text-foreground",
        overdue && "font-medium",
        className,
      )}
      title={overdue ? "Past its deadline. The resource may still exist." : undefined}
    >
      {formatCountdown(remaining)}
    </span>
  );
}
