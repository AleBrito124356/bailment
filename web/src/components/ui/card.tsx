import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * A white surface on the page tint, separated by a one-pixel zinc border.
 *
 * No shadow. Shadows imply elevation, elevation implies a stack, and a dashboard where
 * everything floats has no hierarchy at all -- just a lot of soft edges. The two things
 * that genuinely float (the request dialog, the token popover) carry `shadow-overlay` and
 * are the only things that do.
 */
export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("rounded-lg border border-border bg-card text-card-foreground", className)}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-1 p-5", className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h3
      className={cn("text-[15px] font-semibold leading-none tracking-[-0.01em]", className)}
      {...props}
    />
  );
}

export function CardDescription({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-sm leading-relaxed text-muted-foreground", className)} {...props} />;
}

export function CardContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-5 pt-0", className)} {...props} />;
}

export function CardFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("flex items-center gap-2 border-t border-border bg-muted/30 p-4", className)}
      {...props}
    />
  );
}
