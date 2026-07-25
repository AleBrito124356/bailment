"use client";

import * as React from "react";
import Link from "next/link";
import { ChevronRight, ExternalLink, ScrollText } from "lucide-react";

import { AuditTimeline } from "@/components/audit-timeline";
import { Countdown } from "@/components/countdown";
import { EmptyState } from "@/components/empty-state";
import { ErrorState } from "@/components/error-state";
import { PageHeader } from "@/components/page-header";
import { StateBadge } from "@/components/state-badge";
import { Timestamp } from "@/components/timestamp";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Select } from "@/components/ui/field";
import { SkeletonRows } from "@/components/ui/skeleton";
import { Tooltip } from "@/components/ui/tooltip";
import { formatUsd, shortId } from "@/lib/format";
import { useAudit, useCatalog, useLeases } from "@/lib/queries";
import { STATE_FILTER_GROUPS } from "@/lib/states";
import { LIVE_STATES, TERMINAL_STATES, type Lease, type LeaseState } from "@/lib/types";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 25;

/**
 * Every lease, filterable, with a live countdown on the ones that are still running.
 *
 * Filters are component state rather than URL state. Reading them from `useSearchParams`
 * would be nicer to share, and it forces every page that does it into a Suspense boundary
 * or out of static rendering entirely; for a screen that is polled every ten seconds
 * anyway, a shareable URL is not worth that.
 *
 * The row expands rather than navigating. "What happened to this one" is usually a
 * ten-second question asked of four leases in a row, and answering it by pushing a route
 * and losing the filter set is how a dashboard makes somebody use the API instead.
 */
export default function LeasesPage() {
  const [states, setStates] = React.useState<LeaseState[]>([]);
  const [goldenPath, setGoldenPath] = React.useState("");
  const [provider, setProvider] = React.useState("");
  const [liveOnly, setLiveOnly] = React.useState(false);
  const [offset, setOffset] = React.useState(0);

  const catalog = useCatalog();
  const filters = React.useMemo(
    () => ({
      state: states.length > 0 ? states : undefined,
      golden_path: goldenPath || undefined,
      provider: provider || undefined,
      live: liveOnly || undefined,
      limit: PAGE_SIZE,
      offset,
    }),
    [states, goldenPath, provider, liveOnly, offset],
  );

  const leases = useLeases(filters);
  const items = leases.data?.items ?? [];

  const providers = React.useMemo(() => {
    const names = new Set((catalog.data?.items ?? []).map((entry) => entry.provider));
    return [...names].sort();
  }, [catalog.data]);

  function toggleState(state: LeaseState) {
    setOffset(0);
    setStates((current) =>
      current.includes(state) ? current.filter((item) => item !== state) : [...current, state],
    );
  }

  const filtered = states.length > 0 || goldenPath || provider || liveOnly;

  return (
    <>
      <PageHeader
        title="Leases"
        description="Every request this broker has accepted, in whatever state it reached. Countdowns are the broker's own remaining seconds, ticking locally — not this machine's clock against an expiry date."
        actions={
          <Button asChild variant="primary" size="sm">
            <Link href="/catalog">Request a lease</Link>
          </Button>
        }
      />

      <Card className="mb-4 space-y-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <Tooltip
            content={`Leases in a state where a real resource is believed to exist, and therefore to cost money: ${LIVE_STATES.join(", ")}. Orphaned and unknown are in that list deliberately — excluding them would report what bailment intended rather than what is running.`}
          >
            <button
              type="button"
              onClick={() => {
                setOffset(0);
                setLiveOnly((value) => !value);
              }}
              className={cn(
                "rounded-md border px-2.5 py-1 text-[13px] font-medium transition-colors",
                liveOnly
                  ? "border-accent bg-accent-soft text-accent"
                  : "border-border text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              Live resources only
            </button>
          </Tooltip>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Select
              aria-label="Golden path"
              value={goldenPath}
              className="h-8 w-40 text-[13px]"
              onChange={(event) => {
                setOffset(0);
                setGoldenPath(event.target.value);
              }}
            >
              <option value="">All golden paths</option>
              {(catalog.data?.items ?? []).map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.id}
                </option>
              ))}
            </Select>

            <Select
              aria-label="Provider"
              value={provider}
              className="h-8 w-36 text-[13px]"
              onChange={(event) => {
                setOffset(0);
                setProvider(event.target.value);
              }}
            >
              <option value="">All providers</option>
              {providers.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </Select>
          </div>
        </div>

        <div className="scroll-x -mx-1 px-1">
          <div className="flex min-w-max items-center gap-4 pb-0.5">
            {STATE_FILTER_GROUPS.map((group) => (
              <div key={group.label} className="flex items-center gap-1.5">
                <span className="text-2xs uppercase tracking-wider text-muted-foreground">
                  {group.label}
                </span>
                {group.states.map((state) => {
                  const active = states.includes(state);
                  return (
                    <button
                      key={state}
                      type="button"
                      onClick={() => toggleState(state)}
                      aria-pressed={active}
                      className={cn(
                        "rounded-full border px-2 py-0.5 text-2xs font-medium transition-colors",
                        active
                          ? "border-accent bg-accent-soft text-accent"
                          : "border-border text-muted-foreground hover:bg-muted hover:text-foreground",
                      )}
                    >
                      {state}
                    </button>
                  );
                })}
              </div>
            ))}
            {filtered ? (
              <button
                type="button"
                onClick={() => {
                  setStates([]);
                  setGoldenPath("");
                  setProvider("");
                  setLiveOnly(false);
                  setOffset(0);
                }}
                className="text-2xs font-medium text-accent hover:underline"
              >
                Clear
              </button>
            ) : null}
          </div>
        </div>
      </Card>

      {leases.isError ? (
        <ErrorState error={leases.error} what="leases" onRetry={() => void leases.refetch()} />
      ) : leases.isPending ? (
        <SkeletonRows rows={6} />
      ) : items.length === 0 ? (
        <Card>
          <EmptyState
            icon={ScrollText}
            title={filtered ? "No leases match those filters" : "No leases yet"}
            description={
              filtered
                ? "Nothing in this installation is in that combination of states right now."
                : "A lease appears the moment anything asks for one — this dashboard, an agent over MCP, or a CI system speaking Open Service Broker."
            }
            action={
              filtered ? (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    setStates([]);
                    setGoldenPath("");
                    setProvider("");
                    setLiveOnly(false);
                  }}
                >
                  Clear filters
                </Button>
              ) : (
                <Button asChild variant="primary" size="sm">
                  <Link href="/catalog">Open the catalog</Link>
                </Button>
              )
            }
          />
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <div className="scroll-x">
            <table className="w-full min-w-[860px] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border text-left">
                  <Th className="w-8" />
                  <Th>State</Th>
                  <Th>Golden path</Th>
                  <Th>Requester</Th>
                  <Th className="text-right">Remaining</Th>
                  <Th className="text-right">Cost/h</Th>
                  <Th>Requested</Th>
                  <Th className="text-right">Lease</Th>
                </tr>
              </thead>
              <tbody>
                {items.map((lease) => (
                  <LeaseRow
                    key={lease.id}
                    lease={lease}
                    anchorMs={leases.dataUpdatedAt}
                  />
                ))}
              </tbody>
            </table>
          </div>

          <div className="flex items-center justify-between border-t border-border px-4 py-2.5">
            <span className="text-xs text-muted-foreground">
              Showing {offset + 1}–{offset + items.length}
              {leases.data?.has_more ? "" : " (end)"}
            </span>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                size="sm"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Previous
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={!leases.data?.has_more}
                onClick={() => setOffset(leases.data?.next_offset ?? offset + PAGE_SIZE)}
              >
                Next
              </Button>
            </div>
          </div>
        </Card>
      )}
    </>
  );
}

function Th({ className, children }: { className?: string; children?: React.ReactNode }) {
  return (
    <th
      scope="col"
      className={cn(
        "px-3 py-2 text-2xs font-semibold uppercase tracking-wider text-muted-foreground",
        className,
      )}
    >
      {children}
    </th>
  );
}

function LeaseRow({ lease, anchorMs }: { lease: Lease; anchorMs: number }) {
  const [open, setOpen] = React.useState(false);

  return (
    <>
      <tr
        className={cn(
          "border-b border-border transition-colors hover:bg-muted/40",
          open && "bg-muted/30",
        )}
      >
        <td className="pl-3">
          <button
            type="button"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
            aria-label={open ? "Hide the audit trail" : "Show the audit trail"}
            className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <ChevronRight
              className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-90")}
              aria-hidden
            />
          </button>
        </td>
        <td className="px-3 py-2.5">
          <StateBadge state={lease.state} />
        </td>
        <td className="px-3 py-2.5">
          <span className="font-medium text-foreground">{lease.golden_path}</span>
          <span className="ml-2 text-xs text-muted-foreground">{lease.provider}</span>
        </td>
        <td className="max-w-[180px] truncate px-3 py-2.5 text-muted-foreground">
          {lease.requester}
          {lease.on_behalf_of ? (
            <span className="block truncate text-2xs">for {lease.on_behalf_of}</span>
          ) : null}
        </td>
        <td className="px-3 py-2.5 text-right">
          {/*
            A finished lease keeps its `expires_at`, so the broker keeps computing a
            remaining figure for it -- deeply negative and meaningless. Counting down
            towards nothing on a released row would be noise; on an expired or orphaned one
            it is the point, so those keep their clock.
          */}
          <Countdown
            seconds={TERMINAL_STATES.includes(lease.state) ? null : lease.seconds_remaining}
            anchorMs={anchorMs}
            warnBelow={1800}
          />
        </td>
        <td className="tabular px-3 py-2.5 text-right text-muted-foreground">
          {lease.estimated_hourly_usd > 0 ? formatUsd(lease.estimated_hourly_usd) : "—"}
        </td>
        <td className="px-3 py-2.5 text-muted-foreground">
          <Timestamp value={lease.created_at} />
        </td>
        <td className="px-3 py-2.5 text-right">
          <Link
            href={`/leases/${lease.id}`}
            className="inline-flex items-center gap-1 font-mono text-2xs text-accent hover:underline"
          >
            {shortId(lease.id)}
            <ExternalLink className="h-3 w-3" aria-hidden />
          </Link>
        </td>
      </tr>

      {open ? (
        <tr className="border-b border-border bg-muted/20">
          <td colSpan={8} className="px-5 py-4">
            <RowDetails lease={lease} />
          </td>
        </tr>
      ) : null}
    </>
  );
}

/**
 * Only mounted when the row is open, so opening one row is one request rather than the
 * table being an N+1 fan-out on every poll.
 */
function RowDetails({ lease }: { lease: Lease }) {
  const audit = useAudit(lease.id);

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,18rem)_minmax(0,1fr)]">
      <div className="space-y-3">
        <div>
          <p className="text-2xs font-semibold uppercase tracking-wider text-muted-foreground">
            Inputs
          </p>
          {Object.keys(lease.inputs).length === 0 ? (
            <p className="mt-1 text-sm text-muted-foreground">This path takes no arguments.</p>
          ) : (
            <dl className="mt-1.5 space-y-1 text-sm">
              {Object.entries(lease.inputs).map(([key, value]) => (
                <div key={key} className="flex gap-2">
                  <dt className="font-mono text-xs text-muted-foreground">{key}</dt>
                  <dd className="min-w-0 break-words font-mono text-xs text-foreground">
                    {typeof value === "string" ? value : JSON.stringify(value)}
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </div>

        {lease.secret_output_names.length > 0 ? (
          <div>
            <p className="text-2xs font-semibold uppercase tracking-wider text-muted-foreground">
              Sealed values
            </p>
            <div className="mt-1.5 flex flex-wrap gap-1">
              {lease.secret_output_names.map((name) => (
                <Badge key={name} tone="neutral" className="font-mono">
                  {name}
                </Badge>
              ))}
            </div>
          </div>
        ) : null}

        {lease.failure_reason ? (
          <div>
            <p className="text-2xs font-semibold uppercase tracking-wider text-muted-foreground">
              Failure
            </p>
            <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
              {lease.failure_reason}
            </p>
          </div>
        ) : null}

        <Button asChild variant="secondary" size="sm">
          <Link href={`/leases/${lease.id}`}>Open the lease</Link>
        </Button>
      </div>

      <div>
        <p className="mb-2 text-2xs font-semibold uppercase tracking-wider text-muted-foreground">
          Audit trail
        </p>
        {audit.isError ? (
          <ErrorState error={audit.error} what="the audit trail" />
        ) : (
          <AuditTimeline events={audit.data?.items} loading={audit.isPending} />
        )}
      </div>
    </div>
  );
}
