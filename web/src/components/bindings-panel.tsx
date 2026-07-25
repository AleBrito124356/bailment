"use client";

import type * as React from "react";
import { EyeOff, Lock } from "lucide-react";

import { CopyableCode, CopyButton } from "@/components/copy-button";
import { EmptyState } from "@/components/empty-state";
import { Timestamp } from "@/components/timestamp";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { formatCount } from "@/lib/format";
import type { Binding } from "@/lib/types";

/**
 * Binding references, and a deliberate absence.
 *
 * This panel exists to make one thing unmistakable: the credential is not shown here
 * because **no API in this system returns it**, not because the dashboard has not got round
 * to displaying it. There is no reveal button to look for, no permission that unlocks one,
 * and no query parameter that changes the answer. The broker's own response model refuses
 * at import time to carry a field that could hold a value, and its route module proves at
 * import time that no handler can reach the code that decrypts one.
 *
 * The distinction matters because "missing" invites a workaround and "withheld by design"
 * does not. An operator who reads this panel and understands why should stop looking, and
 * should reach for `bailment exec` instead — which is why the command is right there,
 * pre-filled, with a copy button.
 *
 * `access_count` is the number worth watching. A binding whose count climbs while its lease
 * sits idle is the anomaly this endpoint was built to surface.
 */
export function BindingsPanel({
  bindings,
  loading = false,
  hasSecrets,
}: {
  bindings: Binding[] | undefined;
  loading?: boolean;
  /** Whether the golden path declares any sealed outputs at all. */
  hasSecrets: boolean;
}) {
  if (loading && !bindings) return <SkeletonRows rows={2} />;

  if (!bindings || bindings.length === 0) {
    return (
      <Card>
        <EmptyState
          icon={Lock}
          title="No bindings yet"
          description={
            hasSecrets
              ? "A binding is sealed when the provider confirms the resource exists. Until then there is nothing to reference."
              : "This golden path declares no sealed outputs, so it produces no bindings. Its non-secret outputs are on the lease itself."
          }
        />
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      {bindings.map((binding) => (
        <Card key={binding.reference} className="p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 space-y-2">
              <CopyableCode value={binding.reference} />
              <div className="flex flex-wrap items-center gap-1.5">
                {binding.output_names.map((name) => (
                  <Badge key={name} tone="neutral" className="font-mono">
                    <Lock className="h-3 w-3" aria-hidden />
                    {name}
                  </Badge>
                ))}
              </div>
            </div>

            <Badge tone={binding.usable ? "ok" : "neutral"} dot>
              {binding.revoked_at ? "Revoked" : binding.usable ? "Usable" : "Not usable"}
            </Badge>
          </div>

          <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
            <Fact label="Sealed">
              <Timestamp value={binding.created_at} />
            </Fact>
            <Fact label="Last resolved">
              {binding.last_accessed_at ? (
                <Timestamp value={binding.last_accessed_at} />
              ) : (
                <span className="text-muted-foreground">never</span>
              )}
            </Fact>
            <Fact label="Resolved">
              <span className="tabular">{formatCount(binding.access_count)}×</span>
            </Fact>
            <Fact label="Revoked">
              {binding.revoked_at ? (
                <Timestamp value={binding.revoked_at} />
              ) : (
                <span className="text-muted-foreground">no</span>
              )}
            </Fact>
          </dl>

          <div className="mt-4 rounded-md border border-border bg-muted/40 p-3">
            <p className="flex items-start gap-2 text-xs leading-relaxed text-muted-foreground">
              <EyeOff className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>
                <span className="font-medium text-foreground">
                  The values are withheld by design, not missing.
                </span>{" "}
                Nothing this broker serves over HTTP contains a decrypted credential — not for
                an agent, not for an operator, not behind any flag. A value in a response body
                is a value in the proxy log, the browser, and every tool that records
                payloads. Only the CLI decrypts, into one process:
              </span>
            </p>
            <div className="mt-2.5 flex items-center gap-2">
              <code className="scroll-x block flex-1 rounded border border-border bg-background px-2.5 py-1.5 font-mono text-[12px] text-foreground">
                {binding.how_to_use}
              </code>
              <CopyButton value={binding.how_to_use} label="Copy the command" />
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-2xs uppercase tracking-wider text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-[13px]">{children}</dd>
    </div>
  );
}
