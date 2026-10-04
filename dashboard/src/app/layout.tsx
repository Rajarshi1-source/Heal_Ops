import "./globals.css";

import type { Metadata } from "next";
import Link from "next/link";

import { READ_ONLY } from "@/lib/config";

export const metadata: Metadata = {
  title: { default: "HealOps", template: "%s · HealOps" },
  description:
    "Self-healing Kubernetes, audited: every plan, policy decision, verification and revert, with the evidence.",
};

const NAV = [
  { href: "/", label: "Incidents" },
  { href: "/approvals", label: "Approvals" },
  { href: "/runbooks", label: "Runbooks" },
] as const;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-dvh">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2"
        >
          Skip to content
        </a>
        <header className="border-b border-border bg-surface/60 backdrop-blur">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 sm:px-6">
            <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
              <span aria-hidden className="size-2.5 rounded-full bg-tone-success shadow-[0_0_12px] shadow-tone-success/60" />
              HealOps
            </Link>
            <nav aria-label="Main" className="flex gap-1 text-sm">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="rounded-md px-3 py-1.5 text-muted hover:bg-surface-raised hover:text-foreground"
                >
                  {item.label}
                </Link>
              ))}
            </nav>
            <p className="ml-auto text-xs text-subtle">
              correlate → plan → policy → act → <span className="text-muted">verify</span> → compensate
            </p>
          </div>
          {READ_ONLY && (
            <p className="border-t border-border bg-tone-neutral/8 px-4 py-1.5 text-center text-xs text-tone-neutral">
              Read-only view of recorded incidents. Nothing on this page can change a cluster.
            </p>
          )}
        </header>
        <main id="main" className="mx-auto max-w-6xl px-4 py-8 sm:px-6">
          {children}
        </main>
      </body>
    </html>
  );
}
