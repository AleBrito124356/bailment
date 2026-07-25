"use client";

import type * as React from "react";
import { KeyRound, PlugZap, ShieldAlert, TriangleAlert } from "lucide-react";

import { useConnection } from "@/components/connection";
import { Button } from "@/components/ui/button";
import { ApiError, UnreachableError } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Failure, told apart properly.
 *
 * Four different things go wrong when a dashboard talks to this broker, and the fix for
 * each is different:
 *
 *   - the broker is not running, or CORS refused the response  -> check the deployment
 *   - no usable token                                          -> paste one
 *   - a token that is not operator tier                        -> paste a different one
 *   - the broker refused for a reason of its own               -> read what it said
 *
 * Collapsing those into "something went wrong" is how somebody spends twenty minutes on a
 * missing environment variable. The broker writes its refusals for whoever was blocked, so
 * its message is shown verbatim rather than paraphrased.
 */
export function ErrorState({
  error,
  className,
  onRetry,
  what = "this",
}: {
  error: unknown;
  className?: string;
  onRetry?: () => void;
  /** What could not be loaded, for the headline: "leases", "the approval queue". */
  what?: string;
}) {
  const { open } = useConnection();

  let Icon = TriangleAlert;
  let title = `Could not load ${what}`;
  let message = error instanceof Error ? error.message : String(error);
  let problems: string[] = [];
  let action: React.ReactNode = onRetry ? (
    <Button variant="secondary" size="sm" onClick={onRetry}>
      Try again
    </Button>
  ) : null;

  if (error instanceof UnreachableError) {
    Icon = PlugZap;
    title = "The broker is not answering";
    message = error.message;
  } else if (error instanceof ApiError && error.needsToken) {
    Icon = KeyRound;
    title = "This broker wants a token";
    action = (
      <Button variant="primary" size="sm" onClick={open}>
        Add a token
      </Button>
    );
  } else if (error instanceof ApiError && error.needsOperator) {
    Icon = ShieldAlert;
    title = "Operator token required";
    action = (
      <Button variant="primary" size="sm" onClick={open}>
        Switch token
      </Button>
    );
  } else if (error instanceof ApiError) {
    problems = error.problems;
  }

  return (
    <div
      className={cn(
        "flex flex-col items-center gap-3 rounded-lg border border-border bg-card px-6 py-12 text-center",
        className,
      )}
      role="alert"
    >
      <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-muted/50">
        <Icon className="h-4 w-4 text-muted-foreground" aria-hidden />
      </div>
      <div className="space-y-1.5">
        <p className="text-sm font-medium text-foreground">{title}</p>
        <p className="mx-auto max-w-xl text-sm leading-relaxed text-muted-foreground">{message}</p>
        {problems.length > 0 ? (
          <ul className="mx-auto max-w-xl list-disc space-y-0.5 pl-5 text-left text-sm text-muted-foreground">
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        ) : null}
      </div>
      {action ? <div className="pt-1">{action}</div> : null}
    </div>
  );
}

