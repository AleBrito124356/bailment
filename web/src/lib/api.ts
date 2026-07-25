"use client";

import { getToken } from "@/lib/auth";
import { recordServerTime } from "@/lib/clock";
import type {
  ApprovalQueue,
  ApproveRequestBody,
  AuditTrail,
  BindingList,
  CatalogDetail,
  CatalogList,
  ErrorDetail,
  Lease,
  LeasePage,
  LeaseState,
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

/**
 * The one place the dashboard talks to the broker.
 *
 * Three things this module is careful about:
 *
 * **Unreachable is not the same as refused.** `fetch` rejects with a bare TypeError when
 * the broker is down, the origin is wrong, or CORS blocked the response, and all three
 * look identical to application code that only catches HTTP status codes. They get their
 * own error class, because the fix for each is different and a dashboard that says
 * "something went wrong" for all of them costs somebody an afternoon.
 *
 * **The error envelope is parsed, not guessed at.** Every refusal the broker makes carries
 * `{detail: {error, message, problems}}`, and `message` is written for whoever was
 * blocked. Showing that verbatim is better than anything the dashboard could invent.
 *
 * **The token is attached here and nowhere else.** No caller passes one; nothing else
 * reads it. See src/lib/auth.ts.
 */

const RAW_BASE = process.env.NEXT_PUBLIC_BAILMENT_API ?? "http://127.0.0.1:8000";

/** The broker origin, without a trailing slash. */
export const API_ORIGIN = RAW_BASE.replace(/\/+$/, "");

/**
 * Versioned, and hard-coded on purpose -- the broker's own module docstring says the CLI,
 * the dashboard and third-party clients all pin it.
 */
export const API_PREFIX = "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly problems: string[];

  constructor(status: number, code: string, message: string, problems: string[] = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.problems = problems;
  }

  /** No usable bearer token. The operator needs to paste one. */
  get needsToken(): boolean {
    return this.status === 401;
  }

  /** Authenticated, but this endpoint is operator-tier. */
  get needsOperator(): boolean {
    return this.status === 403;
  }

  get notFound(): boolean {
    return this.status === 404;
  }

  /** A state conflict: already decided, not renewable, window closed. Not a bug. */
  get conflict(): boolean {
    return this.status === 409;
  }
}

/** The broker could not be reached at all. Distinct from any answer it might have given. */
export class UnreachableError extends Error {
  readonly origin: string;

  constructor(origin: string, cause?: unknown) {
    super(
      `could not reach the bailment broker at ${origin}. Check that it is running, that ` +
        `NEXT_PUBLIC_BAILMENT_API points at it, and that BAILMENT_PUBLIC_BASE_URL on the ` +
        `broker names this dashboard's origin so CORS lets the response through.`,
    );
    this.name = "UnreachableError";
    this.origin = origin;
    if (cause !== undefined) this.cause = cause;
  }
}

function isErrorDetail(value: unknown): value is ErrorDetail {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.error === "string" && typeof candidate.message === "string";
}

async function readError(response: Response): Promise<ApiError> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return new ApiError(
      response.status,
      "unparseable_error",
      `the broker answered ${response.status} ${response.statusText} with a body this ` +
        `dashboard could not read`,
    );
  }

  const detail = (body as { detail?: unknown } | null)?.detail;
  if (isErrorDetail(detail)) {
    const problems = Array.isArray(detail.problems)
      ? detail.problems.filter((p): p is string => typeof p === "string")
      : [];
    return new ApiError(response.status, detail.error, detail.message, problems);
  }

  // FastAPI's own validation shape, or a proxy's error page. Neither should reach a
  // browser, but rendering "[object Object]" when one does helps nobody.
  return new ApiError(
    response.status,
    "unexpected_error",
    typeof detail === "string" ? detail : `the broker answered ${response.status}`,
  );
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
  /** Extra query parameters. Undefined and null values are dropped, not sent as "null". */
  query?: Record<string, string | number | boolean | undefined | null | string[]>;
}

function buildUrl(path: string, query: RequestOptions["query"]): string {
  const url = new URL(`${API_ORIGIN}${API_PREFIX}${path}`);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value) url.searchParams.append(key, item);
    } else {
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  // The broker's CORS allow-list is Authorization, Content-Type and X-Broker-API-Version.
  // Anything else -- including X-Bailment-On-Behalf-Of -- fails preflight silently, which
  // is why the dashboard does not send it: a human at this screen acts as themselves.

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
      // No cookies. The token is the only identity, and sending credentials would invite
      // a future deployment to authenticate by session, which is how CSRF gets in.
      credentials: "omit",
      cache: "no-store",
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new UnreachableError(API_ORIGIN, cause);
  }

  if (!response.ok) throw await readError(response);
  if (response.status === 204) return undefined as T;

  const payload = (await response.json()) as T;

  // Every response the broker sends is generated against its own clock. `/stats` is the
  // one that says so explicitly, and it is the anchor relative timestamps are measured
  // from. See src/lib/clock.ts.
  if (typeof (payload as { at?: unknown })?.at === "string") {
    recordServerTime((payload as { at: string }).at);
  }

  return payload;
}

// --------------------------------------------------------------------------------------
// Endpoints
// --------------------------------------------------------------------------------------

export interface LeaseFilters {
  state?: LeaseState[];
  golden_path?: string;
  provider?: string;
  requester?: string;
  live?: boolean;
  limit?: number;
  offset?: number;
}

export const api = {
  stats: (within?: number, signal?: AbortSignal) =>
    request<Stats>("/stats", { query: { within }, signal }),

  catalog: (signal?: AbortSignal) => request<CatalogList>("/catalog", { signal }),

  catalogEntry: (id: string, signal?: AbortSignal) =>
    request<CatalogDetail>(`/catalog/${encodeURIComponent(id)}`, { signal }),

  leases: (filters: LeaseFilters = {}, signal?: AbortSignal) =>
    request<LeasePage>("/leases", {
      query: {
        state: filters.state,
        golden_path: filters.golden_path,
        provider: filters.provider,
        requester: filters.requester,
        live: filters.live ? "true" : undefined,
        limit: filters.limit,
        offset: filters.offset,
      },
      signal,
    }),

  lease: (id: string, signal?: AbortSignal) =>
    request<Lease>(`/leases/${encodeURIComponent(id)}`, { signal }),

  leaseAudit: (id: string, limit = 200, signal?: AbortSignal) =>
    request<AuditTrail>(`/leases/${encodeURIComponent(id)}/audit`, {
      query: { limit },
      signal,
    }),

  /** References and metadata. There is no endpoint that returns a binding's value. */
  leaseBindings: (id: string, signal?: AbortSignal) =>
    request<BindingList>(`/leases/${encodeURIComponent(id)}/bindings`, { signal }),

  provision: (body: ProvisionRequestBody) =>
    request<ProvisionResult>("/leases", { method: "POST", body }),

  renew: (id: string, body: RenewRequestBody) =>
    request<ProvisionResult>(`/leases/${encodeURIComponent(id)}/renew`, {
      method: "POST",
      body,
    }),

  revoke: (id: string, body: RevokeRequestBody) =>
    request<Lease>(`/leases/${encodeURIComponent(id)}/revoke`, { method: "POST", body }),

  retryTeardown: (id: string) =>
    request<Lease>(`/leases/${encodeURIComponent(id)}/retry-teardown`, { method: "POST" }),

  approvals: (limit = 50, offset = 0, signal?: AbortSignal) =>
    request<ApprovalQueue>("/approvals", { query: { limit, offset }, signal }),

  approve: (leaseId: string, body: ApproveRequestBody) =>
    request<Lease>(`/approvals/${encodeURIComponent(leaseId)}/approve`, {
      method: "POST",
      body,
    }),

  reject: (leaseId: string, body: RejectRequestBody) =>
    request<Lease>(`/approvals/${encodeURIComponent(leaseId)}/reject`, {
      method: "POST",
      body,
    }),

  reconcileRuns: (provider?: string, limit = 50, signal?: AbortSignal) =>
    request<ReconcileRunPage>("/reconcile/runs", { query: { provider, limit }, signal }),

  reconcileRun: (runId: string, signal?: AbortSignal) =>
    request<ReconcileRunDetail>(`/reconcile/runs/${encodeURIComponent(runId)}`, { signal }),

  /**
   * Runs one pass and returns what it found. This can never destroy anything: the broker
   * passes an empty destroy allow-list on every HTTP-triggered run, whatever the
   * deployment is configured to do. Destroying an orphan is `bailment reconcile --destroy`
   * at a terminal, with a confirmation that lists the resources by name.
   */
  reconcileNow: (provider?: string) =>
    request<ReconcileTrigger>("/reconcile", { method: "POST", query: { provider } }),

  providers: (signal?: AbortSignal) => request<ProviderList>("/providers", { signal }),
};
