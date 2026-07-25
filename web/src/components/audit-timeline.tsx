"use client";

import * as React from "react";
import { ChevronRight } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { Timestamp } from "@/components/timestamp";
import { SkeletonRows } from "@/components/ui/skeleton";
import { formatValue, humanise } from "@/lib/format";
import { stateTone, TONE_DOT, type Tone } from "@/lib/states";
import type { AuditEvent } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The append-only trail for one lease, oldest first.
 *
 * Oldest first because it is a story and stories start at the beginning -- the broker
 * returns it in that order for the same reason, and reversing it here would mean the
 * dashboard and the API disagree about what "the trail" looks like.
 *
 * **The detail blob is rendered, not summarised.** Every event carries a small JSON object
 * the workers wrote, and it is the part that answers "why". It has already been through the
 * broker's redactor twice -- once when it was written and once on the way out -- so what
 * arrives here is safe to show, and showing it beats paraphrasing it: the detail on a
 * `provision_failed` is the provider's own scrubbed message, and no summary this component
 * could write would be more useful than that sentence.
 */

const ACTION_TONE: Record<string, Tone> = {
  requested: "neutral",
  idempotent_replay: "neutral",
  policy_denied: "neutral",
  approval_requested: "info",
  approved: "ok",
  rejected: "neutral",
  approval_expired: "warn",
  queued: "neutral",
  claimed: "neutral",
  provisioning: "info",
  activated: "ok",
  provision_failed: "danger",
  rolled_back: "danger",
  expiring: "warn",
  expired: "neutral",
  renewed: "info",
  revoked: "neutral",
  teardown_started: "neutral",
  released: "neutral",
  orphaned: "warn",
  state_unknown: "warn",
  binding_resolved: "info",
  binding_revoked: "neutral",
  reconcile_drift: "warn",
  reconcile_orphan: "warn",
};

function toneFor(event: AuditEvent): Tone {
  const explicit = ACTION_TONE[event.action];
  if (explicit) return explicit;
  return event.to_state ? stateTone(event.to_state) : "neutral";
}

export function AuditTimeline({
  events,
  loading = false,
  className,
}: {
  events: AuditEvent[] | undefined;
  loading?: boolean;
  className?: string;
}) {
  if (loading && !events) return <SkeletonRows rows={4} className={className} />;

  if (!events || events.length === 0) {
    return (
      <EmptyState
        title="No events yet"
        description="Every transition this lease makes is appended here, including the ones nobody asked for."
      />
    );
  }

  return (
    <ol className={cn("relative space-y-0", className)}>
      {events.map((event, index) => (
        <AuditRow key={event.id} event={event} last={index === events.length - 1} />
      ))}
    </ol>
  );
}

function AuditRow({ event, last }: { event: AuditEvent; last: boolean }) {
  const [open, setOpen] = React.useState(false);
  const tone = toneFor(event);
  const entries = Object.entries(event.detail ?? {}).filter(
    ([, value]) => value !== null && value !== undefined && value !== "",
  );

  const moved = event.from_state && event.to_state && event.from_state !== event.to_state;

  return (
    <li className="relative flex gap-3 pl-1">
      <div className="relative flex w-3 shrink-0 justify-center">
        <span
          className={cn("z-10 mt-[7px] h-2 w-2 shrink-0 rounded-full ring-4 ring-card", TONE_DOT[tone])}
          aria-hidden
        />
        {last ? null : <span className="absolute top-3 h-full w-px bg-border" aria-hidden />}
      </div>

      <div className="min-w-0 flex-1 pb-4">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="text-sm font-medium text-foreground">{humanise(event.action)}</span>
          {moved ? (
            <span className="font-mono text-2xs text-muted-foreground">
              {event.from_state} → {event.to_state}
            </span>
          ) : null}
          <span className="text-xs text-muted-foreground">
            by {event.actor} · <Timestamp value={event.at} />
          </span>
        </div>

        {entries.length > 0 ? (
          <>
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
              className="mt-1 inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
            >
              <ChevronRight
                className={cn("h-3 w-3 transition-transform", open && "rotate-90")}
                aria-hidden
              />
              {open ? "Hide" : "Show"} {entries.length} {entries.length === 1 ? "field" : "fields"}
            </button>

            {open ? (
              <dl className="mt-1.5 grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-3 gap-y-1 rounded-md border border-border bg-muted/30 px-3 py-2 text-xs">
                {entries.map(([key, value]) => (
                  <React.Fragment key={key}>
                    <dt className="font-mono text-muted-foreground">{key}</dt>
                    <dd className="min-w-0 break-words font-mono text-foreground">
                      {formatValue(value)}
                    </dd>
                  </React.Fragment>
                ))}
              </dl>
            ) : null}
          </>
        ) : null}
      </div>
    </li>
  );
}
