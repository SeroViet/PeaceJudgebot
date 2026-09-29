// Minimaler Service Worker: macht die App installierbar, cacht nur das Icon.
// Seiten werden nie gecacht, damit Tipps und Quoten immer aktuell sind.
const CACHE = "peacejudge-v1";
self.addEventListener("install", (e) => e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/static/icon.svg"]))));
self.addEventListener("fetch", (e) => {
  if (e.request.url.endsWith("/static/icon.svg")) {
    e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
  }
});
