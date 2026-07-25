"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Boxes,
  ClipboardCheck,
  LayoutDashboard,
  Menu,
  RefreshCcw,
  ScrollText,
  X,
} from "lucide-react";

import { ConnectionStatus } from "@/components/connection";
import { ThemeToggle } from "@/components/theme-toggle";
import { useStats } from "@/lib/queries";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  Icon: typeof LayoutDashboard;
  /** Which stat, if any, this item should carry a count for. */
  badge?: "approvals" | "orphans";
}

const NAV: NavItem[] = [
  { href: "/", label: "Overview", Icon: LayoutDashboard },
  { href: "/catalog", label: "Catalog", Icon: Boxes },
  { href: "/leases", label: "Leases", Icon: ScrollText },
  { href: "/approvals", label: "Approvals", Icon: ClipboardCheck, badge: "approvals" },
  { href: "/reconcile", label: "Reconcile", Icon: RefreshCcw, badge: "orphans" },
];

/**
 * A fixed sidebar and a content column, and nothing else.
 *
 * No top bar. Every page states its own title in its header, and a second row of chrome
 * repeating it would take fifty vertical pixels from the tables that are the actual
 * product. The two things that belong to the whole application rather than to any page --
 * which broker this is talking to, and the theme -- sit at the bottom of the sidebar,
 * where they are findable and out of the way.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = React.useState(false);
  const pathname = usePathname();

  React.useEffect(() => {
    setMobileOpen(false);
  }, [pathname]);

  return (
    <div className="min-h-screen">
      <MobileBar open={mobileOpen} onToggle={() => setMobileOpen((value) => !value)} />

      <div className="lg:grid lg:grid-cols-[15rem_minmax(0,1fr)]">
        <aside
          className={cn(
            "z-40 flex-col border-b border-border bg-card px-3 py-4 lg:sticky lg:top-0 " +
              "lg:h-screen lg:border-b-0 lg:border-r",
            mobileOpen ? "flex" : "hidden lg:flex",
          )}
        >
          <div className="hidden lg:block">
            <Brand />
          </div>
          <nav className="flex-1 space-y-0.5 lg:mt-6">
            {NAV.map((item) => (
              <NavLink key={item.href} item={item} pathname={pathname} />
            ))}
          </nav>
          <div className="space-y-2 pt-4">
            <ConnectionStatus />
            <div className="flex items-center justify-between px-0.5">
              <span className="text-2xs text-muted-foreground">Theme</span>
              <ThemeToggle />
            </div>
          </div>
        </aside>

        <main className="min-w-0">
          <div className="mx-auto w-full max-w-[1180px] px-5 py-8 sm:px-8 lg:py-10">{children}</div>
        </main>
      </div>
    </div>
  );
}

function Brand() {
  return (
    <Link href="/" className="flex items-center gap-2.5 rounded-md px-1.5 py-1">
      <span
        aria-hidden
        className="flex h-6 w-6 items-center justify-center rounded-[7px] bg-accent text-[13px] font-bold leading-none text-accent-foreground"
      >
        b
      </span>
      <span className="flex flex-col leading-tight">
        <span className="text-[15px] font-semibold tracking-[-0.01em]">bailment</span>
        <span className="text-2xs text-muted-foreground">provisioning broker</span>
      </span>
    </Link>
  );
}

function NavLink({ item, pathname }: { item: NavItem; pathname: string }) {
  const stats = useStats();
  const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);

  const count =
    item.badge === "approvals"
      ? stats.data?.awaiting_approval
      : item.badge === "orphans"
        ? stats.data?.orphans_outstanding
        : undefined;

  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px] font-medium transition-colors",
        active
          ? "bg-accent-soft text-accent"
          : "text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
    >
      <item.Icon className="h-4 w-4 shrink-0" aria-hidden />
      <span className="flex-1">{item.label}</span>
      {count ? (
        <span
          className={cn(
            "tabular rounded-full px-1.5 py-px text-2xs font-semibold ring-1 ring-inset",
            // Orphans are amber wherever they appear, including here, and never red.
            item.badge === "orphans"
              ? "bg-warn-soft text-warn ring-warn/25"
              : "bg-accent text-accent-foreground ring-transparent",
          )}
        >
          {count}
        </span>
      ) : null}
    </Link>
  );
}

function MobileBar({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <div className="sticky top-0 z-50 flex items-center justify-between border-b border-border bg-card px-4 py-2.5 lg:hidden">
      <Brand />
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-label={open ? "Close navigation" : "Open navigation"}
        className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        {open ? <X className="h-4 w-4" /> : <Menu className="h-4 w-4" />}
      </button>
    </div>
  );
}
