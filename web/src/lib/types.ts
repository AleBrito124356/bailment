/**
 * The wire contract, mirrored from `src/bailment/api/schemas.py` field for field.
 *
 * This file is hand-maintained rather than generated, and the reason to keep it that way
 * is the same reason the Python module exists as a separate layer: it is the file where
 * somebody adding a field to a response has to stop and think. If a `token` or a
 * `connection_string` ever appears in a broker response, it should have to be typed out
 * here first, in a file whose docstring says it must not be.
 *
 * **The rule, restated because it is the whole product.** No response the broker sends
 * carries a decrypted secret value. `Lease.outputs` holds only the values a golden path
 * declares `secret: false`; `Lease.secret_output_names` and `Binding.output_names` are
 * names. The dashboard must never render a "value" column for a binding, because there is
 * no value to render and pretending otherwise teaches operators to look for one.
 *
 * When the API changes, change this file. The alternative -- `any` at the fetch boundary
 * -- means the dashboard renders `undefined` in production and nobody finds out until an
 * incident.
 */

// --------------------------------------------------------------------------------------
// Lease states -- mirrors bailment.states.LeaseState
// --------------------------------------------------------------------------------------

export const LEASE_STATES = [
  "pending",
  "awaiting_approval",
  "rejected",
  "provisioning",
  "active",
  "expiring",
  "expired",
  "revoked",
  "deprovisioning",
  "released",
  "failed",
  "orphaned",
  "unknown",
] as const;

export type LeaseState = (typeof LEASE_STATES)[number];

/**
 * States where a real, probably billable resource is believed to exist. Mirrors
 * `bailment.states.LIVE_STATES`.
 *
 * There is no `USABLE_STATES` here on purpose: the broker already answers that question per
 * lease, on `Lease.usable`, and a second definition in the dashboard is one that can
 * disagree with it.
 */
export const LIVE_STATES: readonly LeaseState[] = [
  "provisioning",
  "active",
  "expiring",
  "expired",
  "revoked",
  "deprovisioning",
  "orphaned",
  "unknown",
];

/** No further transition is possible. Mirrors `TERMINAL_STATES`. */
export const TERMINAL_STATES: readonly LeaseState[] = ["rejected", "released", "failed"];

export type PolicyEffect = "allow" | "deny" | "require_approval";

/** Mirrors `bailment.policy.engine.RuleOutcome`. */
export type RuleOutcome = "matched" | "not_matched" | "error" | "not_evaluated";

// --------------------------------------------------------------------------------------
// JSON Schema -- only the keywords bailment's own validator enforces
// --------------------------------------------------------------------------------------

/**
 * A subset of JSON Schema, matching what `bailment.engine.validation` actually enforces.
 *
 * Anything not listed here still arrives (the index signature keeps it), and the form
 * generator renders an unrecognised field as raw JSON with a visible note rather than
 * dropping it. Silently omitting a property the broker will validate is exactly the
 * drift this project exists to prevent.
 */
export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean | JsonSchema;
  enum?: unknown[];
  const?: unknown;
  default?: unknown;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  format?: string;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  multipleOf?: number;
  items?: JsonSchema;
  minItems?: number;
  maxItems?: number;
  uniqueItems?: boolean;
  [keyword: string]: unknown;
}

// --------------------------------------------------------------------------------------
// Errors
// --------------------------------------------------------------------------------------

export interface ErrorDetail {
  error: string;
  message: string;
  problems: string[];
}

export interface ErrorEnvelope {
  detail: ErrorDetail;
}

// --------------------------------------------------------------------------------------
// Catalog
// --------------------------------------------------------------------------------------

export interface LeaseTerms {
  default_ttl: string;
  default_ttl_seconds: number;
  max_ttl: string;
  max_ttl_seconds: number;
  warn_before: string;
  warn_before_seconds: number;
  renewable: boolean;
  max_renewals: number;
}

export interface CostEstimate {
  estimated_hourly_usd: number;
  estimated_monthly_usd: number;
  note: string;
}

export interface OutputSpec {
  name: string;
  description: string;
  /** A classification, never a value: "this output will be sealed". */
  secret: boolean;
}

export interface PolicyRuleSpec {
  index: number;
  when: string | null;
  effect: PolicyEffect;
  reason: string;
  approvers: string[];
}

export interface CatalogEntry {
  id: string;
  name: string;
  description: string;
  provider: string;
  /** False means "we offer this but nobody has configured the provider credential yet". */
  provider_available: boolean;
  enabled: boolean;
  tags: string[];
  mcp_tool_name: string;
  /** Includes the universal `ttl` argument. See `GoldenPath.input_schema_with_lease`. */
  input_schema: JsonSchema;
  lease: LeaseTerms;
  cost: CostEstimate;
  outputs: OutputSpec[];
}

export interface CatalogDetail extends CatalogEntry {
  policy: PolicyRuleSpec[];
  source: string | null;
}

export interface CatalogList {
  items: CatalogEntry[];
  count: number;
  directory: string | null;
}

// --------------------------------------------------------------------------------------
// Leases
// --------------------------------------------------------------------------------------

export interface ApprovalRecord {
  id: string;
  reason: string;
  allowed_approvers: string[];
  requested_at: string;
  deadline_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  approved: boolean | null;
  decision_note: string | null;
  url: string | null;
  pending: boolean;
}

export interface Lease {
  id: string;
  golden_path: string;
  provider: string;
  state: LeaseState;
  requester: string;
  on_behalf_of: string | null;
  inputs: Record<string, unknown>;

  created_at: string;
  activated_at: string | null;
  expires_at: string | null;
  released_at: string | null;
  /**
   * Computed by the broker against the broker's clock. This, not `expires_at`, is what
   * every countdown in the dashboard is anchored to. See src/lib/clock.ts.
   */
  seconds_remaining: number | null;

  ttl_seconds: number;
  max_ttl_seconds: number;
  renewals: number;
  max_renewals: number;
  renewable: boolean;

  policy_effect: string | null;
  policy_reason: string | null;

  external_name: string | null;
  estimated_hourly_usd: number;

  binding_reference: string | null;
  /** Only the outputs the golden path declares `secret: false`. Never a credential. */
  outputs: Record<string, string>;
  /** Names of the sealed values. Names only -- there is no endpoint that returns them. */
  secret_output_names: string[];

  approval: ApprovalRecord | null;
  failure_reason: string | null;
  usable: boolean;
}

export interface ProvisionResult {
  lease: Lease;
  replayed: boolean;
  ttl_clamped: boolean;
  notices: string[];
}

export interface LeasePage {
  items: Lease[];
  limit: number;
  offset: number;
  has_more: boolean;
  next_offset: number | null;
}

// --------------------------------------------------------------------------------------
// Bindings -- metadata only, by design
// --------------------------------------------------------------------------------------

export interface Binding {
  lease_id: string;
  reference: string;
  /** Which values the sealed envelope contains. Names only. */
  output_names: string[];
  created_at: string;
  last_accessed_at: string | null;
  /** A count that climbs while its lease sits idle is the anomaly worth noticing. */
  access_count: number;
  revoked_at: string | null;
  usable: boolean;
  how_to_use: string;
}

export interface BindingList {
  lease_id: string;
  items: Binding[];
}

// --------------------------------------------------------------------------------------
// Audit
// --------------------------------------------------------------------------------------

export interface AuditEvent {
  id: string;
  at: string;
  actor: string;
  action: string;
  from_state: string | null;
  to_state: string | null;
  detail: Record<string, unknown>;
}

export interface AuditTrail {
  lease_id: string;
  items: AuditEvent[];
  limit: number;
  offset: number;
  has_more: boolean;
}

// --------------------------------------------------------------------------------------
// Approvals
// --------------------------------------------------------------------------------------

export interface PendingApproval {
  lease_id: string;
  golden_path: string;
  provider: string;
  state: LeaseState;
  requester: string;
  on_behalf_of: string | null;
  inputs: Record<string, unknown>;
  ttl_seconds: number;
  estimated_hourly_usd: number;
  reason: string;
  allowed_approvers: string[];
  policy_reason: string | null;
  requested_at: string;
  deadline_at: string | null;
  /** Goes negative once the window has closed. Do not clamp it to zero when rendering. */
  seconds_until_deadline: number | null;
  waiting_seconds: number;
}

export interface ApprovalQueue {
  items: PendingApproval[];
  limit: number;
  offset: number;
  has_more: boolean;
}

// --------------------------------------------------------------------------------------
// Reconciliation
// --------------------------------------------------------------------------------------

export interface OrphanRecord {
  provider: string;
  external_name: string;
  provider_resource_id: string;
  reason: string;
  created_at: string | null;
  age_seconds: number | null;
  lease_id: string | null;
  lease_state: string | null;
  destroyed: boolean;
  destroy_error: string | null;
}

export interface DriftRecord {
  provider: string;
  lease_id: string;
  external_name: string;
  from_state: string;
  to_state: string;
  note: string;
}

export interface ProviderReconcileResult {
  provider: string;
  /** False means the provider was never asked. Never render that as "clean". */
  checked: boolean;
  skipped_reason: string | null;
  destroy_armed: boolean;
  resources_seen: number;
  leases_checked: number;
  within_grace: number;
  in_flight_skipped: number;
  raced: number;
  known_orphan_leases: number;
  status_unknown: string[];
  errors: string[];
  orphans: OrphanRecord[];
  drift: DriftRecord[];
}

export interface ReconcileTrigger {
  started_at: string;
  finished_at: string | null;
  /** Always false from HTTP. The API cannot arm destruction, whatever the deployment. */
  destroy_armed: boolean;
  clean: boolean;
  orphans_found: number;
  orphans_destroyed: number;
  drift_found: number;
  resources_seen: number;
  leases_checked: number;
  status_unknown: number;
  errors: number;
  providers_checked: string[];
  providers_skipped: string[];
  providers: ProviderReconcileResult[];
}

export interface ReconcileRun {
  id: string;
  provider: string;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
  resources_seen: number;
  leases_checked: number;
  orphans_found: number;
  orphans_destroyed: number;
  drift_found: number;
  errors: number;
  status_unknown: number;
  destroy_armed: boolean;
  /** A run with an unknown status is not clean. "We could not tell" is not "nothing". */
  clean: boolean;
}

/** The findings blob a stored run carries. Written by `Reconciler._record_run`. */
export interface ReconcileRunDetailBlob {
  destroy_armed?: boolean;
  grace_seconds?: number;
  within_grace?: number;
  in_flight_skipped?: number;
  raced?: number;
  known_orphan_leases?: number;
  status_unknown?: string[];
  errors?: string[];
  orphans?: OrphanRecord[];
  drift?: DriftRecord[];
  [key: string]: unknown;
}

export interface ReconcileRunDetail extends ReconcileRun {
  detail: ReconcileRunDetailBlob;
}

export interface ReconcileRunPage {
  items: ReconcileRun[];
  limit: number;
  offset: number;
  has_more: boolean;
}

// --------------------------------------------------------------------------------------
// Providers
// --------------------------------------------------------------------------------------

export interface ProviderStatus {
  name: string;
  available: boolean;
  reason: string | null;
  supports_reconciliation: boolean;
  /** Environment variable *names*, never values. Naming them is how an operator fixes it. */
  missing_settings: string[];
  golden_paths: string[];
}

export interface ProviderList {
  items: ProviderStatus[];
}

// --------------------------------------------------------------------------------------
// Stats
// --------------------------------------------------------------------------------------

export interface Stats {
  /** "all" for an operator, "own" for everyone else. Label the numbers with it. */
  scope: "all" | "own";
  /** The broker's clock at the moment it answered. Used to correct relative times. */
  at: string;
  active_leases: number;
  expiring_soon: number;
  expiring_within_seconds: number;
  awaiting_approval: number;
  orphans_outstanding: number;
  live_leases: number;
  total_leases: number;
  estimated_hourly_usd: number;
  estimated_daily_usd: number;
  estimated_monthly_usd: number;
  by_state: Partial<Record<LeaseState, number>>;
  last_reconcile: ReconcileRun | null;
}

// --------------------------------------------------------------------------------------
// Request bodies
// --------------------------------------------------------------------------------------

export interface ProvisionRequestBody {
  golden_path: string;
  inputs: Record<string, unknown>;
  ttl?: string | null;
  idempotency_key?: string | null;
}

export interface RenewRequestBody {
  ttl?: string | null;
}

export interface RevokeRequestBody {
  reason: string;
}

export interface ApproveRequestBody {
  note?: string | null;
}

export interface RejectRequestBody {
  reason: string;
}
