import type { LeaseState } from "@/lib/types";

/**
 * How a lease state looks and what it means, mirrored from `bailment.states`.
 *
 * **Colour is a claim, so it is rationed.** Green means one thing here: a resource exists
 * and you may use it right now. `released` is the successful end of a lease and it is
 * still grey, because a table of fifty finished leases rendered in green drowns the two
 * that are live -- and "which of these can I use" is the question the table is for.
 *
 * **Orphaned is amber, never red.** An orphan is the finding this project exists to
 * produce. Rendering it as an alarm teaches operators that the reconciler is a thing that
 * goes wrong, when it is the thing that is working; and an interface that shouts is an
 * interface people learn to dismiss. Amber says "this needs a decision", which is exactly
 * true. `failed` is the only red, and only because it is terminal and nothing further will
 * happen without a person.
 *
 * The blurbs are the state machine's own docstrings, condensed. They appear on hover, so
 * somebody meeting `deprovisioning` for the first time does not have to go and read
 * states.py.
 */

export type Tone = "neutral" | "info" | "ok" | "warn" | "danger";

interface StateMeta {
  label: string;
  tone: Tone;
  blurb: string;
}

const META: Record<LeaseState, StateMeta> = {
  pending: {
    label: "Pending",
    tone: "neutral",
    blurb: "Request accepted and persisted. No policy decision made yet.",
  },
  awaiting_approval: {
    label: "Awaiting approval",
    tone: "info",
    blurb: "Policy said a human must decide. Nothing has been provisioned.",
  },
  rejected: {
    label: "Rejected",
    tone: "neutral",
    blurb: "Policy denied it, or an approver declined. Terminal, and nothing was created.",
  },
  provisioning: {
    label: "Provisioning",
    tone: "info",
    blurb: "A worker holds the claim and is calling the provider.",
  },
  active: {
    label: "Active",
    tone: "ok",
    blurb: "The provider confirmed the resource exists. The lease clock is running.",
  },
  expiring: {
    label: "Expiring",
    tone: "warn",
    blurb: "Inside the warning window. Still fully usable; a notice has been emitted.",
  },
  expired: {
    label: "Expired",
    tone: "neutral",
    blurb: "The TTL elapsed. An intention to destroy — the resource may still exist.",
  },
  revoked: {
    label: "Revoked",
    tone: "neutral",
    blurb: "Ended early by a human or by policy. Also an intention, not a fact.",
  },
  deprovisioning: {
    label: "Deprovisioning",
    tone: "info",
    blurb: "A worker holds the claim and is calling the provider's delete path.",
  },
  released: {
    label: "Released",
    tone: "neutral",
    blurb: "The provider confirmed the resource is gone. The only clean terminal state.",
  },
  failed: {
    label: "Failed",
    tone: "danger",
    blurb: "Provisioning failed and any partial resource was rolled back. Terminal.",
  },
  orphaned: {
    label: "Orphaned",
    tone: "warn",
    blurb:
      "A real resource is believed to exist that no live lease accounts for. This is the " +
      "state the whole project exists to make visible.",
  },
  unknown: {
    label: "Unknown",
    tone: "warn",
    blurb: "Provider state could not be determined. Never assumed released; always re-checked.",
  },
};

const FALLBACK: StateMeta = {
  label: "Unrecognised",
  tone: "neutral",
  blurb: "This broker reported a lease state this dashboard does not know about.",
};

export function stateMeta(state: string): StateMeta {
  return META[state as LeaseState] ?? { ...FALLBACK, label: state };
}

export function stateLabel(state: string): string {
  return stateMeta(state).label;
}

export function stateTone(state: string): Tone {
  return stateMeta(state).tone;
}

/**
 * The order the filter control offers states in: lifecycle order, matching the enum in
 * states.py, because reading it top to bottom should tell you the story.
 */
export const STATE_FILTER_GROUPS: Array<{ label: string; states: LeaseState[] }> = [
  { label: "Intake", states: ["pending", "awaiting_approval", "rejected"] },
  { label: "Live", states: ["provisioning", "active", "expiring"] },
  { label: "Teardown", states: ["expired", "revoked", "deprovisioning", "released"] },
  { label: "Trouble", states: ["failed", "orphaned", "unknown"] },
];

/** Tailwind classes for a badge in each tone. One definition, used everywhere. */
export const TONE_BADGE: Record<Tone, string> = {
  neutral: "bg-muted text-muted-foreground ring-border",
  info: "bg-info-soft text-info ring-info/20",
  ok: "bg-ok-soft text-ok ring-ok/20",
  warn: "bg-warn-soft text-warn ring-warn/25",
  danger: "bg-danger-soft text-danger ring-danger/20",
};

/** The dot that precedes a badge label. Solid, so it survives being seen out of focus. */
export const TONE_DOT: Record<Tone, string> = {
  neutral: "bg-muted-foreground/60",
  info: "bg-info",
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-danger",
};
