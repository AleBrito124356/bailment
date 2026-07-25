"use client";

import * as React from "react";
import { Check, Copy } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Copy a value that is safe to copy.
 *
 * Everything this button is ever given is public by construction: a lease id, an external
 * resource name, a `bailment://binding/...` reference, a shell command. There is no code
 * path in this dashboard that holds a credential, because there is no endpoint that
 * returns one -- so there is nothing here to be careful about beyond saying so.
 */
export function CopyButton({
  value,
  label = "Copy",
  className,
}: {
  value: string;
  label?: string;
  className?: string;
}) {
  const [copied, setCopied] = React.useState(false);

  React.useEffect(() => {
    if (!copied) return;
    const id = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(id);
  }, [copied]);

  return (
    <button
      type="button"
      aria-label={`${label}: ${value}`}
      onClick={() => {
        void navigator.clipboard
          .writeText(value)
          .then(() => setCopied(true))
          .catch(() => setCopied(false));
      }}
      className={cn(
        "inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted-foreground " +
          "transition-colors hover:bg-muted hover:text-foreground",
        className,
      )}
    >
      {copied ? (
        <Check className="h-3.5 w-3.5 text-ok" aria-hidden />
      ) : (
        <Copy className="h-3.5 w-3.5" aria-hidden />
      )}
    </button>
  );
}

/** A monospace value with a copy affordance. Used for ids, names and references. */
export function CopyableCode({
  value,
  className,
  title,
}: {
  value: string;
  className?: string;
  title?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      <code
        className="rounded bg-muted px-1.5 py-0.5 font-mono text-[12px] text-foreground"
        title={title ?? value}
      >
        {value}
      </code>
      <CopyButton value={value} />
    </span>
  );
}
