"use client";

import * as React from "react";
import { Boxes, Search } from "lucide-react";

import { CatalogCard } from "@/components/catalog-card";
import { EmptyState } from "@/components/empty-state";
import { ErrorState } from "@/components/error-state";
import { PageHeader } from "@/components/page-header";
import { Input } from "@/components/ui/field";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useCatalog } from "@/lib/queries";

/**
 * Everything this installation is willing to hand out.
 *
 * Each card opens a form generated from that path's JSON Schema -- see
 * src/lib/schema-form.ts for why it is generated rather than written. Operators also see
 * disabled paths, flagged; nobody else does, and that filtering happens at the broker.
 */
export default function CatalogPage() {
  const catalog = useCatalog();
  const [query, setQuery] = React.useState("");

  const items = catalog.data?.items ?? [];
  const needle = query.trim().toLowerCase();
  const filtered = needle
    ? items.filter((entry) =>
        [entry.id, entry.name, entry.provider, entry.description, ...entry.tags]
          .join(" ")
          .toLowerCase()
          .includes(needle),
      )
    : items;

  return (
    <>
      <PageHeader
        title="Catalog"
        description="Golden paths, each one a capability a platform team decided to offer. The form behind every card is generated from the same schema the agent's MCP tool is built from."
        actions={
          items.length > 4 ? (
            <div className="relative w-56">
              <Search
                className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
                aria-hidden
              />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Filter"
                aria-label="Filter golden paths"
                className="pl-8"
              />
            </div>
          ) : null
        }
      />

      {catalog.isError ? (
        <ErrorState
          error={catalog.error}
          what="the catalog"
          onRetry={() => void catalog.refetch()}
        />
      ) : catalog.isPending ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 3 }).map((_, index) => (
            <Skeleton key={index} className="h-64 w-full" />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <Card>
          <EmptyState
            icon={Boxes}
            title={needle ? "Nothing matches that" : "This broker offers nothing yet"}
            description={
              needle ? (
                <>
                  No golden path matches <span className="font-mono">{query}</span>.
                </>
              ) : (
                <>
                  A golden path is one YAML file describing something the team is willing to
                  hand out. Drop one in the broker&apos;s catalog directory and it appears here,
                  in the agent&apos;s tool list and in the OSB catalog at the same moment.
                </>
              )
            }
          />
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((entry) => (
            <CatalogCard key={entry.id} entry={entry} />
          ))}
        </div>
      )}

      {catalog.data?.directory ? (
        <p className="pt-6 text-xs text-muted-foreground">
          Loaded from <span className="font-mono">{catalog.data.directory}</span> ·{" "}
          {catalog.data.count} {catalog.data.count === 1 ? "path" : "paths"}
        </p>
      ) : null}
    </>
  );
}
