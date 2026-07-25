"use client";

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ConnectionProvider } from "@/components/connection";
import { TooltipProvider } from "@/components/ui/tooltip";

/**
 * One query client for the browser session.
 *
 * `staleTime` is zero and the intervals live on the individual hooks, because the right
 * refresh rate for the catalog (files on disk) and for the approval queue (a person is
 * waiting) differ by two orders of magnitude, and a single global number would have to be
 * wrong for one of them.
 *
 * Created inside a state initialiser rather than at module scope: a module-level client is
 * shared across requests in a server runtime, which is how one user's cached leases end up
 * rendered for another.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Polling is per-hook. Background tabs stop polling, which is the default and
            // is deliberately not overridden: a dashboard on a spare monitor should not
            // keep a broker's access log busy all night.
            refetchOnWindowFocus: true,
            refetchIntervalInBackground: false,
            gcTime: 5 * 60 * 1000,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={client}>
      <TooltipProvider delayDuration={200} skipDelayDuration={300}>
        <ConnectionProvider>{children}</ConnectionProvider>
      </TooltipProvider>
    </QueryClientProvider>
  );
}
