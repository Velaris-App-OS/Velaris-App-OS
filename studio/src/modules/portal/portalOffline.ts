/**
 * Portal v2 P2 — offline submission queue + PWA registration.
 *
 * Submissions made while offline land in IndexedDB with a client-generated
 * UUID (client_ref). The queue flushes automatically: on page load, on the
 * `online` event, and — where the browser supports Background Sync — from
 * the service worker even after the tab closes. The server dedupes on
 * client_ref, so any number of replays resolve to one case.
 *
 * Honesty rules: drafts are device-local only, and OTP login cannot happen
 * offline — the queue serves anonymous and already-logged-in submits.
 */

const DB_NAME = "velaris-portal";
const STORE = "pending_submissions";

export type PendingSubmission = {
  client_ref: string;
  slug: string;
  payload: {
    case_type_id: string; submitter_name: string; submitter_email: string;
    subject: string; description: string; priority: string;
  };
  saved_at: string;
};

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) {
        req.result.createObjectStore(STORE, { keyPath: "client_ref" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function tx<T>(mode: IDBTransactionMode, fn: (store: IDBObjectStore) => IDBRequest): Promise<T> {
  const db = await openDb();
  return new Promise<T>((resolve, reject) => {
    const r = fn(db.transaction(STORE, mode).objectStore(STORE));
    r.onsuccess = () => resolve(r.result as T);
    r.onerror = () => reject(r.error);
  });
}

export const savePending = (row: PendingSubmission) => tx<void>("readwrite", s => s.put(row));
export const deletePending = (ref: string) => tx<void>("readwrite", s => s.delete(ref));
export async function listPending(slug: string): Promise<PendingSubmission[]> {
  const all = await tx<PendingSubmission[]>("readonly", s => s.getAll());
  return (all || []).filter(r => r.slug === slug);
}

// ── Change notification (pending count badges) ─────────────────────
const listeners = new Set<() => void>();
export function onQueueChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
const notify = () => listeners.forEach(fn => fn());

/** Flush every queued submission for this slug. Safe to call repeatedly. */
export async function flushPending(slug: string): Promise<number> {
  const rows = await listPending(slug);
  let flushed = 0;
  for (const row of rows) {
    try {
      const res = await fetch(`/api/v1/portal/${row.slug}/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...row.payload, client_ref: row.client_ref }),
      });
      // Success or permanent rejection: either way, stop retrying this row.
      if (res.ok || (res.status >= 400 && res.status < 500)) {
        await deletePending(row.client_ref);
        flushed++;
      }
    } catch { /* still offline — a later flush retries */ }
  }
  if (flushed) notify();
  return flushed;
}

export async function queueSubmission(slug: string, payload: PendingSubmission["payload"]): Promise<string> {
  const client_ref = crypto.randomUUID();
  await savePending({ client_ref, slug, payload, saved_at: new Date().toISOString() });
  notify();
  // Ask the SW for Background Sync so a closed tab still submits.
  try {
    const reg: any = await navigator.serviceWorker?.ready;
    await reg?.sync?.register("portal-flush");
  } catch { /* no Background Sync — load/online flush covers it */ }
  return client_ref;
}

// ── PWA registration (portal pages only) ───────────────────────────
let _registered = false;
export function registerPortalPwa(slug: string): void {
  if (_registered || typeof window === "undefined") return;
  _registered = true;

  // Manifest is injected at runtime so only portal routes become installable.
  if (!document.querySelector('link[rel="manifest"]')) {
    const link = document.createElement("link");
    link.rel = "manifest";
    link.href = "/portal-manifest.webmanifest";
    document.head.appendChild(link);
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/portal-sw.js").catch(() => {});
    navigator.serviceWorker.addEventListener("message", (e: MessageEvent) => {
      if (e.data?.type === "portal-flushed") notify();
    });
  }

  window.addEventListener("online", () => { flushPending(slug); });
  // Flush anything left over from a previous offline visit.
  flushPending(slug);
}
