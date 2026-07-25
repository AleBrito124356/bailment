"use client";

import Link from "next/link";

import { Timestamp } from "@/components/timestamp";
import { Badge } from "@/components/ui/badge";
import { formatDuration, shortId } from "@/lib/format";
import type { DriftRecord, OrphanRecord } from "@/lib/types";

/**
 * The two kinds of drift, rendered as findings rather than as alarms.
 *
 * An **orphan** is a resource that exists at a provider with no live lease behind it.
 * **Drift** is the other direction: a lease we believed was live whose resource has
 * vanished underneath us. Both are amber. Finding either one is the reconciler working, and
 * a screen that treats its own successful output as a failure teaches people to stop
 * reading it.
 *
 * Nothing here offers to destroy anything, and that is not an omission. Destroying an
 * orphan needs two switches thrown deliberately -- a global setting and a per-provider
 * allow-list -- and the API passes an empty allow-list unconditionally, whatever the
 * deployment is configured to do. The armed path is `bailment reconcile --destroy`, at a
 * terminal, with a confirmation that lists the resources by name. A tool that deletes cloud
 * resources because somebody was persuaded to click a button is a tool nobody installs
 * twice.
 */

export function OrphanList({ orphans }: { orphans: OrphanRecord[] }) {
  if (orphans.length === 0) return null;

  return (
    <div className="space-y-2">
      <p className="text-2xs font-semibold uppercase tracking-wider text-warn">
        {orphans.length} {orphans.length === 1 ? "orphan" : "orphans"}
      </p>
      <ul className="space-y-2">
        {orphans.map((orphan) => (
          <li
            key={`${orphan.provider}:${orphan.provider_resource_id}`}
            className="rounded-md border border-warn/30 bg-warn-soft/60 p-3"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[13px] font-medium text-foreground">
                {orphan.external_name}
              </span>
              <Badge tone="neutral">{orphan.provider}</Badge>
              {orphan.lease_state ? (
                <Badge tone="neutral">lease {orphan.lease_state}</Badge>
              ) : (
                <Badge tone="warn">no lease at all</Badge>
              )}
              {orphan.destroyed ? <Badge tone="ok">destroyed</Badge> : null}
            </div>

            <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
              {orphan.reason}
            </p>

            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-muted-foreground">
              <span className="font-mono">{orphan.provider_resource_id}</span>
              {orphan.age_seconds !== null ? (
                <span>alive for {formatDuration(orphan.age_seconds)}</span>
              ) : null}
              {orphan.created_at ? (
                <span>
                  created <Timestamp value={orphan.created_at} />
                </span>
              ) : null}
              {orphan.lease_id ? (
                <Link
                  href={`/leases/${orphan.lease_id}`}
                  className="font-mono text-accent hover:underline"
                >
                  {shortId(orphan.lease_id)}
                </Link>
              ) : null}
            </div>

            {orphan.destroy_error ? (
              <p className="mt-1.5 text-2xs text-danger">
                Destroy attempt failed: {orphan.destroy_error}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function DriftList({ drift }: { drift: DriftRecord[] }) {
  if (drift.length === 0) return null;

  return (
    <div className="space-y-2">
      <p className="text-2xs font-semibold uppercase tracking-wider text-warn">
        {drift.length} {drift.length === 1 ? "drifted lease" : "drifted leases"}
      </p>
      <ul className="space-y-2">
        {drift.map((record) => (
          <li key={record.lease_id} className="rounded-md border border-border bg-card p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[13px] font-medium text-foreground">
                {record.external_name}
              </span>
              <Badge tone="neutral">{record.provider}</Badge>
              <span className="font-mono text-2xs text-muted-foreground">
                {record.from_state} → {record.to_state}
              </span>
              <Link
                href={`/leases/${record.lease_id}`}
                className="font-mono text-2xs text-accent hover:underline"
              >
                {shortId(record.lease_id)}
              </Link>
            </div>
            <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{record.note}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * "We could not tell" is not "there was nothing there".
 *
 * A provider that could not be reached reports zero orphans, and a dashboard that cannot
 * tell that apart from a clean account is a dashboard that is reassuring and wrong.
 */
export function UnknownList({ names }: { names: string[] }) {
  if (names.length === 0) return null;
  return (
    <div className="rounded-md border border-warn/30 bg-warn-soft/60 p-3">
      <p className="text-2xs font-semibold uppercase tracking-wider text-warn">
        {names.length} of unknown status
      </p>
      <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
        These could not be determined, so this run is not clean. Nothing is assumed released.
      </p>
      <p className="mt-1.5 break-words font-mono text-2xs text-muted-foreground">
        {names.join(", ")}
      </p>
    </div>
  );
}
