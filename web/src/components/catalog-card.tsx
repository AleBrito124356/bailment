"use client";

import * as React from "react";
import { Lock, TriangleAlert } from "lucide-react";

import { RequestDialog } from "@/components/request-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Tooltip } from "@/components/ui/tooltip";
import { formatUsd } from "@/lib/format";
import type { CatalogEntry } from "@/lib/types";

/**
 * One golden path.
 *
 * The card shows the three things a person needs before they decide to ask: how long they
 * get it for, what it costs, and what comes back. Everything on it is read from the
 * catalog response -- there is no per-path copy in this file, because a card with
 * hand-written details for `postgres` is the first place the catalog and the dashboard
 * would drift.
 *
 * The first paragraph of the description is shown and the rest is not. The golden path
 * descriptions are written as instructions to a capable stranger and run to several
 * paragraphs; the first one says what the thing is, which is what a card is for.
 */
export function CatalogCard({ entry }: { entry: CatalogEntry }) {
  const [open, setOpen] = React.useState(false);
  const summary = entry.description.split("\n\n")[0] ?? entry.description;
  const sealed = entry.outputs.filter((output) => output.secret).length;
  const unavailable = !entry.provider_available;

  return (
    <>
      <Card className="flex flex-col">
        <div className="flex-1 space-y-3 p-5">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 space-y-1">
              <h3 className="text-[15px] font-semibold leading-snug tracking-[-0.01em]">
                {entry.name}
              </h3>
              <p className="font-mono text-2xs text-muted-foreground">{entry.id}</p>
            </div>
            <div className="flex shrink-0 flex-col items-end gap-1">
              <Badge tone="neutral">{entry.provider}</Badge>
              {!entry.enabled ? <Badge tone="warn">disabled</Badge> : null}
            </div>
          </div>

          <p className="text-sm leading-relaxed text-muted-foreground">{summary}</p>

          {entry.tags.length > 0 ? (
            <div className="flex flex-wrap gap-1">
              {entry.tags.map((tag) => (
                <span
                  key={tag}
                  className="rounded border border-border px-1.5 py-0.5 text-2xs text-muted-foreground"
                >
                  {tag}
                </span>
              ))}
            </div>
          ) : null}
        </div>

        <dl className="grid grid-cols-3 gap-px border-t border-border bg-border text-center">
          <Fact label="Default" value={entry.lease.default_ttl} />
          <Fact label="Ceiling" value={entry.lease.max_ttl} />
          <Fact
            label="Estimated"
            value={
              entry.cost.estimated_hourly_usd > 0
                ? `${formatUsd(entry.cost.estimated_hourly_usd)}/h`
                : "free"
            }
            hint={entry.cost.note}
          />
        </dl>

        <div className="flex items-center gap-3 border-t border-border p-4">
          <div className="min-w-0 flex-1 text-xs text-muted-foreground">
            {unavailable ? (
              <span className="flex items-start gap-1.5 text-warn">
                <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                <span>{entry.provider} is not configured on this broker</span>
              </span>
            ) : sealed > 0 ? (
              <span className="flex items-center gap-1.5">
                <Lock className="h-3.5 w-3.5 shrink-0" aria-hidden />
                <span>
                  {sealed} sealed {sealed === 1 ? "value" : "values"}, handed over as a reference
                </span>
              </span>
            ) : (
              <span>No sealed values on this path</span>
            )}
          </div>
          <Button variant="primary" size="sm" onClick={() => setOpen(true)}>
            Request
          </Button>
        </div>
      </Card>

      <RequestDialog entry={entry} open={open} onOpenChange={setOpen} />
    </>
  );
}

function Fact({ label, value, hint }: { label: string; value: string; hint?: string }) {
  const body = (
    <div className="bg-card px-2 py-2.5">
      <dt className="text-2xs uppercase tracking-wider text-muted-foreground">{label}</dt>
      <dd className="tabular mt-0.5 text-[13px] font-medium text-foreground">{value}</dd>
    </div>
  );
  return hint ? <Tooltip content={hint}>{body}</Tooltip> : body;
}
