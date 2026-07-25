import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * A loading placeholder, shaped like the thing it is standing in for.
 *
 * Deliberately not a spinner. A spinner says "wait"; a skeleton says "here is a table, and
 * it has about four rows", which is the difference between a page that feels broken for
 * 300ms and one that does not.
 */
export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("animate-pulse rounded-md bg-muted", className)} {...props} />;
}

export function SkeletonRows({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("space-y-2", className)}>
      {Array.from({ length: rows }).map((_, index) => (
        <Skeleton key={index} className="h-11 w-full" />
      ))}
    </div>
  );
}
