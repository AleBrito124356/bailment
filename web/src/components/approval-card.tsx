"use client";

import * as React from "react";
import Link from "next/link";
import { Bot, CircleDot, UserRound } from "lucide-react";

import { Countdown } from "@/components/countdown";
import { Timestamp } from "@/components/timestamp";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { FieldHint, Label, Textarea } from "@/components/ui/field";
import { Tooltip } from "@/components/ui/tooltip";
import { formatDuration, formatUsd, formatValue } from "@/lib/format";
import { useApprove, useCatalogEntry, useReject } from "@/lib/queries";
import type { CatalogEntry, PendingApproval } from "@/lib/types";
import { cn } from "@/lib/utils";

const HOURS_PER_MONTH = 730;

/**
 * One request waiting on a human.
 *
 * This card is the argument. Everything else in the product is machinery; this is the
 * moment a person decides whether an automated process gets a copy of production data, and
 * the card has to carry enough for that decision to be made in about fifteen seconds
 * without opening anything else.
 *
 * So it says, in this order: **who asked and whether it was a machine**, because "an agent
 * asked, on Alice's behalf" is a different question from "Alice asked"; **what they asked
 * for, against the path's defaults**, so the deviations stand out rather than being hidden
 * in a list of six identical-looking values; **what it costs for how long**; **the policy's
 * own reason, verbatim**, because it was written by whoever wrote the rule and no
 * paraphrase of it is more authoritative; and **how long is left to decide**, because an
 * approval nobody answers is rejected by the ticker with a reason that says so.
 *
 * The two buttons are equals. Approve is the accent; reject is an outline and neither
 * smaller nor hidden behind a menu. A queue where declining is harder than agreeing is a
 * queue that produces agreement.
 */
export function ApprovalCard({
  approval,
  anchorMs,
}: {
  approval: PendingApproval;
  anchorMs: number;
}) {
  const [approveOpen, setApproveOpen] = React.useState(false);
  const [rejectOpen, setRejectOpen] = React.useState(false);
  const entry = useCatalogEntry(approval.golden_path);

  const monthly = approval.estimated_hourly_usd * HOURS_PER_MONTH;
  const forThisLease = (approval.estimated_hourly_usd * approval.ttl_seconds) / 3600;
  const overdue =
    approval.seconds_until_deadline !== null && approval.seconds_until_deadline < 0;

  return (
    <Card className={cn("overflow-hidden", overdue && "border-warn/40")}>
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border p-5">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[15px] font-semibold tracking-[-0.01em]">
              {entry.data?.name ?? approval.golden_path}
            </h3>
            <Badge tone="neutral">{approval.provider}</Badge>
            <span className="font-mono text-2xs text-muted-foreground">
              {approval.golden_path}
            </span>
          </div>
          <Requester approval={approval} />
        </div>

        <div className="text-right">
          <p className="text-2xs uppercase tracking-wider text-muted-foreground">
            {overdue ? "Window closed" : "Time to decide"}
          </p>
          <Countdown
            seconds={approval.seconds_until_deadline}
            anchorMs={anchorMs}
            warnBelow={600}
            className="text-[20px] font-semibold"
          />
          <p className="mt-0.5 text-2xs text-muted-foreground">
            waiting {formatDuration(approval.waiting_seconds)}
          </p>
        </div>
      </div>

      <div className="grid gap-5 p-5 lg:grid-cols-[minmax(0,1fr)_16rem]">
        <div className="min-w-0 space-y-4">
          <Reason approval={approval} />
          <InputsDiff approval={approval} entry={entry.data} />
        </div>

        <dl className="space-y-3 lg:border-l lg:border-border lg:pl-5">
          <SideFact label="Lease">{formatDuration(approval.ttl_seconds)}</SideFact>
          <SideFact label="Cost for this lease">
            {approval.estimated_hourly_usd > 0 ? formatUsd(forThisLease) : "free"}
          </SideFact>
          <SideFact
            label="If it ran all month"
            hint="The path's own estimate, at 730 hours. Shown because a four-hour lease that gets renewed is how a rehearsal becomes an environment."
          >
            {approval.estimated_hourly_usd > 0 ? formatUsd(monthly) : "free"}
          </SideFact>
          <SideFact label="Requested">
            <Timestamp value={approval.requested_at} />
          </SideFact>
          {approval.allowed_approvers.length > 0 ? (
            <SideFact label="Approvers">{approval.allowed_approvers.join(", ")}</SideFact>
          ) : null}
        </dl>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-border bg-muted/30 p-4">
        <Button asChild variant="ghost" size="sm" className="mr-auto">
          <Link href={`/leases/${approval.lease_id}`}>Open the lease</Link>
        </Button>
        <Button variant="danger" size="sm" onClick={() => setRejectOpen(true)}>
          Reject
        </Button>
        <Button variant="primary" size="sm" onClick={() => setApproveOpen(true)}>
          Approve
        </Button>
      </div>

      <ApproveDialog approval={approval} open={approveOpen} onOpenChange={setApproveOpen} />
      <RejectDialog approval={approval} open={rejectOpen} onOpenChange={setRejectOpen} />
    </Card>
  );
}

/**
 * Who asked.
 *
 * `on_behalf_of` is an audit annotation that grants nothing, and it is the single most
 * decision-relevant field on the card: an agent acting for a named human is a different
 * risk from an agent acting for nobody in particular, and both are different from a person
 * asking directly.
 */
function Requester({ approval }: { approval: PendingApproval }) {
  const onBehalf = Boolean(approval.on_behalf_of);
  const Icon = onBehalf ? Bot : UserRound;

  return (
    <Tooltip
      content={
        onBehalf
          ? "The caller declared a principal it was acting for. That is an audit annotation and grants nothing — it is who to ask about this, not who authorised it."
          : "No principal was declared behind this request. The broker records 'on behalf of' only when the caller sends it, so its absence means nobody was named — not that a human typed this."
      }
    >
      <p className="flex w-fit flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
        <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span className="font-medium text-foreground">{approval.requester}</span>
        {onBehalf ? (
          <>
            <span>acting on behalf of</span>
            <span className="font-medium text-foreground">{approval.on_behalf_of}</span>
          </>
        ) : (
          <span>— no principal was named behind it</span>
        )}
      </p>
    </Tooltip>
  );
}

function Reason({ approval }: { approval: PendingApproval }) {
  return (
    <div className="rounded-md border-l-2 border-accent bg-accent-soft/60 px-4 py-3">
      <p className="text-2xs font-semibold uppercase tracking-wider text-accent">
        Why policy stopped it
      </p>
      <p className="mt-1.5 text-sm leading-relaxed text-foreground">{approval.reason}</p>
      {approval.policy_reason && approval.policy_reason !== approval.reason ? (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          {approval.policy_reason}
        </p>
      ) : null}
    </div>
  );
}

/**
 * What was asked for, against what the path would have given by default.
 *
 * Rendering the inputs as a flat list makes every value look equally ordinary, and the
 * whole reason this request is in the queue is that one of them is not. Marking the values
 * that deviate from the schema's defaults — and listing the properties that were left to
 * take one — puts the deviation where the eye lands.
 */
function InputsDiff({
  approval,
  entry,
}: {
  approval: PendingApproval;
  entry: CatalogEntry | undefined;
}) {
  const properties = entry?.input_schema.properties ?? {};
  const supplied = Object.entries(approval.inputs);

  const omitted = Object.entries(properties).filter(
    ([name, schema]) =>
      name !== "ttl" && !(name in approval.inputs) && schema.default !== undefined,
  );

  return (
    <div>
      <p className="text-2xs font-semibold uppercase tracking-wider text-muted-foreground">
        Inputs
      </p>

      {supplied.length === 0 ? (
        <p className="mt-1.5 text-sm text-muted-foreground">This path takes no arguments.</p>
      ) : (
        <ul className="mt-2 space-y-1">
          {supplied.map(([name, value]) => {
            const schema = properties[name];
            const fallback = schema?.default;
            const differs = fallback !== undefined && formatValue(fallback) !== formatValue(value);
            const noDefault = fallback === undefined;

            return (
              <li
                key={name}
                className={cn(
                  "flex flex-wrap items-baseline gap-x-2 rounded px-2 py-1 text-sm",
                  differs && "bg-warn-soft",
                )}
              >
                {differs ? (
                  <CircleDot className="h-3 w-3 shrink-0 self-center text-warn" aria-hidden />
                ) : null}
                <span className="font-mono text-[13px] text-muted-foreground">{name}</span>
                <span className="min-w-0 break-all font-mono text-[13px] font-medium text-foreground">
                  {formatValue(value)}
                </span>
                {differs ? (
                  <span className="text-2xs text-warn">
                    default is {formatValue(fallback)}
                  </span>
                ) : noDefault ? null : (
                  <span className="text-2xs text-muted-foreground">default</span>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {omitted.length > 0 ? (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          Left unset, and will take the path&apos;s default:{" "}
          {omitted
            .map(([name, schema]) => `${name} = ${formatValue(schema.default)}`)
            .join(", ")}
          .
        </p>
      ) : null}
    </div>
  );
}

function SideFact({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  const body = (
    <div>
      <dt className="text-2xs uppercase tracking-wider text-muted-foreground">{label}</dt>
      <dd className="tabular mt-0.5 text-[13px] text-foreground">{children}</dd>
    </div>
  );
  return hint ? <Tooltip content={hint}>{body}</Tooltip> : body;
}

function ApproveDialog({
  approval,
  open,
  onOpenChange,
}: {
  approval: PendingApproval;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [note, setNote] = React.useState("");
  const approve = useApprove();

  React.useEffect(() => {
    if (open) {
      setNote("");
      approve.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent widthClass="max-w-md">
        <DialogHeader>
          <DialogTitle>Approve this request</DialogTitle>
          <DialogDescription>
            The lease is queued for provisioning immediately and will run for{" "}
            {formatDuration(approval.ttl_seconds)}. Your principal is recorded on the approval.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-2">
          <Label htmlFor="approve-note">Note</Label>
          <Textarea
            id="approve-note"
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Optional. Goes in the audit trail."
          />
          <FieldHint>
            Worth a sentence when the answer was not obvious. Somebody reading this in a month
            will not remember the conversation you had before clicking.
          </FieldHint>

          {approve.isError ? (
            <p className="rounded-md border border-danger/25 bg-danger-soft px-3 py-2 text-sm leading-relaxed text-danger">
              {approve.error.message}
            </p>
          ) : null}
        </DialogBody>

        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={approve.isPending}
            onClick={() =>
              approve.mutate(
                { leaseId: approval.lease_id, body: { note: note.trim() || null } },
                { onSuccess: () => onOpenChange(false) },
              )
            }
          >
            Approve
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RejectDialog({
  approval,
  open,
  onOpenChange,
}: {
  approval: PendingApproval;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [reason, setReason] = React.useState("");
  const reject = useReject();

  React.useEffect(() => {
    if (open) {
      setReason("");
      reject.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent widthClass="max-w-md">
        <DialogHeader>
          <DialogTitle>Decline this request</DialogTitle>
          <DialogDescription>
            Nothing was provisioned, so nothing is destroyed. The requester —{" "}
            {approval.requester} — sees your reason verbatim.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-2">
          <Label htmlFor="reject-reason" required>
            Reason
          </Label>
          <Textarea
            id="reject-reason"
            rows={3}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="What would have to be different for this to be granted?"
          />
          <FieldHint>
            Required, and the requester is usually an agent. An agent told only
            &ldquo;denied&rdquo; tries again with a small variation until something works; one
            told what to change either changes it or stops.
          </FieldHint>

          {reject.isError ? (
            <p className="rounded-md border border-danger/25 bg-danger-soft px-3 py-2 text-sm leading-relaxed text-danger">
              {reject.error.message}
            </p>
          ) : null}
        </DialogBody>

        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={reject.isPending}
            disabled={reason.trim().length === 0}
            onClick={() =>
              reject.mutate(
                { leaseId: approval.lease_id, body: { reason: reason.trim() } },
                { onSuccess: () => onOpenChange(false) },
              )
            }
          >
            Reject
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
