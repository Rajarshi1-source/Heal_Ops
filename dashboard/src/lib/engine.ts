import "server-only";

/**
 * Server-side access to the engine's read API. Nothing in the browser talks to the engine directly,
 * and nothing in the dashboard talks to PostgreSQL: one query layer, one set of types, and no
 * cluster credentials anywhere in the front end -- which is what lets the public build point at a
 * read-only seeded engine.
 *
 * Failure is a value, not an exception. A dashboard that errors during an incident is worse than one
 * showing 30-second-old data, so callers get `{ data, error, fetchedAt }` and decide how to degrade.
 */

export const ENGINE_URL = process.env.ENGINE_URL ?? "http://localhost:8000";

export interface Fetched<T> {
  data: T | null;
  error: string | null;
  /** When the engine produced this response. Older than the request when served from cache. */
  fetchedAt: string | null;
  status: number | null;
}

export async function engine<T>(path: string, { revalidate = 10 } = {}): Promise<Fetched<T>> {
  try {
    const response = await fetch(`${ENGINE_URL}${path}`, {
      headers: { accept: "application/json" },
      // Stale-while-revalidate through the data cache: if the engine is down at revalidation,
      // the last good response keeps being served, and `fetchedAt` says how old it is.
      next: { revalidate },
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) {
      return { data: null, error: `engine returned ${response.status}`, fetchedAt: null, status: response.status };
    }
    const date = response.headers.get("date");
    return {
      data: (await response.json()) as T,
      error: null,
      fetchedAt: date ? new Date(date).toISOString() : new Date().toISOString(),
      status: response.status,
    };
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    return { data: null, error: `engine unreachable (${reason})`, fetchedAt: null, status: null };
  }
}
