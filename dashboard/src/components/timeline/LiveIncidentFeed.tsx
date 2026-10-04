"use client";

import { useEffect, useRef, useState } from "react";

import type { IncidentPage } from "@/lib/types";

import { IncidentRow } from "./IncidentRow";

const POLL_MS = 10_000;

/**
 * The incident list, kept live by polling the dashboard's own Route Handler (never the engine
 * directly). Server-rendered first, so the page is complete before any JavaScript runs.
 *
 * A failed poll keeps the incidents it already has and says so, rather than blanking the list:
 * during an incident, slightly old data is far more useful than none.
 */
export function LiveIncidentFeed({ initial }: { initial: IncidentPage }) {
  const [page, setPage] = useState(initial);
  const [stale, setStale] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const known = useRef(new Set(initial.items.map((i) => i.id)));

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const response = await fetch("/api/incidents?limit=25", { cache: "no-store" });
        if (!response.ok) throw new Error(String(response.status));
        const next = (await response.json()) as IncidentPage;
        if (cancelled) return;
        const fresh = next.items.filter((i) => !known.current.has(i.id));
        fresh.forEach((i) => known.current.add(i.id));
        if (fresh.length > 0) {
          setAnnouncement(
            `${fresh.length} new incident${fresh.length > 1 ? "s" : ""}: ` +
              fresh.map((i) => `${i.cause_alertname} on ${i.target_key}`).join(", "),
          );
        }
        setPage(next);
        setStale(false);
      } catch {
        if (!cancelled) setStale(true);
      } finally {
        if (!cancelled) setNow(Date.now());
      }
    }
    const timer = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <div className="flex flex-col gap-3">
      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>
      {stale && (
        <p role="status" className="text-xs text-tone-warning">
          Could not refresh — showing the last incidents received.
        </p>
      )}
      {page.items.map((incident) => (
        <IncidentRow key={incident.id} incident={incident} now={now} />
      ))}
      {page.total > page.items.length && (
        <p className="text-center text-xs text-subtle">
          Showing the latest {page.items.length} of {page.total} incidents.
        </p>
      )}
    </div>
  );
}
