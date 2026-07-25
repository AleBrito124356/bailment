"use client";

import type * as React from "react";
import Link from "next/link";

import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip } from "@/components/ui/tooltip";
import type { Tone } from "@/lib/states";
import { cn } from "@/lib/utils";

const VALUE_TONE: Record<Tone, string> = {
  neutral: "text-foreground",
  info: "text-foreground",
  ok: "text-foreground",
  warn: "text-warn",
  danger: "text-danger",
};

const RING_TONE: Record<Tone, string> = {
  neutral: "border-border",
  info: "border-border",
  ok: "border-border",
  warn: "border-warn/35",
  danger: "border-danger/35",
};

/**
 * One number, said plainly.
 *
 * The tone is a property of the *value*, not of the tile: "orphans outstanding" is zinc
 * when it reads zero and amber when it does not, and the tile it lives in does not change
 * shape when that happens. A layout that rearranges itself when something goes wrong is a
 * layout an operator has to re-read under exactly the conditions where they should not
 * have to.
 */
export function StatTile({
  label,
  value,
  tone = "neutral",
  sub,
  hint,
  href,
  loading = false,
  className,
}: {
  label: string;
  value: React.ReactNode;
  tone?: Tone;
  sub?: React.ReactNode;
  /** The sentence explaining what the number counts, on hover. */
  hint?: React.ReactNode;
  href?: string;
  loading?: boolean;
  className?: string;
}) {
  const body = (
    <>
      <span className="text-[13px] font-medium text-muted-foreground">{label}</span>
      {loading ? (
        <Skeleton className="mt-2 h-8 w-20" />
      ) : (
        <span
          className={cn(
            "tabular mt-1.5 block text-[28px] font-semibold leading-none tracking-[-0.02em]",
            VALUE_TONE[tone],
          )}
        >
          {value}
        </span>
      )}
      {sub ? <span className="mt-2 block text-xs text-muted-foreground">{sub}</span> : null}
    </>
  );

  const classes = cn(
    "block rounded-lg border bg-card p-4 transition-colors",
    RING_TONE[tone],
    href && "hover:bg-muted/40",
    className,
  );

  const tile = href ? (
    <Link href={href} className={classes}>
      {body}
    </Link>
  ) : (
    <div className={classes}>{body}</div>
  );

  return hint ? <Tooltip content={hint}>{tile}</Tooltip> : tile;
}
