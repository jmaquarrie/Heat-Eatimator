/* Heat Planner service worker: app shell cached, last plan kept for offline viewing.
   Browsers only run service workers on HTTPS (or localhost), so this activates once the
   dashboard is served through a TLS proxy such as nginx proxy manager. */
const SHELL = "hp-shell-v2", DATA = "hp-data-v1";
const SHELL_FILES = ["/", "/static/app.css", "/static/app.js", "/static/icon.svg", "/static/icon-192.png", "/manifest.webmanifest"];
const OFFLINE_API = ["/api/plan", "/api/params", "/api/plans", "/api/config", "/api/log"];

self.addEventListener("install", (e) => { e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;
  if (OFFLINE_API.includes(u.pathname)) {
    // network first; on failure serve the saved copy, marked so the page can say it is offline
    e.respondWith(fetch(e.request).then((r) => { if (r.ok) { const cp = r.clone(); caches.open(DATA).then((c) => c.put(u.pathname, cp)); } return r; })
      .catch(() => caches.open(DATA).then((c) => c.match(u.pathname)).then((r) => {
        if (!r) return new Response(JSON.stringify({ error: "offline" }), { status: 503, headers: { "Content-Type": "application/json" } });
        const h = new Headers(r.headers); h.set("X-From-Cache", "1");
        return r.blob().then((b) => new Response(b, { status: 200, headers: h }));
      })));
    return;
  }
  if (u.pathname === "/" || u.pathname.startsWith("/static/") || u.pathname === "/manifest.webmanifest") {
    // network first for the shell too (so updates show straight away), cache as fallback
    e.respondWith(fetch(e.request).then((r) => { if (r.ok) { const cp = r.clone(); caches.open(SHELL).then((c) => c.put(e.request, cp)); } return r; })
      .catch(() => caches.match(e.request).then((r) => r || caches.match("/"))));
  }
});
