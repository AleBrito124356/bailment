"use client";

import { useSyncExternalStore } from "react";

/**
 * Where the operator's token lives, and why it lives there.
 *
 * The dashboard has no credential of its own. There is no `BAILMENT_API_TOKEN` in the
 * Next process, no server-side proxy that attaches one, and no cookie. The operator pastes
 * their token, the browser sends it as a bearer header, and the broker decides what that
 * token may see. Everything else in this file is consequence.
 *
 * **sessionStorage, not localStorage.** A token in localStorage outlives the tab, the
 * browser restart and usually the person's memory of having pasted it. sessionStorage is
 * scoped to the tab: close it and the operator tier is gone. That is the right default for
 * a credential that can approve a production database branch, and re-pasting once per
 * session is a cost worth paying.
 *
 * **Never in the URL, never in a query string, never logged.** The token is read here and
 * written into exactly one place: the `Authorization` header in src/lib/api.ts.
 *
 * **Absence is a legitimate state.** A broker with `BAILMENT_ALLOW_ANONYMOUS` set serves
 * the demo without a token, at agent tier. The dashboard therefore does not gate its UI
 * behind a token prompt -- it tries the request, and if the broker says 401 it explains
 * what to do. Guessing that a missing token means "you cannot use this" would break the
 * one configuration where somebody is evaluating the project for the first time.
 */

const STORAGE_KEY = "bailment.token";

let cached: string | null | undefined;
const listeners = new Set<() => void>();

function read(): string | null {
  if (typeof window === "undefined") return null;
  if (cached !== undefined) return cached;
  try {
    cached = window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    // Private modes and locked-down enterprise profiles can throw on storage access.
    // A dashboard that crashes because it could not cache a token is worse than one that
    // asks for it again.
    cached = null;
  }
  return cached;
}

/** The token to send with the next request, or null for an anonymous call. */
export function getToken(): string | null {
  return read();
}

/** Store (or clear) the token and notify every subscriber. */
export function setToken(token: string | null): void {
  const value = token?.trim() ? token.trim() : null;
  cached = value;
  if (typeof window !== "undefined") {
    try {
      if (value === null) window.sessionStorage.removeItem(STORAGE_KEY);
      else window.sessionStorage.setItem(STORAGE_KEY, value);
    } catch {
      // Keep the in-memory value; it works for this page view, which is enough.
    }
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * The current token, re-rendering the component when it changes.
 *
 * The server snapshot is always null so the first client render matches the server's:
 * anything else produces a hydration mismatch on a value nobody should be rendering
 * anyway.
 */
export function useToken(): string | null {
  return useSyncExternalStore(
    subscribe,
    () => read(),
    () => null,
  );
}

/**
 * A safe thing to show in the UI so an operator can tell *which* token is loaded without
 * the token itself being readable over a shoulder or in a screen share.
 */
export function tokenFingerprint(token: string | null): string | null {
  if (!token) return null;
  if (token.length <= 8) return `${token.slice(0, 2)}${"•".repeat(6)}`;
  return `${token.slice(0, 4)}${"•".repeat(6)}${token.slice(-2)}`;
}
