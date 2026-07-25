import Link from "next/link";

import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

export default function NotFound() {
  return (
    <Card>
      <EmptyState
        title="No such page"
        description="Nothing is served at that address. The dashboard has five: overview, catalog, leases, approvals and reconcile."
        action={
          <Button asChild variant="secondary" size="sm">
            <Link href="/">Back to the overview</Link>
          </Button>
        }
      />
    </Card>
  );
}
