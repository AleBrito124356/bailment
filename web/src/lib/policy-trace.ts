import type { AuditEvent, CatalogDetail, Lease, PolicyRuleSpec, RuleOutcome } from "@/lib/types";

/**
 * Rebuilding `PolicyEngine.explain`'s per-rule trace on the lease detail page.
 *
 * **Why this is a reconstruction and not a fetch.** The broker evaluates policy once, at
 * request time, and persists the *decision* -- effect, reason, and the index of the rule
 * that produced it. It does not persist the trace. That is the right call for the broker:
 * a trace is derivable, and storing a denormalised copy of something derivable on every
 * lease row is how the stored copy eventually disagrees with the rules.
 *
 * **Why the reconstruction is exact rather than a guess.** The chain is evaluated top to
 * bottom and the first match wins -- a total order with a single decision point. Given the
 * deciding index, every other rule's outcome follows with no ambiguity: everything above
 * it was evaluated and did not match, everything below it was never evaluated at all.
 * There is exactly one case with a second possibility, and the audit trail settles it: a
 * rule that could not be *evaluated* also stops the walk, and when that happens the
 * `policy_denied` event carries the error string. So `matched` and `error` are told apart
 * by evidence rather than inferred.
 *
 * **What it does not claim to know.** If `policy_rule_index` is null, no rule decided --
 * the path was disabled, or the chain ended without one, both of which the engine handles
 * before or beyond the rules. Every rule is then reported as `not_evaluated` and the
 * decision stands on its own reason, which is precisely what the engine did.
 *
 * The rules themselves come from `/catalog/{id}`, which is the same object the engine
 * walked. If a path's YAML has been edited since the lease was requested, the trace shows
 * today's rules -- and `staleRules` says so, because a trace that silently describes a
 * different chain than the one that ran is worse than no trace.
 */

export interface TracedRule {
  rule: PolicyRuleSpec;
  outcome: RuleOutcome;
  /** The evaluation failure, when this rule denied by being unevaluable. */
  error: string | null;
  /** True for the rule that produced the decision on the lease row. */
  deciding: boolean;
}

export interface PolicyTrace {
  rules: TracedRule[];
  /** The index the broker recorded, or null when nothing in the chain decided. */
  decidingIndex: number | null;
  effect: string | null;
  reason: string | null;
  /** Set when the decision came from a rule that could not be evaluated. */
  error: string | null;
  /**
   * True when the decision names a rule index the current chain does not have, which
   * means the golden path was edited after this lease was requested.
   */
  staleRules: boolean;
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function buildPolicyTrace(
  lease: Lease,
  entry: CatalogDetail | undefined,
  events: AuditEvent[],
): PolicyTrace {
  const requested = events.find((event) => event.action === "requested");
  const denied = events.find((event) => event.action === "policy_denied");

  const decidingIndex =
    readNumber(requested?.detail.policy_rule_index) ?? readNumber(denied?.detail.rule_index);
  const error = readString(denied?.detail.error);

  const rules = entry?.policy ?? [];
  const staleRules = decidingIndex !== null && decidingIndex >= rules.length;

  const traced: TracedRule[] = rules.map((rule) => {
    let outcome: RuleOutcome = "not_evaluated";
    if (decidingIndex !== null && !staleRules) {
      if (rule.index < decidingIndex) outcome = "not_matched";
      else if (rule.index === decidingIndex) outcome = error ? "error" : "matched";
    }
    return {
      rule,
      outcome,
      error: rule.index === decidingIndex ? error : null,
      deciding: rule.index === decidingIndex && !staleRules,
    };
  });

  return {
    rules: traced,
    decidingIndex,
    effect: lease.policy_effect,
    reason: lease.policy_reason,
    error,
    staleRules,
  };
}

export const OUTCOME_LABEL: Record<RuleOutcome, string> = {
  matched: "Matched",
  not_matched: "Did not match",
  error: "Could not be evaluated",
  not_evaluated: "Not reached",
};

export const OUTCOME_BLURB: Record<RuleOutcome, string> = {
  matched: "This rule decided the request.",
  not_matched: "Evaluated against the request and returned false, so the walk continued.",
  error:
    "The expression could not be evaluated, so the request was denied. A policy that " +
    "cannot be evaluated cannot be trusted, and skipping a broken rule would turn a deny " +
    "into an allow.",
  not_evaluated: "An earlier rule already decided, so this one never ran.",
};
