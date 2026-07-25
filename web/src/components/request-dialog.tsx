"use client";

import * as React from "react";
import Link from "next/link";
import { CircleCheck, Info, Lock, TriangleAlert } from "lucide-react";

import { SchemaField } from "@/components/form/schema-field";
import { TtlField } from "@/components/form/ttl-field";
import { StateBadge } from "@/components/state-badge";
import { Badge } from "@/components/ui/badge";
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
import { ApiError } from "@/lib/api";
import { formatDuration, formatUsd } from "@/lib/format";
import { useProvision } from "@/lib/queries";
import {
  assignProblems,
  buildForm,
  initialValues,
  parseDuration,
  toInputs,
  validateAll,
  type FormValues,
} from "@/lib/schema-form";
import type { CatalogEntry, ProvisionResult } from "@/lib/types";

/**
 * The request form, generated from the golden path's own JSON Schema.
 *
 * Nothing in this component knows what a Postgres branch or a DNS record is. It is handed
 * `input_schema` -- the same object the agent's MCP tool is built from -- and renders
 * whatever that says. Add an input to a YAML file and it appears here on the next reload,
 * in both surfaces, with no code change in either. That is the property the whole project
 * is arguing for, so the dashboard had better demonstrate it.
 *
 * **The idempotency key is derived from the request, not from the dialog.** It is
 * regenerated whenever the inputs or the TTL change and held stable while they do not, so
 * pressing submit twice on the same request returns the same lease and never provisions a
 * second resource -- while editing a field and submitting again is a genuinely new request
 * rather than a replay of the old one. Keying it to the dialog session would make the
 * second case silently return the first lease; omitting it entirely would make a retry
 * after a timeout create two databases.
 */
export function RequestDialog({
  entry,
  open,
  onOpenChange,
}: {
  entry: CatalogEntry;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const form = React.useMemo(() => buildForm(entry.input_schema), [entry.input_schema]);
  const [values, setValues] = React.useState<FormValues>(() => initialValues(form));
  const [ttl, setTtl] = React.useState<string | null>(null);
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [result, setResult] = React.useState<ProvisionResult | null>(null);
  const idempotency = React.useRef<{ signature: string; key: string } | null>(null);

  const provision = useProvision();

  React.useEffect(() => {
    if (!open) return;
    setValues(initialValues(form));
    setTtl(null);
    setErrors({});
    setResult(null);
    provision.reset();
    idempotency.current = null;
    // `provision` is a stable mutation object; re-running this on every render of it would
    // wipe the form while a request is in flight.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, form]);

  const blocked = !entry.provider_available
    ? `The ${entry.provider} provider is registered but not configured on this broker, so nothing on this path can be created yet.`
    : !entry.enabled
      ? "This golden path is disabled. Only operators can see it, and nobody can provision it."
      : null;

  function keyFor(inputs: Record<string, unknown>, chosenTtl: string | null): string {
    const signature = JSON.stringify({ id: entry.id, inputs, ttl: chosenTtl });
    if (idempotency.current?.signature !== signature) {
      idempotency.current = { signature, key: newKey() };
    }
    return idempotency.current.key;
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (blocked) return;

    const problems = validateAll(form.fields, values);
    if (ttl && parseDuration(ttl) === null) {
      problems.ttl = "is not a duration this broker parses";
    }
    setErrors(problems);
    if (Object.keys(problems).length > 0) return;

    const inputs = toInputs(form.fields, values);
    provision.mutate(
      {
        golden_path: entry.id,
        inputs,
        ttl: ttl || null,
        idempotency_key: keyFor(inputs, ttl),
      },
      {
        onSuccess: (outcome) => setResult(outcome),
        onError: (error) => {
          if (error instanceof ApiError && error.problems.length > 0) {
            const { byField, general } = assignProblems(form.fields, error.problems);
            setErrors(general.length > 0 ? { ...byField, __general: general.join(" ") } : byField);
          }
        },
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent widthClass="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{entry.name}</DialogTitle>
          <DialogDescription>
            {result
              ? "The broker has accepted the request. What happens next depends on its policy."
              : entry.description.split("\n\n")[0]}
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <>
            <DialogBody>
              <Outcome result={result} />
            </DialogBody>
            <DialogFooter>
              <Button variant="secondary" onClick={() => onOpenChange(false)}>
                Close
              </Button>
              <Button asChild variant="primary">
                <Link href={`/leases/${result.lease.id}`}>Open the lease</Link>
              </Button>
            </DialogFooter>
          </>
        ) : (
          <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
            <DialogBody className="space-y-6">
              {blocked ? (
                <p className="flex items-start gap-2 rounded-md border border-warn/30 bg-warn-soft px-3 py-2.5 text-sm leading-relaxed text-warn">
                  <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
                  {blocked}
                </p>
              ) : null}

              {form.fields.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  This path takes no arguments. Choose how long you want it for.
                </p>
              ) : (
                <div className="space-y-5">
                  {form.fields.map((field) => (
                    <SchemaField
                      key={field.name}
                      field={field}
                      value={values[field.name] ?? null}
                      error={errors[field.name]}
                      onChange={(next) =>
                        setValues((current) => ({ ...current, [field.name]: next }))
                      }
                    />
                  ))}
                </div>
              )}

              <div className="border-t border-border pt-5">
                <TtlField
                  terms={entry.lease}
                  cost={entry.cost}
                  value={ttl}
                  error={errors.ttl}
                  onChange={setTtl}
                />
              </div>

              <Outputs entry={entry} />

              {errors.__general ? (
                <p className="rounded-md border border-danger/25 bg-danger-soft px-3 py-2 text-sm text-danger">
                  {errors.__general}
                </p>
              ) : null}

              {provision.isError && !(provision.error instanceof ApiError && provision.error.problems.length) ? (
                <p className="rounded-md border border-danger/25 bg-danger-soft px-3 py-2 text-sm leading-relaxed text-danger">
                  {provision.error.message}
                </p>
              ) : null}
            </DialogBody>

            <DialogFooter>
              <span className="mr-auto text-xs text-muted-foreground">
                {entry.cost.estimated_hourly_usd > 0
                  ? `About ${formatUsd(entry.cost.estimated_monthly_usd)}/month if it ran continuously.`
                  : "This path is free."}
              </span>
              <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button
                type="submit"
                variant="primary"
                loading={provision.isPending}
                disabled={Boolean(blocked)}
              >
                Request lease
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

/**
 * What the lease will hand back, said before it is asked for.
 *
 * The sealed outputs are listed by name with a lock, and the copy says plainly that the
 * value is never shown here. That is not an apology for a missing feature -- it is the
 * feature, and an operator who understands it on this screen does not go looking for a
 * "reveal" button on the next one.
 */
function Outputs({ entry }: { entry: CatalogEntry }) {
  if (entry.outputs.length === 0) return null;
  const sealed = entry.outputs.filter((output) => output.secret);

  return (
    <div className="rounded-md border border-border bg-muted/30 p-3">
      <p className="text-2xs font-semibold uppercase tracking-wider text-muted-foreground">
        What you get back
      </p>
      <ul className="mt-2 space-y-1.5">
        {entry.outputs.map((output) => (
          <li key={output.name} className="flex items-start gap-2 text-sm">
            {output.secret ? (
              <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
            ) : (
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-muted-foreground/50" />
            )}
            <span className="min-w-0">
              <span className="font-mono text-[13px] text-foreground">{output.name}</span>
              {output.secret ? (
                <Badge tone="neutral" className="ml-2 align-middle">
                  sealed
                </Badge>
              ) : null}
              {output.description ? (
                <span className="block text-xs leading-relaxed text-muted-foreground">
                  {output.description}
                </span>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
      {sealed.length > 0 ? (
        <p className="mt-3 border-t border-border pt-2.5 text-xs leading-relaxed text-muted-foreground">
          Sealed values are never returned by this API, to this dashboard or to an agent. You
          get a <span className="font-mono">bailment://binding/…</span> reference, and{" "}
          <span className="font-mono">bailment exec &lt;lease-id&gt; -- &lt;command&gt;</span>{" "}
          injects the real value into that one process.
        </p>
      ) : null}
    </div>
  );
}

function Outcome({ result }: { result: ProvisionResult }) {
  const { lease, notices, replayed, ttl_clamped } = result;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2.5">
        <CircleCheck className="h-4 w-4 text-ok" aria-hidden />
        <StateBadge state={lease.state} />
        <span className="font-mono text-[13px] text-muted-foreground">{lease.id}</span>
      </div>

      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
        <dt className="text-muted-foreground">Golden path</dt>
        <dd className="font-mono text-[13px]">{lease.golden_path}</dd>
        <dt className="text-muted-foreground">Lease</dt>
        <dd>{formatDuration(lease.ttl_seconds)}</dd>
        {lease.external_name ? (
          <>
            <dt className="text-muted-foreground">Resource name</dt>
            <dd className="truncate font-mono text-[13px]">{lease.external_name}</dd>
          </>
        ) : null}
      </dl>

      {replayed ? (
        <Note>
          This request replayed an idempotency key you had already used, so it returned the
          existing lease. Nothing new was created.
        </Note>
      ) : null}

      {ttl_clamped ? (
        <Note>The TTL you asked for was above this path&apos;s ceiling and was clamped.</Note>
      ) : null}

      {notices.map((notice) => (
        <Note key={notice}>{notice}</Note>
      ))}
    </div>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <p className="flex items-start gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm leading-relaxed text-muted-foreground">
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      <span>{children}</span>
    </p>
  );
}

/**
 * A key that is unique per request attempt.
 *
 * `crypto.randomUUID` needs a secure context, which localhost is and plain HTTP on a LAN
 * address is not. The fallback is not cryptography -- an idempotency key only has to not
 * collide with another request from the same principal -- so time plus randomness is
 * enough, and losing idempotency entirely because a demo is served over HTTP would be the
 * worse outcome.
 */
function newKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `dash-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
