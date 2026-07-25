"use client";

import * as React from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { FieldError, FieldHint, Input, Label, Textarea } from "@/components/ui/field";
import { formatDuration } from "@/lib/format";
import { useRenew, useRetryTeardown, useRevoke } from "@/lib/queries";
import { parseDuration } from "@/lib/schema-form";
import { TERMINAL_STATES, type CatalogEntry, type Lease } from "@/lib/types";

/**
 * Renew, revoke, retry.
 *
 * Each one states its consequence in the dialog rather than in a tooltip somebody has to
 * find, because the consequences here are not symmetrical:
 *
 * - **Renew** measures from now, not from the old deadline. Somebody renewing a lease with
 *   three hours left expects to gain time, and on this broker they get exactly the TTL they
 *   ask for counted from this instant -- which can be *less* than they had.
 * - **Revoke** writes an intention, not a fact. The lease moves to REVOKED and a worker
 *   destroys the resource; the state that means "gone" is only ever written by a worker
 *   that called the provider and got a clean answer. Saying so here is what stops somebody
 *   reading REVOKED as "deleted" during an incident.
 * - **Retry teardown** is the button for a lease that gave up. Orphans are deliberately not
 *   retried automatically -- they got there by exhausting a retry budget against a provider
 *   that kept refusing, and a loop that keeps calling it hides the problem.
 */
export function LeaseActions({ lease, entry }: { lease: Lease; entry: CatalogEntry | undefined }) {
  const [renewOpen, setRenewOpen] = React.useState(false);
  const [revokeOpen, setRevokeOpen] = React.useState(false);

  const retry = useRetryTeardown(lease.id);

  // `usable` is the broker's own answer to "is this ACTIVE or EXPIRING", so the dashboard
  // does not maintain a second opinion about it.
  const renewable = lease.renewable && lease.usable && lease.renewals < lease.max_renewals;
  // A terminal lease has nothing left to revoke; the broker refuses it, and the button
  // should say so before the round trip rather than after a 409.
  const revocable = !TERMINAL_STATES.includes(lease.state);

  return (
    <div className="flex flex-wrap items-center gap-2">
      {lease.state === "orphaned" ? (
        <Button
          variant="secondary"
          size="sm"
          loading={retry.isPending}
          onClick={() => retry.mutate()}
        >
          Retry teardown
        </Button>
      ) : null}

      <Button
        variant="secondary"
        size="sm"
        disabled={!renewable}
        onClick={() => setRenewOpen(true)}
        title={
          renewable
            ? undefined
            : !lease.renewable
              ? "This golden path is not renewable."
              : lease.renewals >= lease.max_renewals
                ? `All ${lease.max_renewals} renewals have been used.`
                : "Only an active or expiring lease can be renewed."
        }
      >
        Renew
      </Button>

      <Button
        variant="danger"
        size="sm"
        disabled={!revocable}
        onClick={() => setRevokeOpen(true)}
        title={revocable ? undefined : "This lease has already finished."}
      >
        Revoke
      </Button>

      <RenewDialog lease={lease} entry={entry} open={renewOpen} onOpenChange={setRenewOpen} />
      <RevokeDialog lease={lease} open={revokeOpen} onOpenChange={setRevokeOpen} />

      {retry.isError ? (
        <p className="w-full text-xs text-danger">{retry.error.message}</p>
      ) : null}
    </div>
  );
}

function RenewDialog({
  lease,
  entry,
  open,
  onOpenChange,
}: {
  lease: Lease;
  entry: CatalogEntry | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [ttl, setTtl] = React.useState("");
  const renew = useRenew(lease.id);

  React.useEffect(() => {
    if (open) {
      setTtl("");
      renew.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const parsed = ttl.trim() ? parseDuration(ttl) : null;
  const malformed = Boolean(ttl.trim() && parsed === null);
  const remaining = lease.seconds_remaining ?? 0;
  const shrinks = parsed !== null && parsed < remaining;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent widthClass="max-w-md">
        <DialogHeader>
          <DialogTitle>Renew this lease</DialogTitle>
          <DialogDescription>
            The new deadline is measured from now, not from the current one. Renewal{" "}
            {lease.renewals + 1} of {lease.max_renewals}.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-3">
          <Label htmlFor="renew-ttl">How much longer</Label>
          <Input
            id="renew-ttl"
            value={ttl}
            spellCheck={false}
            aria-invalid={malformed}
            placeholder={entry ? `${entry.lease.default_ttl} (this path's default)` : "e.g. 2h"}
            onChange={(event) => setTtl(event.target.value)}
          />
          <FieldHint>
            Leave it empty for the path&apos;s default
            {entry ? ` of ${entry.lease.default_ttl}` : ""}. Anything above the ceiling
            {entry ? ` of ${entry.lease.max_ttl}` : ""} is clamped rather than rejected.
          </FieldHint>

          {malformed ? (
            <FieldError>Use a compact duration such as 2h, 45m or 1h30m.</FieldError>
          ) : null}

          {shrinks ? (
            <p className="rounded-md border border-warn/30 bg-warn-soft px-3 py-2 text-xs leading-relaxed text-warn">
              This lease already has {formatDuration(remaining)} left. Renewing for{" "}
              {formatDuration(parsed ?? 0)} from now would move the deadline{" "}
              <em>earlier</em>; the broker never shortens a lease on renewal, so it will keep
              the later of the two.
            </p>
          ) : null}

          {renew.isError ? (
            <p className="rounded-md border border-danger/25 bg-danger-soft px-3 py-2 text-sm leading-relaxed text-danger">
              {renew.error.message}
            </p>
          ) : null}
        </DialogBody>

        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={renew.isPending}
            disabled={malformed}
            onClick={() =>
              renew.mutate(
                { ttl: ttl.trim() || null },
                { onSuccess: () => onOpenChange(false) },
              )
            }
          >
            Renew
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RevokeDialog({
  lease,
  open,
  onOpenChange,
}: {
  lease: Lease;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [reason, setReason] = React.useState("");
  const revoke = useRevoke(lease.id);

  React.useEffect(() => {
    if (open) {
      setReason("");
      revoke.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent widthClass="max-w-md">
        <DialogHeader>
          <DialogTitle>Revoke this lease</DialogTitle>
          <DialogDescription>
            The lease moves to REVOKED and a worker is queued to destroy{" "}
            {lease.external_name ? (
              <span className="font-mono text-[13px]">{lease.external_name}</span>
            ) : (
              "its resource"
            )}
            . REVOKED is an intention: the state that means the resource is gone is only ever
            written by a worker that called the provider and got a clean answer.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-2">
          <Label htmlFor="revoke-reason" required>
            Reason
          </Label>
          <Textarea
            id="revoke-reason"
            rows={3}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Goes in the audit trail. Write it for whoever reads it in a month."
          />
          <FieldHint>
            Required. Somebody reading this trail during an incident should not have to guess
            why a resource disappeared.
          </FieldHint>

          {revoke.isError ? (
            <p className="rounded-md border border-danger/25 bg-danger-soft px-3 py-2 text-sm leading-relaxed text-danger">
              {revoke.error.message}
            </p>
          ) : null}
        </DialogBody>

        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={revoke.isPending}
            disabled={reason.trim().length === 0}
            onClick={() =>
              revoke.mutate({ reason: reason.trim() }, { onSuccess: () => onOpenChange(false) })
            }
          >
            Revoke lease
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
