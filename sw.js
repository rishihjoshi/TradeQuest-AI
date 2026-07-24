// IMPORTANT: bump this version string on EVERY change to the app shell (index.html, app.js,
// style.css, manifest, icons). The shell is served cache-first, so old assets are only purged
// when this version changes. v14→v15: Cash card added (index.html #cashPct removed → #cashValue/
// #cashSub) — without this bump, clients served a fresh index.html but a stale cached app.js and
// crashed on $('cashPct').
const CACHE = 'tradequest-v15';
const SHELL = [
  './index.html',
  './style.css',
  './app.js',
  './manifest.json',
  './icons/icon.svg',
  './icons/icon-maskable.svg',
  './icons/App Icon.png',
  './icons/Hero Image.png',
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
