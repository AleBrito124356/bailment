"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { AuditTimeline } from "@/components/audit-timeline";
import { BindingsPanel } from "@/components/bindings-panel";
import { CopyableCode } from "@/components/copy-button";
import { Countdown } from "@/components/countdown";
import { ErrorState } from "@/components/error-state";
import { LeaseActions } from "@/components/lease-actions";
import { PageHeader, Section } from "@/components/page-header";
import { PolicyTraceView } from "@/components/policy-trace-view";
import { StateBadge } from "@/components/state-badge";
import { Timestamp } from "@/components/timestamp";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDuration, formatUsd } from "@/lib/format";
import { buildPolicyTrace } from "@/lib/policy-trace";
import { useAudit, useBindings, useCatalogEntry, useLease } from "@/lib/queries";
import { TERMINAL_STATES, type Lease } from "@/lib/types";

/**
 * One lease, in full.
 *
 * The order of the page is the order of the questions somebody actually asks: what state is
 * it in and how long has it got, what was asked for, why was that allowed, what credential
 * did it produce (by reference), and then everything that happened to it.
 */
export default function LeaseDetailPage() {
  const params = useParams<{ id: string }>();
  const id = typeof params.id === "string" ? params.id : "";

  const lease = useLease(id);
  const audit = useAudit(id);
  const bindings = useBindings(id);
  const entry = useCatalogEntry(lease.data?.golden_path ?? null);

  const trace = React.useMemo(
    () =>
      lease.data ? buildPolicyTrace(lease.data, entry.data, audit.data?.items ?? []) : null,
    [lease.data, entry.data, audit.data],
  );

  if (lease.isError) {
    return (
      <>
        <BackLink />
        <ErrorState error={lease.error} what="this lease" onRetry={() => void lease.refetch()} />
      </>
    );
  }

  if (lease.isPending || !lease.data) {
    return (
      <>
        <BackLink />
        <Skeleton className="h-10 w-72" />
        <div className="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-20 w-full" />
          ))}
        </div>
      </>
    );
  }

  const data = lease.data;
  const hasSecrets = data.secret_output_names.length > 0;

  return (
    <>
      <BackLink />

      <PageHeader
        title={
          <span className="flex flex-wrap items-center gap-3">
            {data.golden_path}
            <StateBadge state={data.state} />
          </span>
        }
        description={
          <span className="flex flex-wrap items-center gap-2">
            <CopyableCode value={data.id} />
            <span>·</span>
            <span>{data.provider}</span>
          </span>
        }
        actions={<LeaseActions lease={data} entry={entry.data} />}
      />

      <div className="space-y-8">
        <Facts lease={data} anchorMs={lease.dataUpdatedAt} />

        <div className="grid gap-6 lg:grid-cols-2">
          <Section title="Inputs" description="Validated against the golden path's schema.">
            <Card className="p-4">
              {Object.keys(data.inputs).length === 0 ? (
                <p className="text-sm text-muted-foreground">This path takes no arguments.</p>
              ) : (
                <dl className="space-y-2">
                  {Object.entries(data.inputs).map(([key, value]) => (
                    <div
                      key={key}
                      className="flex flex-wrap items-baseline gap-x-3 border-b border-border pb-2 last:border-0 last:pb-0"
                    >
                      <dt className="font-mono text-[13px] text-muted-foreground">{key}</dt>
                      <dd className="min-w-0 break-words font-mono text-[13px] text-foreground">
                        {typeof value === "string" ? value : JSON.stringify(value)}
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
            </Card>
          </Section>

          <Section
            title="Outputs"
            description="Non-secret values in plain text; sealed ones by name only."
          >
            <Card className="p-4">
              {Object.keys(data.outputs).length === 0 && !hasSecrets ? (
                <p className="text-sm text-muted-foreground">
                  Nothing has been produced yet. Outputs appear when the provider confirms the
                  resource exists.
                </p>
              ) : (
                <div className="space-y-3">
                  {Object.entries(data.outputs).map(([key, value]) => (
                    <div key={key} className="space-y-1">
                      <p className="font-mono text-2xs uppercase tracking-wider text-muted-foreground">
                        {key}
                      </p>
                      <CopyableCode value={value} />
                    </div>
                  ))}
                  {hasSecrets ? (
                    <div className="space-y-1.5 border-t border-border pt-3">
                      <p className="text-2xs uppercase tracking-wider text-muted-foreground">
                        Sealed
                      </p>
                      <div className="flex flex-wrap gap-1.5">
                        {data.secret_output_names.map((name) => (
                          <Badge key={name} tone="neutral" className="font-mono">
                            {name}
                          </Badge>
                        ))}
                      </div>
                      <p className="text-xs leading-relaxed text-muted-foreground">
                        Names, not values. See the bindings below for why that is the whole
                        point rather than a limitation.
                      </p>
                    </div>
                  ) : null}
                </div>
              )}
            </Card>
          </Section>
        </div>

        {data.approval ? <ApprovalPanel lease={data} /> : null}

        <Section
          title="Policy decision"
          description="The chain that ran, rule by rule, including the rules that were never reached."
        >
          {trace ? (
            <PolicyTraceView trace={trace} />
          ) : (
            <Skeleton className="h-32 w-full" />
          )}
        </Section>

        <Section
          title="Bindings"
          description="References and metadata. No API in this system returns the value."
        >
          <BindingsPanel
            bindings={bindings.data?.items}
            loading={bindings.isPending}
            hasSecrets={hasSecrets}
          />
        </Section>

        <Section title="Audit trail" description="Append-only, oldest first.">
          <Card className="p-5">
            {audit.isError ? (
              <ErrorState error={audit.error} what="the audit trail" className="border-0" />
            ) : (
              <AuditTimeline events={audit.data?.items} loading={audit.isPending} />
            )}
          </Card>
        </Section>
      </div>
    </>
  );
}

function BackLink() {
  return (
    <Button asChild variant="ghost" size="sm" className="-ml-2 mb-2">
      <Link href="/leases">
        <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
        Leases
      </Link>
    </Button>
  );
}

function Facts({ lease, anchorMs }: { lease: Lease; anchorMs: number }) {
  // A terminal lease keeps its `expires_at`, so the broker keeps computing a remaining
  // figure for it. Counting down towards nothing would be noise on a lease that is over.
  const finished = TERMINAL_STATES.includes(lease.state);

  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Fact label={finished ? "Ran until" : "Remaining"}>
        {finished ? (
          <span className="text-[15px] font-medium">
            <Timestamp value={lease.released_at ?? lease.expires_at} />
          </span>
        ) : (
          <Countdown
            seconds={lease.seconds_remaining}
            anchorMs={anchorMs}
            warnBelow={1800}
            className="text-[22px] font-semibold"
          />
        )}
        <span className="mt-1 block text-xs text-muted-foreground">
          {finished ? (
            "this lease is finished and will not move again"
          ) : lease.expires_at ? (
            <>
              expires <Timestamp value={lease.expires_at} />
            </>
          ) : (
            "no deadline set yet"
          )}
        </span>
      </Fact>

      <Fact label="Term">
        <span className="tabular text-[22px] font-semibold">
          {formatDuration(lease.ttl_seconds)}
        </span>
        <span className="mt-1 block text-xs text-muted-foreground">
          {lease.renewals} of {lease.max_renewals} renewals used · ceiling{" "}
          {formatDuration(lease.max_ttl_seconds)}
        </span>
      </Fact>

      <Fact label="Requested by">
        <span className="block truncate text-[15px] font-medium">{lease.requester}</span>
        <span className="mt-1 block text-xs text-muted-foreground">
          {lease.on_behalf_of ? `on behalf of ${lease.on_behalf_of}` : "not acting for anyone"}
          {" · "}
          <Timestamp value={lease.created_at} />
        </span>
      </Fact>

      <Fact label="Resource">
        {lease.external_name ? (
          <span className="block truncate font-mono text-[13px]" title={lease.external_name}>
            {lease.external_name}
          </span>
        ) : (
          <span className="text-[15px] text-muted-foreground">not named yet</span>
        )}
        <span className="mt-1 block text-xs text-muted-foreground">
          {lease.estimated_hourly_usd > 0
            ? `${formatUsd(lease.estimated_hourly_usd)} per hour while it exists`
            : "no estimated cost"}
        </span>
      </Fact>

      {lease.failure_reason ? (
        <div className="rounded-lg border border-border bg-card p-4 sm:col-span-2 xl:col-span-4">
          <p className="text-2xs uppercase tracking-wider text-muted-foreground">Failure</p>
          <p className="mt-1 text-sm leading-relaxed text-foreground">{lease.failure_reason}</p>
        </div>
      ) : null}
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <p className="text-[13px] font-medium text-muted-foreground">{label}</p>
      <div className="mt-1.5">{children}</div>
    </div>
  );
}

/** The approval that gated this lease, decided or otherwise. */
function ApprovalPanel({ lease }: { lease: Lease }) {
  const approval = lease.approval;
  if (!approval) return null;

  const tone = approval.pending ? "info" : approval.approved ? "ok" : "neutral";
  const label = approval.pending
    ? "Waiting on a human"
    : approval.approved
      ? `Approved by ${approval.decided_by ?? "an operator"}`
      : `Declined by ${approval.decided_by ?? "an operator"}`;

  return (
    <Section title="Approval">
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={tone} dot>
            {label}
          </Badge>
          <span className="text-xs text-muted-foreground">
            requested <Timestamp value={approval.requested_at} />
            {approval.decided_at ? (
              <>
                {" · decided "}
                <Timestamp value={approval.decided_at} />
              </>
            ) : approval.deadline_at ? (
              <>
                {" · times out "}
                <Timestamp value={approval.deadline_at} />
              </>
            ) : null}
          </span>
        </div>

        <p className="mt-2.5 text-sm leading-relaxed text-foreground">{approval.reason}</p>

        {approval.decision_note ? (
          <p className="mt-2 border-l-2 border-border pl-3 text-sm leading-relaxed text-muted-foreground">
            {approval.decision_note}
          </p>
        ) : null}

        {approval.allowed_approvers.length > 0 ? (
          <p className="mt-2 text-xs text-muted-foreground">
            Only these principals may decide it: {approval.allowed_approvers.join(", ")}. Holding
            an operator token is not the same as being on the list.
          </p>
        ) : null}

        {approval.pending ? (
          <div className="mt-3">
            <Button asChild variant="primary" size="sm">
              <Link href="/approvals">Open the approval queue</Link>
            </Button>
          </div>
        ) : null}
      </Card>
    </Section>
  );
}
