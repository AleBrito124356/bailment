"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { ApiError, api, type LeaseFilters } from "@/lib/api";
import { useToken } from "@/lib/auth";
import type {
  ApprovalQueue,
  ApproveRequestBody,
  AuditTrail,
  BindingList,
  CatalogDetail,
  CatalogList,
  Lease,
  LeasePage,
  ProviderList,
  ProvisionRequestBody,
  ProvisionResult,
  ReconcileRunDetail,
  ReconcileRunPage,
  ReconcileTrigger,
  RejectRequestBody,
  RenewRequestBody,
  RevokeRequestBody,
  Stats,
} from "@/lib/types";
import { TERMINAL_STATES } from "@/lib/types";

/**
 * Every read this dashboard performs, with the interval it performs it at.
 *
 * **The intervals are chosen against what the broker can actually change.** A lease's TTL
 * runs down continuously, but the *number* on the wire only changes when the ticker moves
 * it between states, and countdowns tick locally without asking anybody. So the lease list
 * refreshes every ten seconds and the countdowns stay smooth in between. The catalog is
 * files on disk; five minutes is generous. Reconcile history changes when a sweep
 * finishes, which is minutes apart at best.
 *
 * Polling stops when the tab is hidden -- TanStack's default. A dashboard left open on a
 * second monitor overnight should not be the reason somebody's broker log is unreadable.
 *
 * **The token is part of every query key.** Swapping an agent token for an operator token
 * changes what every endpoint returns, and a cache that survived that swap would show one
 * caller the other's data for as long as it stayed fresh.
 */

const SECOND = 1000;

export const POLL = {
  stats: 10 * SECOND,
  leases: 10 * SECOND,
  leaseLive: 5 * SECOND,
  leaseSettled: 60 * SECOND,
  audit: 15 * SECOND,
  approvals: 8 * SECOND,
  reconcile: 30 * SECOND,
  providers: 120 * SECOND,
  catalog: 300 * SECOND,
} as const;

function scope(token: string | null): string {
  // The token itself is never a cache key -- it would end up in devtools, in a React
  // profiler export, and in anything that serialises the query cache. Its length and last
  // two characters are enough to make two different tokens different keys.
  return token ? `t${token.length}:${token.slice(-2)}` : "anonymous";
}

export const keys = {
  stats: (token: string | null, within: number) => ["stats", scope(token), within] as const,
  catalog: (token: string | null) => ["catalog", scope(token)] as const,
  catalogEntry: (token: string | null, id: string) => ["catalog", scope(token), id] as const,
  leases: (token: string | null, filters: LeaseFilters) =>
    ["leases", scope(token), filters] as const,
  lease: (token: string | null, id: string) => ["lease", scope(token), id] as const,
  audit: (token: string | null, id: string) => ["audit", scope(token), id] as const,
  bindings: (token: string | null, id: string) => ["bindings", scope(token), id] as const,
  approvals: (token: string | null) => ["approvals", scope(token)] as const,
  reconcileRuns: (token: string | null, provider?: string) =>
    ["reconcile-runs", scope(token), provider ?? "all"] as const,
  reconcileRun: (token: string | null, id: string) => ["reconcile-run", scope(token), id] as const,
  providers: (token: string | null) => ["providers", scope(token)] as const,
};

/**
 * Retrying a refusal is pointless and, for a 401, actively harmful: three retries per
 * query across six queries is eighteen failed authentications per poll, which is what an
 * intrusion detection system is built to notice. An unreachable broker is worth one retry,
 * because a dev server restarting is the common case.
 */
// The `error` parameter is annotated `Error` rather than `unknown` on purpose: TanStack
// infers a query's TError from this callback's signature, so `unknown` here would make
// every hook below return UseQueryResult<T, unknown> and disagree with its own declared
// return type. Everything api.ts throws is an Error, and ApiError extends it.
function retryPolicy(failureCount: number, error: Error): boolean {
  if (error instanceof ApiError) return false;
  return failureCount < 1;
}

const common = { retry: retryPolicy, refetchOnWindowFocus: true } as const;

export function useStats(within = 3600): UseQueryResult<Stats, Error> {
  const token = useToken();
  return useQuery({
    ...common,
    queryKey: keys.stats(token, within),
    queryFn: ({ signal }) => api.stats(within, signal),
    refetchInterval: POLL.stats,
  });
}

export function useCatalog(): UseQueryResult<CatalogList, Error> {
  const token = useToken();
  return useQuery({
    ...common,
    queryKey: keys.catalog(token),
    queryFn: ({ signal }) => api.catalog(signal),
    refetchInterval: POLL.catalog,
    staleTime: POLL.catalog,
  });
}

export function useCatalogEntry(id: string | null): UseQueryResult<CatalogDetail, Error> {
  const token = useToken();
  return useQuery({
    ...common,
    queryKey: keys.catalogEntry(token, id ?? ""),
    queryFn: ({ signal }) => api.catalogEntry(id as string, signal),
    enabled: Boolean(id),
    staleTime: POLL.catalog,
  });
}

export function useLeases(filters: LeaseFilters): UseQueryResult<LeasePage, Error> {
  const token = useToken();
  return useQuery({
    ...common,
    queryKey: keys.leases(token, filters),
    queryFn: ({ signal }) => api.leases(filters, signal),
    refetchInterval: POLL.leases,
    // Keeps the previous page on screen while a filter change is in flight, so the table
    // does not blink back to a skeleton every time somebody ticks a checkbox.
    placeholderData: (previous) => previous,
  });
}

/** A lease, polled fast while anything can still happen to it and slowly once it cannot. */
export function useLease(id: string): UseQueryResult<Lease, Error> {
  const token = useToken();
  return useQuery({
    ...common,
    queryKey: keys.lease(token, id),
    queryFn: ({ signal }) => api.lease(id, signal),
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      if (!state) return POLL.leaseLive;
      // Terminal states are the state machine's own list. A lease in one of them will never
      // move again, so polling it every five seconds is asking a question with a known
      // answer -- but it still gets polled, slowly, because the page may have been left
      // open across a broker restart and a stale detail view is worse than a slow one.
      return TERMINAL_STATES.includes(state) ? POLL.leaseSettled : POLL.leaseLive;
    },
  });
}

export function useAudit(id: string): UseQueryResult<AuditTrail, Error> {
  const token = useToken();
  return useQuery({
    ...common,
    queryKey: keys.audit(token, id),
    queryFn: ({ signal }) => api.leaseAudit(id, 200, signal),
    refetchInterval: POLL.audit,
  });
}

export function useBindings(id: string): UseQueryResult<BindingList, Error> {
  const token = useToken();
  return useQuery({
    ...common,
    queryKey: keys.bindings(token, id),
    queryFn: ({ signal }) => api.leaseBindings(id, signal),
    refetchInterval: POLL.audit,
  });
}

export function useApprovals(): UseQueryResult<ApprovalQueue, Error> {
  const token = useToken();
  return useQuery({
    ...common,
    queryKey: keys.approvals(token),
    queryFn: ({ signal }) => api.approvals(50, 0, signal),
    refetchInterval: POLL.approvals,
  });
}

export function useReconcileRuns(provider?: string): UseQueryResult<ReconcileRunPage, Error> {
  const token = useToken();
  return useQuery({
    ...common,
    queryKey: keys.reconcileRuns(token, provider),
    queryFn: ({ signal }) => api.reconcileRuns(provider, 50, signal),
    refetchInterval: POLL.reconcile,
  });
}

export function useReconcileRun(id: string | null): UseQueryResult<ReconcileRunDetail, Error> {
  const token = useToken();
  return useQuery({
    ...common,
    queryKey: keys.reconcileRun(token, id ?? ""),
    queryFn: ({ signal }) => api.reconcileRun(id as string, signal),
    enabled: Boolean(id),
  });
}

export function useProviders(): UseQueryResult<ProviderList, Error> {
  const token = useToken();
  return useQuery({
    ...common,
    queryKey: keys.providers(token),
    queryFn: ({ signal }) => api.providers(signal),
    refetchInterval: POLL.providers,
  });
}

// --------------------------------------------------------------------------------------
// Mutations
// --------------------------------------------------------------------------------------

/**
 * After anything that changes a lease, throw away every cached read.
 *
 * Blunt, and correct. An approval changes the approval queue, the lease, its audit trail,
 * the state histogram and the spend estimate; enumerating those five and getting one wrong
 * produces a dashboard that shows an approved request still sitting in the queue, which is
 * the single most alarming thing this screen could do.
 */
function useInvalidateAll(): () => Promise<void> {
  const client = useQueryClient();
  return async () => {
    await client.invalidateQueries();
  };
}

export function useProvision(): UseMutationResult<ProvisionResult, Error, ProvisionRequestBody> {
  const invalidate = useInvalidateAll();
  return useMutation({
    mutationFn: (body: ProvisionRequestBody) => api.provision(body),
    onSuccess: invalidate,
  });
}

export function useRenew(
  leaseId: string,
): UseMutationResult<ProvisionResult, Error, RenewRequestBody> {
  const invalidate = useInvalidateAll();
  return useMutation({
    mutationFn: (body: RenewRequestBody) => api.renew(leaseId, body),
    onSuccess: invalidate,
  });
}

export function useRevoke(leaseId: string): UseMutationResult<Lease, Error, RevokeRequestBody> {
  const invalidate = useInvalidateAll();
  return useMutation({
    mutationFn: (body: RevokeRequestBody) => api.revoke(leaseId, body),
    onSuccess: invalidate,
  });
}

export function useRetryTeardown(leaseId: string): UseMutationResult<Lease, Error, void> {
  const invalidate = useInvalidateAll();
  return useMutation({
    mutationFn: () => api.retryTeardown(leaseId),
    onSuccess: invalidate,
  });
}

export function useApprove(): UseMutationResult<
  Lease,
  Error,
  { leaseId: string; body: ApproveRequestBody }
> {
  const invalidate = useInvalidateAll();
  return useMutation({
    mutationFn: ({ leaseId, body }: { leaseId: string; body: ApproveRequestBody }) =>
      api.approve(leaseId, body),
    onSuccess: invalidate,
  });
}

export function useReject(): UseMutationResult<
  Lease,
  Error,
  { leaseId: string; body: RejectRequestBody }
> {
  const invalidate = useInvalidateAll();
  return useMutation({
    mutationFn: ({ leaseId, body }: { leaseId: string; body: RejectRequestBody }) =>
      api.reject(leaseId, body),
    onSuccess: invalidate,
  });
}

/**
 * Run a reconcile pass now.
 *
 * The pass runs inline on the broker, so this request takes as long as the provider calls
 * do -- deliberately, per the broker's own docstring: a fire-and-forget trigger would
 * answer immediately and leave the operator unable to tell a clean sweep from one that
 * never happened. The button that calls this must stay in its pending state until it
 * returns, however long that is.
 */
export function useReconcileNow(): UseMutationResult<ReconcileTrigger, Error, string | undefined> {
  const invalidate = useInvalidateAll();
  return useMutation({
    mutationFn: (provider?: string) => api.reconcileNow(provider),
    onSuccess: invalidate,
  });
}
