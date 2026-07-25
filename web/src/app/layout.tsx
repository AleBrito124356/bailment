import type { ReactNode } from "react";
import type { Metadata } from "next";

import { AppShell } from "@/components/app-shell";
import { Providers } from "@/app/providers";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "bailment",
    template: "%s · bailment",
  },
  description:
    "Capabilities instead of credentials: leases that expire, and a reconciler that finds " +
    "what outlived them.",
  robots: { index: false, follow: false },
};

/**
 * The theme is applied before first paint by the inline script below.
 *
 * Next.js streams the document, so a theme decided in an effect arrives one frame after
 * the page has already been painted in the wrong one. That flash is unpleasant everywhere
 * and unacceptable on a tool people open at night next to a dark terminal. The script is
 * eight lines, runs synchronously in `<head>`, and reads the same key the toggle writes.
 *
 * Deliberately not a font from a CDN. `next/font/google` downloads at build time, and this
 * repository has to build on a machine with no network; the stack in tailwind.config.ts
 * prefers a locally installed Inter and falls back to the platform UI font, which on every
 * platform this dashboard runs on is a clean neo-grotesque already.
 */
const THEME_SCRIPT = `
try {
  var stored = localStorage.getItem('bailment.theme');
  var dark = stored === 'dark' || (stored === null &&
    window.matchMedia('(prefers-color-scheme: dark)').matches);
  if (dark) document.documentElement.classList.add('dark');
} catch (e) {}
`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
