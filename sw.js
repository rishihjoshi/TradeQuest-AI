const CACHE = 'tradequest-v12';
const SHELL = [
  './index.html',
  './style.css',
  './app.js',
  './manifest.json',
  './icons/icon.svg',
  './icons/icon-maskable.svg',
  // CDN dependency — pre-cached so app renders offline
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js',
];

self.addEventListener('install', e => {
  // skipWaiting inside the chain so the SW only activates after cache is committed
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // CDN assets (Chart.js): network-first so updates are picked up,
  // cache fallback so app renders offline
  if (url.hostname !== self.location.hostname) {
    e.respondWith(
      fetch(e.request)
        .then(r => {
          const clone = r.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
          return r;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // Data files (portfolio.json, symbols.json, bars/*.json): network-first,
  // serve stale cache when offline so the PWA still shows last-known data
  if (url.pathname.includes('/data/')) {
    e.respondWith(
      fetch(e.request).then(r => {
        const clone = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return r;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // App shell (HTML, CSS, JS, manifest, icons): cache-first
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
