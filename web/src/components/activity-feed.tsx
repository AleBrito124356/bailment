"use client";

import Link from "next/link";
import { ArrowRight, CircleCheck, CirclePlus, CircleSlash } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { StateBadge } from "@/components/state-badge";
import { Timestamp } from "@/components/timestamp";
import { SkeletonRows } from "@/components/ui/skeleton";
import { shortId } from "@/lib/format";
import type { Lease } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Recent activity, derived from lease timestamps rather than from an event stream.
 *
 * There is no installation-wide audit endpoint -- the trail is per lease, and deliberately
 * so: `/leases/{id}/audit` is scoped by the same visibility predicate as the lease itself,
 * and a global feed would need a second copy of that predicate, which is the failure mode
 * the broker's own pagination docstring is written about.
 *
 * So this feed is built from the three moments a lease row records on its face: when it
 * was requested, when it became active, and when its resource was confirmed gone. One
 * request, no fan-out, and every entry is a fact the API already told us. It is not a
 * complete history and it does not pretend to be one; the complete history of any lease is
 * one click away on its detail page.
 */

type Kind = "requested" | "activated" | "released";

interface Entry {
  key: string;
  at: string;
  kind: Kind;
  lease: Lease;
}

const KIND: Record<Kind, { verb: string; Icon: typeof CirclePlus; className: string }> = {
  requested: { verb: "requested", Icon: CirclePlus, className: "text-muted-foreground" },
  activated: { verb: "became active", Icon: CircleCheck, className: "text-ok" },
  released: { verb: "released", Icon: CircleSlash, className: "text-muted-foreground" },
};

function entriesFor(lease: Lease): Entry[] {
  const out: Entry[] = [];
  out.push({ key: `${lease.id}:requested`, at: lease.created_at, kind: "requested", lease });
  if (lease.activated_at) {
    out.push({ key: `${lease.id}:activated`, at: lease.activated_at, kind: "activated", lease });
  }
  if (lease.released_at) {
    out.push({ key: `${lease.id}:released`, at: lease.released_at, kind: "released", lease });
  }
  return out;
}

export function ActivityFeed({
  leases,
  loading = false,
  limit = 12,
}: {
  leases: Lease[] | undefined;
  loading?: boolean;
  limit?: number;
}) {
  if (loading && !leases) return <SkeletonRows rows={5} className="p-4" />;

  const entries = (leases ?? [])
    .flatMap(entriesFor)
    .sort((a, b) => Date.parse(b.at) - Date.parse(a.at))
    .slice(0, limit);

  if (entries.length === 0) {
    return (
      <EmptyState
        title="Nothing has happened yet"
        description="Activity appears here as leases are requested, activated and released. Take a sandbox lease from the catalog and the whole lifecycle runs in five minutes."
      />
    );
  }

  return (
    <ul className="divide-y divide-border">
      {entries.map((entry) => {
        const { verb, Icon, className } = KIND[entry.kind];
        return (
          <li key={entry.key}>
            <Link
              href={`/leases/${entry.lease.id}`}
              className="group flex items-center gap-3 px-4 py-2.5 transition-colors hover:bg-muted/40"
            >
              <Icon className={cn("h-3.5 w-3.5 shrink-0", className)} aria-hidden />

              <span className="min-w-0 flex-1 truncate text-sm">
                <span className="font-medium text-foreground">{entry.lease.golden_path}</span>
                <span className="text-muted-foreground"> {verb} by </span>
                <span className="text-foreground">{entry.lease.requester}</span>
                {entry.lease.on_behalf_of ? (
                  <span className="text-muted-foreground">
                    {" "}
                    for {entry.lease.on_behalf_of}
                  </span>
                ) : null}
              </span>

              {entry.kind === "requested" ? (
                <StateBadge state={entry.lease.state} showBlurb={false} />
              ) : null}

              <span className="hidden w-28 shrink-0 text-right text-xs text-muted-foreground sm:block">
                <Timestamp value={entry.at} />
              </span>

              <span className="hidden w-20 shrink-0 text-right font-mono text-2xs text-muted-foreground md:block">
                {shortId(entry.lease.id)}
              </span>

              <ArrowRight
                className="h-3.5 w-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100"
                aria-hidden
              />
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
