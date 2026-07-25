"use client";

import * as React from "react";
import { KeyRound, ShieldCheck, ShieldQuestion } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

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
import { FieldHint, Input, Label } from "@/components/ui/field";
import { API_ORIGIN } from "@/lib/api";
import { setToken, tokenFingerprint, useToken } from "@/lib/auth";
import { useStats } from "@/lib/queries";
import { cn } from "@/lib/utils";

/**
 * Where the operator tells the dashboard who they are.
 *
 * The dialog is reachable from the sidebar and from every error state that turns out to be
 * an authentication problem, because "paste a token" is the fix for a 401 and making
 * somebody hunt for the button is the difference between a broker that looks broken and
 * one that looks like it is waiting.
 *
 * The token is never rendered back. Once it is stored the field shows a fingerprint --
 * first four characters and last two -- which is enough to tell an agent token from an
 * operator token during a screen share without putting either on the screen.
 */

interface ConnectionApi {
  open: () => void;
}

const ConnectionContext = React.createContext<ConnectionApi | null>(null);

export function useConnection(): ConnectionApi {
  const value = React.useContext(ConnectionContext);
  // A no-op fallback rather than a throw: an error state rendered outside the provider
  // should still render its message, just without the button that fixes it.
  return value ?? { open: () => undefined };
}

export function ConnectionProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = React.useState(false);
  const api = React.useMemo<ConnectionApi>(() => ({ open: () => setOpen(true) }), []);

  return (
    <ConnectionContext.Provider value={api}>
      {children}
      <TokenDialog open={open} onOpenChange={setOpen} />
    </ConnectionContext.Provider>
  );
}

function TokenDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const current = useToken();
  const client = useQueryClient();
  const [draft, setDraft] = React.useState("");

  React.useEffect(() => {
    if (open) setDraft("");
  }, [open]);

  function apply(value: string | null) {
    setToken(value);
    // Every cached read was scoped to the old token. Drop all of it rather than let a page
    // show the previous caller's view until it happens to refetch.
    void client.invalidateQueries();
    onOpenChange(false);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent widthClass="max-w-lg">
        <DialogHeader>
          <DialogTitle>Broker connection</DialogTitle>
          <DialogDescription>
            This dashboard holds no credential of its own. The token you paste stays in this
            browser tab and is sent as a bearer header on each request; the broker decides what
            it may see.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          <div className="space-y-1.5">
            <Label>Broker</Label>
            <div className="rounded-md border border-border bg-muted/40 px-3 py-2 font-mono text-[13px]">
              {API_ORIGIN}
            </div>
            <FieldHint>
              Set at build time with <code className="font-mono">NEXT_PUBLIC_BAILMENT_API</code>.
            </FieldHint>
          </div>

          <form
            className="space-y-1.5"
            onSubmit={(event) => {
              event.preventDefault();
              if (draft.trim()) apply(draft);
            }}
          >
            <Label htmlFor="bailment-token">Token</Label>
            <Input
              id="bailment-token"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder={current ? (tokenFingerprint(current) ?? "") : "Paste a bearer token"}
            />
            <FieldHint>
              An entry from <code className="font-mono">BAILMENT_ADMIN_TOKENS</code> sees every
              lease and can approve. One from{" "}
              <code className="font-mono">BAILMENT_API_TOKENS</code> sees only its own. A broker
              running with <code className="font-mono">BAILMENT_ALLOW_ANONYMOUS</code> needs no
              token at all, at agent tier.
            </FieldHint>
          </form>

          <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
            Kept in <code className="font-mono">sessionStorage</code>, so it is gone when this tab
            closes. It is never written to a cookie, a URL or the Next.js server — nothing but
            this browser and the broker ever sees it.
          </p>
        </DialogBody>

        <DialogFooter>
          {current ? (
            <Button variant="ghost" onClick={() => apply(null)} className="mr-auto">
              Forget token
            </Button>
          ) : null}
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!draft.trim()} onClick={() => apply(draft)}>
            Use this token
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * The sidebar's connection row.
 *
 * It reports scope rather than tier, because scope is what changes what you are looking at:
 * "every lease" or "your own". An operator who has pasted the wrong token notices here,
 * before they wonder why the queue is empty.
 */
export function ConnectionStatus({ className }: { className?: string }) {
  const token = useToken();
  const { open } = useConnection();
  const stats = useStats();

  const scope = stats.data?.scope;
  const label = scope === "all" ? "Operator" : scope === "own" ? "Agent tier" : "Not connected";
  const Icon = scope === "all" ? ShieldCheck : scope === "own" ? ShieldQuestion : KeyRound;

  return (
    <button
      type="button"
      onClick={open}
      className={cn(
        "group flex w-full items-center gap-2.5 rounded-md border border-border bg-card px-2.5 " +
          "py-2 text-left transition-colors hover:bg-muted/60",
        className,
      )}
    >
      <Icon
        className={cn(
          "h-4 w-4 shrink-0",
          scope === "all" ? "text-accent" : "text-muted-foreground",
        )}
        aria-hidden
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-medium text-foreground">{label}</span>
        <span className="block truncate font-mono text-2xs text-muted-foreground">
          {token ? tokenFingerprint(token) : "anonymous"}
        </span>
      </span>
    </button>
  );
}
