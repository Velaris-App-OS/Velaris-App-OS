/**
 * Portal v2 P2 — customer-portal service worker.
 *
 * Scope discipline: this worker ONLY touches portal traffic —
 * navigations to /portal/* and GETs under /api/v1/portal/*. Everything else
 * (Studio app, engine APIs, HMR) passes straight through untouched.
 *
 * Strategies:
 *  - portal navigations + portal GET APIs: network-first, fall back to the
 *    last cached copy (read-only offline view of config + case list).
 *  - queued submissions: the page owns the queue (IndexedDB) and flushes on
 *    load/online; the 'sync' event re-flushes from here when the browser
 *    grants Background Sync, so a closed tab still syncs.
 */
// v2: never caches authenticated /account/ responses (see isPortalGet).
// Bumping the name purges any personal data the v1 worker may have cached.
const CACHE = "velaris-portal-v2";
const DB_NAME = "velaris-portal";
const STORE = "pending_submissions";

self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil((async () => {
  for (const name of await caches.keys()) {
    if (name.startsWith("velaris-portal-") && name !== CACHE) await caches.delete(name);
  }
  await self.clients.claim();
})()));

function isPortalNav(req, url) {
  return req.mode === "navigate" && url.pathname.startsWith("/portal/");
}
// SECURITY: only PUBLIC portal GETs are cacheable. Anything under `/account/`
// carries a specific customer's personal data and is authorized by the
// Authorization header — but the Cache API keys by URL and ignores headers, so
// caching it would let an offline account-switch on a shared device serve one
// customer's cached case list to another. Never cache authenticated responses.
function isPortalGet(req, url) {
  return req.method === "GET"
    && url.pathname.startsWith("/api/v1/portal/")
    && !url.pathname.includes("/account/");
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;
  if (!isPortalNav(e.request, url) && !isPortalGet(e.request, url)) return;

  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const fresh = await fetch(e.request);
      if (fresh.ok) cache.put(e.request, fresh.clone());
      return fresh;
    } catch {
      const cached = await cache.match(e.request);
      if (cached) return cached;
      if (e.request.mode === "navigate") {
        const shell = await cache.match("/portal-offline-shell");
        if (shell) return shell;
      }
      return new Response(JSON.stringify({ detail: "offline" }), {
        status: 503, headers: { "Content-Type": "application/json" },
      });
    }
  })());
});

// ── Background flush of queued submissions ─────────────────────────
function openDb() {
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

function idbAll(db) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly").objectStore(STORE).getAll();
    tx.onsuccess = () => resolve(tx.result || []);
    tx.onerror = () => reject(tx.error);
  });
}

function idbDelete(db, key) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite").objectStore(STORE).delete(key);
    tx.onsuccess = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function flushQueue() {
  const db = await openDb();
  const rows = await idbAll(db);
  for (const row of rows) {
    try {
      const res = await fetch(`/api/v1/portal/${row.slug}/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...row.payload, client_ref: row.client_ref }),
      });
      // Success or permanent rejection (4xx): stop retrying either way —
      // the client_ref makes a later manual retry safe.
      if (res.ok || (res.status >= 400 && res.status < 500)) {
        await idbDelete(db, row.client_ref);
        const clients = await self.clients.matchAll();
        clients.forEach((c) => c.postMessage({ type: "portal-flushed", client_ref: row.client_ref, ok: res.ok }));
      }
    } catch { /* still offline — the next sync/online event retries */ }
  }
}

self.addEventListener("sync", (e) => {
  if (e.tag === "portal-flush") e.waitUntil(flushQueue());
});
self.addEventListener("message", (e) => {
  if (e.data?.type === "portal-flush") flushQueue();
});
