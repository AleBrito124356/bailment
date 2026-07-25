"use client";

import Link from "next/link";
import { ClipboardCheck, RefreshCcw } from "lucide-react";

import { ActivityFeed } from "@/components/activity-feed";
import { ErrorState } from "@/components/error-state";
import { PageHeader, Section } from "@/components/page-header";
import { StatTile } from "@/components/stat-tile";
import { Timestamp } from "@/components/timestamp";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { formatCount, formatDuration, formatUsd } from "@/lib/format";
import { useLeases, useStats } from "@/lib/queries";
import type { ReconcileRun } from "@/lib/types";

/**
 * The overview.
 *
 * Four numbers, and the third one is the reason this page is laid out the way it is.
 * "Orphans outstanding" is the finding no other broker in this category produces: a
 * resource that exists at a provider with no live lease behind it. It gets the same tile as
 * everything else -- no siren, no red -- and turns amber when it is not zero. Amber says
 * "somebody needs to decide something", which is true. Red would say "the system is
 * broken", which is the opposite of true: an orphan on this screen means the reconciler
 * did its job.
 */
export default function OverviewPage() {
  const stats = useStats();
  const recent = useLeases({ limit: 30 });

  const data = stats.data;
  const orphans = data?.orphans_outstanding ?? 0;
  const scopeNote =
    data?.scope === "own"
      ? "Scoped to leases you requested. An operator token shows the whole installation."
      : "Every lease in this installation.";

  return (
    <>
      <PageHeader
        title="Overview"
        description={
          stats.isSuccess
            ? scopeNote
            : "Leases, spend, and anything the reconciler found that nobody is holding a lease for."
        }
        actions={
          <Button asChild variant="primary" size="sm">
            <Link href="/catalog">Request a lease</Link>
          </Button>
        }
      />

      {stats.isError ? (
        <ErrorState
          error={stats.error}
          what="the overview"
          onRetry={() => void stats.refetch()}
        />
      ) : (
        <div className="space-y-8">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile
              label="Active leases"
              value={formatCount(data?.active_leases ?? 0)}
              loading={stats.isPending}
              href="/leases"
              sub="Usable right now"
              hint="Leases in ACTIVE or EXPIRING: the provider has confirmed a resource exists and a consumer may still inject its binding."
            />
            <StatTile
              label="Expiring within the hour"
              value={formatCount(data?.expiring_soon ?? 0)}
              loading={stats.isPending}
              href="/leases"
              sub={
                data
                  ? `Window: ${formatDuration(data.expiring_within_seconds)}`
                  : "Window: 1h"
              }
              hint="Usable leases whose deadline falls inside the window. They are still fully usable; renewing one extends it from now, not from the old deadline."
            />
            <StatTile
              label="Orphans outstanding"
              value={formatCount(orphans)}
              tone={orphans > 0 ? "warn" : "neutral"}
              loading={stats.isPending}
              href="/reconcile"
              sub={orphans > 0 ? "Resources with no live lease" : "Nothing unaccounted for"}
              hint="Leases in ORPHANED: a real resource is believed to exist that no live lease accounts for, either because a destroy kept failing or because the reconciler found it at the provider. Destroying one is a deliberate act at a terminal, never a button here."
            />
            <StatTile
              label="Estimated live spend"
              value={data ? `${formatUsd(data.estimated_hourly_usd)}` : "—"}
              loading={stats.isPending}
              sub={data ? `per hour · ${formatUsd(data.estimated_monthly_usd)} per month` : undefined}
              hint="Summed over every lease in a state where a real resource is believed to exist — including ORPHANED and UNKNOWN. Excluding those would report what bailment intended rather than what is running."
            />
          </div>

          <div className="grid gap-6 lg:grid-cols-3">
            <Section
              title="Recent activity"
              description="Derived from lease timestamps. The full trail lives on each lease."
              className="lg:col-span-2"
              actions={
                <Button asChild variant="ghost" size="sm">
                  <Link href="/leases">All leases</Link>
                </Button>
              }
            >
              <Card className="overflow-hidden">
                {recent.isError ? (
                  <ErrorState
                    error={recent.error}
                    what="recent activity"
                    onRetry={() => void recent.refetch()}
                    className="border-0"
                  />
                ) : (
                  <ActivityFeed leases={recent.data?.items} loading={recent.isPending} />
                )}
              </Card>
            </Section>

            <div className="space-y-6">
              <ApprovalsCallout count={data?.awaiting_approval ?? 0} loading={stats.isPending} />
              <ReconcileCallout run={data?.last_reconcile ?? null} />
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function ApprovalsCallout({ count, loading }: { count: number; loading: boolean }) {
  return (
    <Section title="Approvals">
      <Card className="p-4">
        <div className="flex items-start gap-3">
          <ClipboardCheck className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
          <div className="min-w-0 flex-1 space-y-2">
            {loading ? (
              <p className="text-sm text-muted-foreground">Checking the queue…</p>
            ) : count > 0 ? (
              <p className="text-sm leading-relaxed text-foreground">
                <span className="tabular font-semibold">{count}</span>{" "}
                {count === 1 ? "request is" : "requests are"} waiting on a human. Nothing has
                been provisioned for them.
              </p>
            ) : (
              <p className="text-sm leading-relaxed text-muted-foreground">
                Nothing is waiting on a human. Requests that need one appear here the moment
                policy says so.
              </p>
            )}
            <Button asChild variant={count > 0 ? "primary" : "secondary"} size="sm">
              <Link href="/approvals">{count > 0 ? "Review the queue" : "Open the queue"}</Link>
            </Button>
          </div>
        </div>
      </Card>
    </Section>
  );
}

/**
 * The last stored sweep, which `/stats` returns for operators only. A non-operator sees
 * the "nothing recorded" copy, which is honest: from where they are standing, there is
 * nothing recorded that they may read.
 */
function ReconcileCallout({ run }: { run: ReconcileRun | null }) {
  return (
    <Section title="Reconciliation">
      <Card className="p-4">
        <div className="flex items-start gap-3">
          <RefreshCcw className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
          <div className="min-w-0 flex-1 space-y-2">
            {run ? (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={run.clean ? "neutral" : "warn"} dot>
                    {run.clean ? "Clean" : "Findings"}
                  </Badge>
                  <span className="text-sm text-muted-foreground">
                    {run.provider} · <Timestamp value={run.started_at} />
                  </span>
                </div>
                <p className="text-sm leading-relaxed text-muted-foreground">
                  {formatCount(run.resources_seen)} resources seen,{" "}
                  {formatCount(run.orphans_found)} orphaned,{" "}
                  {formatCount(run.drift_found)} drifted.
                  {run.status_unknown > 0
                    ? ` ${formatCount(run.status_unknown)} could not be determined — that is not the same as clean.`
                    : ""}
                </p>
              </>
            ) : (
              <p className="text-sm leading-relaxed text-muted-foreground">
                No sweep has been recorded. Only providers that were actually checked get a
                row, so an empty history means nothing has been asked yet — not that
                everything is accounted for.
              </p>
            )}
            <Button asChild variant="secondary" size="sm">
              <Link href="/reconcile">Reconcile</Link>
            </Button>
          </div>
        </div>
      </Card>
    </Section>
  );
}
