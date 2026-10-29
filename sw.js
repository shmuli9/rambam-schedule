/* Service worker for the Rambam Yad schedule PWA.
 *
 * The schedule pages are self-contained, so the app is just a handful of
 * files.  Strategy: precache them on install; pages (navigations) are
 * network-first, bypassing the browser's HTTP cache, so a redeploy is picked up
 * immediately and the cached copy is only the offline fallback; everything else
 * is stale-while-revalidate.  Bump CACHE to purge old files.  Paths are relative so it works under a GitHub Pages sub-path.
 */
const CACHE = 'rambam-yad-v4';
/* The Hebrew text (texts/*.json, ~12 MB, 3 MB gzipped), the English translation (texts_en/*.json,
 * ~17 MB) and Steinsaltz's glosses fetched from Sefaria live in their own cache:
 * each file is stored on first use (nothing is fetched ahead), read cache-first, and bumping CACHE
 * never throws it away. */
const TEXT_CACHE = 'rambam-texts-v1';
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
      .then(keys => Promise.all(keys.filter(k => k !== CACHE && k !== TEXT_CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  /* Steinsaltz's word notes come from Sefaria's API: cache-first, so re-reading works offline */
  const steinsaltz = req.method === 'GET' && url.origin === 'https://www.sefaria.org' &&
    url.pathname.startsWith('/api/texts/Steinsaltz');
  if (req.method !== 'GET' || (url.origin !== self.location.origin && !steinsaltz)) return;
  if (steinsaltz || /\/texts(_en)?\/[^/]+\.json$/.test(url.pathname)) {
    e.respondWith(
      caches.open(TEXT_CACHE).then(cache =>
        cache.match(req, { ignoreSearch: true }).then(hit => hit || fetch(req).then(res => {
          if (res && res.ok) cache.put(req, res.clone());
          return res;
        })))
    );
    return;
  }
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req, { cache: 'reload' })
        .then(res => {
          if (res && res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
          return res;
        })
        .catch(() => caches.match(req, { ignoreSearch: true })
          .then(hit => hit || caches.match('./index.html')))
    );
    return;
  }
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
