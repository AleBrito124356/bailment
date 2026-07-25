"use client";

import { Info } from "lucide-react";

import { FieldError, FieldHint, Input, Label } from "@/components/ui/field";
import { formatDuration, formatUsd } from "@/lib/format";
import { parseDuration, toDurationString } from "@/lib/schema-form";
import type { CostEstimate, LeaseTerms } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * How long the lease runs for.
 *
 * `ttl` is advertised inside the path's input schema because an MCP tool has exactly one
 * argument object, but it is not one of the path's inputs -- the broker peels it off before
 * the closed schema sees it. So it gets its own control here, and the dashboard sends it as
 * the request body's own `ttl` field.
 *
 * The control does the arithmetic the requester would otherwise do in their head: what this
 * costs for that long, and what happens if they ask for more than the ceiling. Asking above
 * the ceiling is *clamped, not rejected*, which is easy to miss and produces a lease that
 * quietly ends sooner than the person who asked for it expects. Saying so before they
 * submit is cheaper than saying so afterwards in a notice.
 */
export function TtlField({
  terms,
  cost,
  value,
  error,
  onChange,
}: {
  terms: LeaseTerms;
  cost: CostEstimate;
  value: string | null;
  error?: string;
  onChange: (value: string | null) => void;
}) {
  const parsed = value ? parseDuration(value) : null;
  const effective =
    parsed === null ? terms.default_ttl_seconds : Math.min(parsed, terms.max_ttl_seconds);
  const clamped = parsed !== null && parsed > terms.max_ttl_seconds;
  const malformed = Boolean(value && value.trim() && parsed === null);

  const presets = buildPresets(terms);
  const estimate = (cost.estimated_hourly_usd * effective) / 3600;

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-3">
        <Label htmlFor="field-ttl">
          <span className="font-mono text-[13px]">ttl</span>
        </Label>
        <span className="text-2xs text-muted-foreground">
          default {terms.default_ttl} · ceiling {terms.max_ttl}
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {presets.map((preset) => {
          const active = (value ?? "") === preset.value || (value === null && preset.isDefault);
          return (
            <button
              key={preset.value}
              type="button"
              onClick={() => onChange(preset.isDefault ? null : preset.value)}
              className={cn(
                "rounded-md border px-2.5 py-1 text-[13px] font-medium transition-colors",
                active
                  ? "border-accent bg-accent-soft text-accent"
                  : "border-border text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              {preset.label}
            </button>
          );
        })}
      </div>

      <Input
        id="field-ttl"
        value={value ?? ""}
        spellCheck={false}
        aria-invalid={Boolean(error) || malformed}
        placeholder={`${terms.default_ttl} (the default for this path)`}
        onChange={(event) => onChange(event.target.value || null)}
      />

      <FieldHint>
        Compact durations only: <span className="font-mono">2h</span>,{" "}
        <span className="font-mono">45m</span>, <span className="font-mono">1h30m</span>.
      </FieldHint>

      {malformed ? (
        <FieldError>
          {value} is not a duration this broker parses. Use hours, minutes and seconds, e.g. 2h
          or 90m.
        </FieldError>
      ) : (
        <FieldError>{error}</FieldError>
      )}

      <div
        className={cn(
          "flex items-start gap-2 rounded-md border px-2.5 py-2 text-xs leading-relaxed",
          clamped
            ? "border-warn/30 bg-warn-soft text-warn"
            : "border-border bg-muted/40 text-muted-foreground",
        )}
      >
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
        <span>
          {clamped ? (
            <>
              {formatDuration(parsed ?? 0)} is above this path&apos;s ceiling of {terms.max_ttl}.
              The request is not rejected — the broker clamps it to {terms.max_ttl} and says so
              in the response.{" "}
            </>
          ) : null}
          The lease will run for <span className="font-medium">{formatDuration(effective)}</span>
          {cost.estimated_hourly_usd > 0 ? (
            <>
              , an estimated <span className="font-medium">{formatUsd(estimate)}</span> at{" "}
              {formatUsd(cost.estimated_hourly_usd)}/hour
            </>
          ) : (
            <> at no cost</>
          )}
          .{" "}
          {terms.renewable
            ? `Renewable ${terms.max_renewals} ${terms.max_renewals === 1 ? "time" : "times"}, measured from the moment you renew rather than from the old deadline.`
            : "This path is not renewable; ask for the duration you need up front."}
        </span>
      </div>
    </div>
  );
}

interface Preset {
  value: string;
  label: string;
  isDefault: boolean;
}

/**
 * Presets are derived from the path rather than fixed, because "1h, 4h, 8h" is nonsense on
 * the sandbox path, whose whole lease is five minutes long.
 */
function buildPresets(terms: LeaseTerms): Preset[] {
  const seen = new Set<string>();
  const out: Preset[] = [
    { value: terms.default_ttl, label: `${terms.default_ttl} · default`, isDefault: true },
  ];
  seen.add(terms.default_ttl);

  const candidates = [
    terms.default_ttl_seconds * 2,
    terms.default_ttl_seconds * 4,
    terms.max_ttl_seconds,
  ];

  for (const seconds of candidates) {
    if (seconds <= terms.default_ttl_seconds || seconds > terms.max_ttl_seconds) continue;
    const label = toDurationString(seconds);
    if (seen.has(label)) continue;
    seen.add(label);
    out.push({
      value: label,
      label: seconds === terms.max_ttl_seconds ? `${label} · max` : label,
      isDefault: false,
    });
  }

  return out;
}
