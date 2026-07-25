"use client";

import { useEffect, useState } from "react";

/**
 * Time, measured the only way that is safe here.
 *
 * **Countdowns never subtract a client clock from a server timestamp.** A lease is live
 * for as long as the *broker* thinks it is, and a laptop whose clock is four minutes fast
 * would show a lease as expired while the resource is still up -- or, worse, the other way
 * round. So every countdown starts from `seconds_remaining`, which the broker computed
 * against its own clock, and ticks down by *elapsed local time since the response
 * arrived*. Two readings of the same clock give a correct interval even when that clock is
 * wrong in absolute terms. The only error left is the round trip, which is milliseconds.
 *
 * Concretely: `remaining = seconds_remaining - (now - dataUpdatedAt) / 1000`, and
 * `dataUpdatedAt` is the query cache's own stamp for when the response landed. Nothing in
 * this dashboard computes `Date.parse(expires_at) - Date.now()`.
 *
 * **Absolute timestamps are corrected by the offset the broker reports.** `/stats` returns
 * `at`, the broker's clock at the moment it answered. The difference between that and the
 * local clock is recorded here and applied when rendering "3 minutes ago", so a skewed
 * workstation does not put a future timestamp on an event that already happened. It is a
 * cosmetic correction and it is deliberately not used for countdowns, which need no clock
 * at all.
 */

/**
 * How far this workstation's clock is ahead of the broker's, in milliseconds. Zero until a
 * response has been seen, which is exactly what an uncorrected clock would give -- so the
 * correction is never worse than not having one.
 */
let skewMs = 0;

/** Record the broker's clock. Called from src/lib/api.ts for any payload carrying `at`. */
export function recordServerTime(iso: string): void {
  const server = Date.parse(iso);
  if (Number.isNaN(server)) return;
  skewMs = Date.now() - server;
}

/** Local time expressed on the broker's clock, as a millisecond epoch. */
export function serverNow(): number {
  return Date.now() - skewMs;
}

/**
 * A countdown that ticks once a second, anchored on a broker-computed number of seconds
 * and the moment that number arrived.
 *
 * Returns null when there is nothing to count -- a lease with no expiry, or one that has
 * already finished. Goes negative when the deadline has passed and the broker has not
 * swept it yet; callers must render that honestly rather than clamping it to zero, for
 * the same reason `PendingApprovalResponse.seconds_until_deadline` is allowed to be
 * negative.
 */
export function useCountdown(
  seconds: number | null | undefined,
  anchorMs: number,
): number | null {
  const live = seconds !== null && seconds !== undefined;
  // Anchored rather than `Date.now()` so the first render is a pure function of its props.
  const [nowMs, setNowMs] = useState(anchorMs);

  useEffect(() => {
    if (!live) return;
    setNowMs(Date.now());
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [live, anchorMs]);

  if (!live) return null;
  const elapsed = Math.max(0, (nowMs - anchorMs) / 1000);
  return Math.round(seconds - elapsed);
}

/**
 * A re-render every `intervalMs`, for relative timestamps that should age on screen.
 *
 * Deliberately coarse. "4 minutes ago" does not need to be recomputed every second, and a
 * page with sixty of them that does is a page that heats a laptop for nothing.
 */
export function useSlowTick(intervalMs = 30_000): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((value) => value + 1), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return tick;
}
