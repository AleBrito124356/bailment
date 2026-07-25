"use client";

import { CircleDot, CircleSlash, Minus, TriangleAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { OUTCOME_BLURB, OUTCOME_LABEL, type PolicyTrace } from "@/lib/policy-trace";
import type { PolicyEffect, RuleOutcome } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The policy decision, and the walk that produced it.
 *
 * The chain is rendered whole, including the rules that were never reached, because
 * "rule 3 allowed this" means something entirely different depending on what rules 1 and 2
 * said. An operator answering "why did an agent get a production branch" needs to see that
 * the prod rule was evaluated and did not match, not merely that some rule allowed it.
 *
 * The trace is reconstructed rather than stored -- see src/lib/policy-trace.ts, which
 * explains why that is exact and where its one caveat is. When the caveat bites (the YAML
 * changed after the lease was requested) this component says so instead of showing a chain
 * that never ran.
 */

const EFFECT: Record<PolicyEffect, { label: string; tone: "ok" | "neutral" | "info" }> = {
  allow: { label: "Allowed", tone: "ok" },
  deny: { label: "Denied", tone: "neutral" },
  require_approval: { label: "Needed approval", tone: "info" },
};

const OUTCOME_ICON: Record<RuleOutcome, typeof CircleDot> = {
  matched: CircleDot,
  not_matched: CircleSlash,
  error: TriangleAlert,
  not_evaluated: Minus,
};

const OUTCOME_STYLE: Record<RuleOutcome, string> = {
  matched: "border-accent/40 bg-accent-soft",
  not_matched: "border-border bg-card",
  error: "border-warn/40 bg-warn-soft",
  not_evaluated: "border-dashed border-border bg-card opacity-60",
};

export function PolicyTraceView({ trace }: { trace: PolicyTrace }) {
  const effect = trace.effect as PolicyEffect | null;
  const summary = effect && effect in EFFECT ? EFFECT[effect] : null;

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="flex flex-wrap items-center gap-2">
          {summary ? (
            <Badge tone={summary.tone} dot>
              {summary.label}
            </Badge>
          ) : (
            <Badge tone="neutral">No decision recorded</Badge>
          )}
          {trace.decidingIndex !== null ? (
            <span className="text-xs text-muted-foreground">
              by rule {trace.decidingIndex} of {trace.rules.length}
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">
              decided before or beyond the rule chain
            </span>
          )}
        </div>
        {trace.reason ? (
          <p className="mt-2.5 text-sm leading-relaxed text-foreground">{trace.reason}</p>
        ) : null}
        {trace.error ? (
          <p className="mt-2 rounded-md border border-warn/30 bg-warn-soft px-3 py-2 text-xs leading-relaxed text-warn">
            The rule could not be evaluated: {trace.error}. A rule that cannot be evaluated
            denies the request rather than being skipped — skipping it would turn a deny into
            an allow at exactly the moment nobody is looking.
          </p>
        ) : null}
      </div>

      {trace.staleRules ? (
        <p className="rounded-md border border-warn/30 bg-warn-soft px-3 py-2 text-xs leading-relaxed text-warn">
          This lease was decided by a rule the golden path no longer has, so its policy file
          has been edited since. The chain below is today&apos;s, and no outcome is claimed for
          any rule in it.
        </p>
      ) : null}

      {trace.rules.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          The rule chain for this golden path is not available — the catalog entry could not be
          read, or the path has been removed since.
        </p>
      ) : (
        <ol className="space-y-2">
          {trace.rules.map(({ rule, outcome, deciding }) => {
            const Icon = OUTCOME_ICON[outcome];
            return (
              <li
                key={rule.index}
                className={cn("rounded-lg border p-3.5", OUTCOME_STYLE[outcome])}
              >
                <div className="flex items-start gap-2.5">
                  <Icon
                    className={cn(
                      "mt-0.5 h-3.5 w-3.5 shrink-0",
                      outcome === "matched"
                        ? "text-accent"
                        : outcome === "error"
                          ? "text-warn"
                          : "text-muted-foreground",
                    )}
                    aria-hidden
                  />
                  <div className="min-w-0 flex-1 space-y-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-2xs text-muted-foreground">
                        rule {rule.index}
                      </span>
                      <Badge tone={rule.effect === "allow" ? "ok" : "neutral"}>
                        {rule.effect}
                      </Badge>
                      <span
                        className={cn(
                          "text-2xs font-medium",
                          deciding ? "text-accent" : "text-muted-foreground",
                        )}
                        title={OUTCOME_BLURB[outcome]}
                      >
                        {OUTCOME_LABEL[outcome]}
                      </span>
                      {rule.approvers.length > 0 ? (
                        <span className="text-2xs text-muted-foreground">
                          approvers: {rule.approvers.join(", ")}
                        </span>
                      ) : null}
                    </div>

                    {rule.when ? (
                      <pre className="scroll-x rounded border border-border bg-background px-2.5 py-1.5 font-mono text-[12px] leading-relaxed text-foreground">
                        {rule.when}
                      </pre>
                    ) : (
                      <p className="text-2xs italic text-muted-foreground">
                        unconditional — the default every path must end with
                      </p>
                    )}

                    <p className="text-xs leading-relaxed text-muted-foreground">{rule.reason}</p>
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
