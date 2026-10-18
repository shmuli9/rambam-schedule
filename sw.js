/* Service worker for the Rambam Yad schedule PWA.
 *
 * The schedule pages are self-contained, so the app is just a handful of
 * files.  Strategy: precache them on install, then serve every same-origin GET
 * stale-while-revalidate -- instant and offline-capable, and a redeploy shows up
 * on the next visit after the one that fetched it.  Bump CACHE only to purge
 * old files.  Paths are relative so it works under a GitHub Pages sub-path.
 */
const CACHE = 'rambam-yad-v1';
const PRECACHE = [
  './',
  './index.html',
  './output/rambam_schedule.html',
  './output/rambam_schedule_b1000.html',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-maskable-512.png',
  './icons/apple-touch-icon.png',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) return;
  e.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(req, { ignoreSearch: true }).then(hit => {
        const fresh = fetch(req).then(res => {
          if (res && res.ok) cache.put(req, res.clone());
          return res;
        });
        if (hit) { fresh.catch(() => {}); return hit; }
        return fresh.catch(() => cache.match('./index.html'));
      })
    )
  );
});
