"use client";

import { ClipboardCheck } from "lucide-react";

import { ApprovalCard } from "@/components/approval-card";
import { EmptyState } from "@/components/empty-state";
import { ErrorState } from "@/components/error-state";
import { PageHeader } from "@/components/page-header";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useApprovals } from "@/lib/queries";

/**
 * The queue.
 *
 * Oldest first, because the interesting end is the one about to time out. An unanswered
 * request is rejected by the ticker once its deadline passes -- with a reason that says
 * nobody answered rather than a generic denial -- so a queue nobody reads quietly turns
 * into a pile of rejections, and the requester, usually an agent, learns nothing from any
 * of them.
 *
 * Operator-only at the broker. An agent token gets a 403 here, and that is not an error:
 * an approval gate the gated thing can open is not a gate. The error state says so and
 * offers the token dialog rather than implying something is broken.
 */
export default function ApprovalsPage() {
  const approvals = useApprovals();
  const items = approvals.data?.items ?? [];

  return (
    <>
      <PageHeader
        title="Approvals"
        description="Requests that policy stopped for a human. Nothing has been provisioned for any of them, so declining one destroys nothing."
      />

      {approvals.isError ? (
        <ErrorState
          error={approvals.error}
          what="the approval queue"
          onRetry={() => void approvals.refetch()}
        />
      ) : approvals.isPending ? (
        <div className="space-y-4">
          <Skeleton className="h-64 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : items.length === 0 ? (
        <Card>
          <EmptyState
            icon={ClipboardCheck}
            title="Nothing is waiting"
            description="Requests appear here the moment a policy rule returns require_approval — a production database branch, a lease longer than the pre-approved spend. Everything else is self-service and never reaches this page."
          />
        </Card>
      ) : (
        <div className="space-y-4">
          {items.map((approval) => (
            <ApprovalCard
              key={approval.lease_id}
              approval={approval}
              anchorMs={approvals.dataUpdatedAt}
            />
          ))}
          {approvals.data?.has_more ? (
            <p className="text-xs text-muted-foreground">
              More requests are waiting than fit on this page. Decide these and the rest follow.
            </p>
          ) : null}
        </div>
      )}
    </>
  );
}
