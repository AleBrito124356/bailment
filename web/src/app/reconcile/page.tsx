"use client";

import * as React from "react";
import { ChevronRight, RefreshCcw, ShieldCheck } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { ErrorState } from "@/components/error-state";
import { PageHeader, Section } from "@/components/page-header";
import { DriftList, OrphanList, UnknownList } from "@/components/reconcile-findings";
import { Timestamp } from "@/components/timestamp";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { Tooltip } from "@/components/ui/tooltip";
import { formatCount } from "@/lib/format";
import { useProviders, useReconcileNow, useReconcileRun, useReconcileRuns } from "@/lib/queries";
import type { ProviderReconcileResult, ReconcileRun, ReconcileTrigger } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Reconciliation: both directions of drift, and the history of looking.
 *
 * This is the page that justifies the project. Credential brokers exist; brokers that go
 * back and check whether the thing they created still exists, and whether something exists
 * that they never created, do not. So the screen is built around the two findings --
 * orphans and drift -- and around one distinction that is easy to get wrong and expensive
 * to get wrong: a provider that could not be reached is not a provider with nothing in it.
 * `checked` and `status_unknown` are rendered everywhere they apply.
 */
export default function ReconcilePage() {
  const runs = useReconcileRuns();
  const providers = useProviders();
  const reconcile = useReconcileNow();

  return (
    <>
      <PageHeader
        title="Reconcile"
        description="Compare what the providers actually have against what this broker believes it has. Running from here reports; it never destroys."
        actions={
          <Button
            variant="primary"
            size="sm"
            loading={reconcile.isPending}
            onClick={() => reconcile.mutate(undefined)}
          >
            <RefreshCcw className="h-3.5 w-3.5" aria-hidden />
            {reconcile.isPending ? "Sweeping…" : "Run now"}
          </Button>
        }
      />

      <div className="space-y-8">
        {reconcile.isPending ? (
          <Card className="p-4 text-sm leading-relaxed text-muted-foreground">
            The sweep runs inline on the broker, so this takes as long as the provider calls
            do. A trigger that answered immediately would leave you unable to tell a clean
            sweep from one that never happened.
          </Card>
        ) : null}

        {reconcile.isError ? (
          <ErrorState
            error={reconcile.error}
            what="a reconcile pass"
            onRetry={() => reconcile.mutate(undefined)}
          />
        ) : null}

        {reconcile.data ? <TriggerResult result={reconcile.data} /> : null}

        <Section
          title="Run history"
          description="One row per provider per pass. Only providers that were actually checked get one."
        >
          {runs.isError ? (
            <ErrorState
              error={runs.error}
              what="the reconcile history"
              onRetry={() => void runs.refetch()}
            />
          ) : runs.isPending ? (
            <SkeletonRows rows={4} />
          ) : (runs.data?.items.length ?? 0) === 0 ? (
            <Card>
              <EmptyState
                icon={RefreshCcw}
                title="Nothing has been swept yet"
                description="An empty history means nobody has looked — not that everything is accounted for. Run a pass, or let the periodic reconciler take its first one."
                action={
                  <Button
                    variant="primary"
                    size="sm"
                    loading={reconcile.isPending}
                    onClick={() => reconcile.mutate(undefined)}
                  >
                    Run now
                  </Button>
                }
              />
            </Card>
          ) : (
            <Card className="overflow-hidden">
              <div className="scroll-x">
                <table className="w-full min-w-[780px] border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-border text-left">
                      <Th className="w-8" />
                      <Th>Provider</Th>
                      <Th>Started</Th>
                      <Th className="text-right">Seen</Th>
                      <Th className="text-right">Orphans</Th>
                      <Th className="text-right">Drift</Th>
                      <Th className="text-right">Unknown</Th>
                      <Th className="text-right">Errors</Th>
                      <Th>Result</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {(runs.data?.items ?? []).map((run) => (
                      <RunRow key={run.id} run={run} />
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}
        </Section>

        <Section
          title="Providers"
          description="Registered whether or not they are configured, because 'no orphans here' and 'never asked' must not look the same."
        >
          {providers.isError ? (
            <ErrorState
              error={providers.error}
              what="the provider list"
              onRetry={() => void providers.refetch()}
            />
          ) : providers.isPending ? (
            <SkeletonRows rows={3} />
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {(providers.data?.items ?? []).map((provider) => (
                <Card key={provider.name} className="p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[15px] font-semibold">{provider.name}</span>
                    <Badge tone={provider.available ? "ok" : "neutral"} dot>
                      {provider.available ? "configured" : "not configured"}
                    </Badge>
                    {provider.supports_reconciliation ? (
                      <Tooltip content="This provider can enumerate its own resources by bailment's marker, so it can be swept for orphans.">
                        <span className="inline-flex">
                          <Badge tone="neutral">
                            <ShieldCheck className="h-3 w-3" aria-hidden />
                            reconcilable
                          </Badge>
                        </span>
                      </Tooltip>
                    ) : (
                      <Badge tone="warn">cannot be swept</Badge>
                    )}
                  </div>

                  {provider.reason ? (
                    <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                      {provider.reason}
                    </p>
                  ) : null}

                  {provider.missing_settings.length > 0 ? (
                    <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                      Set{" "}
                      {provider.missing_settings.map((name, index) => (
                        <React.Fragment key={name}>
                          {index > 0 ? ", " : ""}
                          <span className="font-mono text-foreground">{name}</span>
                        </React.Fragment>
                      ))}{" "}
                      to enable it.
                    </p>
                  ) : null}

                  {provider.golden_paths.length > 0 ? (
                    <p className="mt-2 text-2xs text-muted-foreground">
                      Serves: {provider.golden_paths.join(", ")}
                    </p>
                  ) : null}
                </Card>
              ))}
            </div>
          )}
        </Section>
      </div>
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

function TriggerResult({ result }: { result: ReconcileTrigger }) {
  return (
    <Section title="Last run from here">
      <Card className="p-5">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={result.clean ? "ok" : "warn"} dot>
            {result.clean ? "Clean" : "Findings"}
          </Badge>
          <span className="text-sm text-muted-foreground">
            <Timestamp value={result.started_at} /> · {formatCount(result.resources_seen)}{" "}
            resources seen across {result.providers_checked.length}{" "}
            {result.providers_checked.length === 1 ? "provider" : "providers"}
          </span>
          <Tooltip content="Destroying an orphan needs the global setting and a per-provider allow-list. The API passes an empty allow-list on every run, whatever the deployment is configured to do.">
            <span className="ml-auto inline-flex">
              <Badge tone="neutral">report only</Badge>
            </span>
          </Tooltip>
        </div>

        {result.providers_skipped.length > 0 ? (
          <p className="mt-2.5 text-xs leading-relaxed text-muted-foreground">
            Skipped: {result.providers_skipped.join(", ")}. A skipped provider reports zero
            orphans, which is not the same as having none.
          </p>
        ) : null}

        <div className="mt-4 space-y-4">
          {result.providers.map((provider) => (
            <ProviderResult key={provider.provider} result={provider} />
          ))}
        </div>
      </Card>
    </Section>
  );
}

function ProviderResult({ result }: { result: ProviderReconcileResult }) {
  const nothing =
    result.orphans.length === 0 && result.drift.length === 0 && result.status_unknown.length === 0;

  return (
    <div className="rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-semibold">{result.provider}</span>
        {result.checked ? (
          <Badge tone="neutral">
            {formatCount(result.resources_seen)} seen · {formatCount(result.leases_checked)} leases
          </Badge>
        ) : (
          <Badge tone="warn">not checked</Badge>
        )}
        {result.within_grace > 0 ? (
          <Tooltip content="Resources younger than the grace period are skipped: a resource created seconds ago has not necessarily had its lease row committed yet, and reporting it would manufacture an orphan out of a race.">
            <span className="inline-flex">
              <Badge tone="neutral">{result.within_grace} within grace</Badge>
            </span>
          </Tooltip>
        ) : null}
      </div>

      {result.skipped_reason ? (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          {result.skipped_reason}
        </p>
      ) : null}

      {result.errors.length > 0 ? (
        <ul className="mt-2 space-y-1 text-xs text-danger">
          {result.errors.map((error) => (
            <li key={error}>{error}</li>
          ))}
        </ul>
      ) : null}

      {nothing && result.checked ? (
        <p className="mt-2 text-xs text-muted-foreground">
          Everything at this provider is accounted for.
        </p>
      ) : (
        <div className="mt-3 space-y-3">
          <OrphanList orphans={result.orphans} />
          <DriftList drift={result.drift} />
          <UnknownList names={result.status_unknown} />
        </div>
      )}
    </div>
  );
}

function RunRow({ run }: { run: ReconcileRun }) {
  const [open, setOpen] = React.useState(false);
  const detail = useReconcileRun(open ? run.id : null);

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
            aria-label={open ? "Hide findings" : "Show findings"}
            className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <ChevronRight
              className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-90")}
              aria-hidden
            />
          </button>
        </td>
        <td className="px-3 py-2.5 font-medium">{run.provider}</td>
        <td className="px-3 py-2.5 text-muted-foreground">
          <Timestamp value={run.started_at} />
          {run.duration_seconds !== null ? (
            <span className="ml-1.5 text-2xs">({run.duration_seconds}s)</span>
          ) : null}
        </td>
        <Td>{formatCount(run.resources_seen)}</Td>
        <Td tone={run.orphans_found > 0 ? "warn" : undefined}>
          {formatCount(run.orphans_found)}
          {run.orphans_destroyed > 0 ? (
            <span className="text-2xs text-muted-foreground">
              {" "}
              ({run.orphans_destroyed} destroyed)
            </span>
          ) : null}
        </Td>
        <Td tone={run.drift_found > 0 ? "warn" : undefined}>{formatCount(run.drift_found)}</Td>
        <Td tone={run.status_unknown > 0 ? "warn" : undefined}>
          {formatCount(run.status_unknown)}
        </Td>
        <Td tone={run.errors > 0 ? "danger" : undefined}>{formatCount(run.errors)}</Td>
        <td className="px-3 py-2.5">
          <Badge tone={run.clean ? "neutral" : "warn"} dot>
            {run.clean ? "clean" : "findings"}
          </Badge>
        </td>
      </tr>

      {open ? (
        <tr className="border-b border-border bg-muted/20">
          <td colSpan={9} className="px-5 py-4">
            {detail.isError ? (
              <ErrorState error={detail.error} what="this run" />
            ) : detail.isPending ? (
              <SkeletonRows rows={2} />
            ) : (
              <div className="space-y-3">
                <OrphanList orphans={detail.data?.detail.orphans ?? []} />
                <DriftList drift={detail.data?.detail.drift ?? []} />
                <UnknownList names={detail.data?.detail.status_unknown ?? []} />
                {(detail.data?.detail.errors ?? []).length > 0 ? (
                  <ul className="space-y-1 text-xs text-danger">
                    {(detail.data?.detail.errors ?? []).map((error) => (
                      <li key={error}>{error}</li>
                    ))}
                  </ul>
                ) : null}
                {run.clean ? (
                  <p className="text-sm text-muted-foreground">
                    This pass found nothing: every resource at the provider had a live lease
                    behind it, and every lease this broker believed was live still had its
                    resource.
                  </p>
                ) : null}
                <p className="text-2xs text-muted-foreground">
                  Grace period: {formatCount(detail.data?.detail.grace_seconds ?? 0)}s ·{" "}
                  {formatCount(detail.data?.detail.in_flight_skipped ?? 0)} skipped as in-flight ·{" "}
                  {formatCount(detail.data?.detail.raced ?? 0)} raced ·{" "}
                  {formatCount(detail.data?.detail.known_orphan_leases ?? 0)} already known
                  orphans
                </p>
              </div>
            )}
          </td>
        </tr>
      ) : null}
    </>
  );
}

function Td({ children, tone }: { children: React.ReactNode; tone?: "warn" | "danger" }) {
  return (
    <td
      className={cn(
        "tabular px-3 py-2.5 text-right",
        tone === "warn" ? "font-medium text-warn" : tone === "danger" ? "text-danger" : "",
      )}
    >
      {children}
    </td>
  );
}
