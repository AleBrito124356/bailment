import * as React from "react";

import { TONE_BADGE, TONE_DOT, type Tone } from "@/lib/states";
import { cn } from "@/lib/utils";

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
  /** A solid dot before the label, so the badge is legible without relying on hue alone. */
  dot?: boolean;
}

/**
 * A quiet pill with a 1px ring rather than a border, so a row of them does not read as a
 * row of buttons. The dot is not decoration: it carries the state for anyone who cannot
 * separate the background tints, and it survives a screenshot pasted into a ticket.
 */
export function Badge({ tone = "neutral", dot = false, className, children, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium " +
          "ring-1 ring-inset",
        TONE_BADGE[tone],
        className,
      )}
      {...props}
    >
      {dot ? (
        <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", TONE_DOT[tone])} aria-hidden />
      ) : null}
      {children}
    </span>
  );
}
