import { serverNow } from "@/lib/clock";

/**
 * Rendering helpers. Small, boring, and centralised so that a duration reads the same way
 * on every page -- an operator comparing "4h" on the catalog with "03:59:12" in the lease
 * table should not have to work out whether those are the same unit.
 */

function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

/**
 * A ticking clock: `1:04:11` over an hour, `04:11` under one, `-00:42` once overdue.
 *
 * Overdue is rendered as a negative rather than as zero. A lease whose deadline passed
 * ninety seconds ago and whose resource is still up is a fact worth showing.
 */
export function formatCountdown(seconds: number): string {
  const sign = seconds < 0 ? "-" : "";
  const total = Math.abs(Math.trunc(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours > 0
    ? `${sign}${hours}:${pad(minutes)}:${pad(secs)}`
    : `${sign}${pad(minutes)}:${pad(secs)}`;
}

/** A static duration, in the same compact form the golden path YAML uses. `1h 30m`. */
export function formatDuration(seconds: number): string {
  const total = Math.abs(Math.trunc(seconds));
  if (total === 0) return "0s";
  const days = Math.floor(total / 86_400);
  const hours = Math.floor((total % 86_400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;

  const parts: string[] = [];
  if (days) parts.push(`${days}d`);
  if (hours) parts.push(`${hours}h`);
  if (minutes) parts.push(`${minutes}m`);
  // Seconds only matter when they are all there is; "1h 4m 12s" is noise on a TTL.
  if (secs && !days && !hours) parts.push(`${secs}s`);
  return parts.slice(0, 2).join(" ");
}

/**
 * Money.
 *
 * Two decimals stops being honest below a cent, and several of the shipped golden paths
 * cost fractions of one per hour ($0.005 for a Redis database). Rounding those to $0.01
 * would inflate an estimate by a factor of two; rounding them to $0.00 would render a real
 * cost as free.
 */
export function formatUsd(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const digits = value !== 0 && Math.abs(value) < 0.1 ? 4 : 2;
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  }).format(value);
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat().format(value);
}

const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

const RELATIVE_STEPS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ["second", 60],
  ["minute", 60],
  ["hour", 24],
  ["day", 7],
  ["week", 4.348],
  ["month", 12],
  ["year", Number.POSITIVE_INFINITY],
];

/**
 * "4 minutes ago", measured against the broker's clock rather than this workstation's.
 *
 * See src/lib/clock.ts: the offset comes from `/stats.at`. Before any stats response has
 * landed the offset is zero, which is the same answer an uncorrected clock would give --
 * so this is never worse than the naive version and is usually better.
 */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const timestamp = Date.parse(iso);
  if (Number.isNaN(timestamp)) return "—";

  let delta = (timestamp - serverNow()) / 1000;
  if (Math.abs(delta) < 5) return "just now";

  for (const [unit, span] of RELATIVE_STEPS) {
    if (Math.abs(delta) < span) return RELATIVE.format(Math.round(delta), unit);
    delta /= span;
  }
  return RELATIVE.format(Math.round(delta), "year");
}

const ABSOLUTE = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
});

/** The full local timestamp, for tooltips and detail rows. */
export function formatAbsolute(iso: string | null | undefined): string {
  if (!iso) return "—";
  const timestamp = Date.parse(iso);
  if (Number.isNaN(timestamp)) return "—";
  return ABSOLUTE.format(timestamp);
}

/** An audit action id (`approval_requested`) as a sentence fragment. */
export function humanise(token: string): string {
  return token.replace(/[_-]+/g, " ").replace(/^./, (c) => c.toUpperCase());
}

/** Shorten a lease id for a table cell without making two of them look identical. */
export function shortId(id: string): string {
  return id.length <= 12 ? id : `${id.slice(0, 8)}…${id.slice(-4)}`;
}

/** Render an input value for display. Objects become compact JSON, never "[object]". */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return JSON.stringify(value);
}
