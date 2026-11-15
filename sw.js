/* Fire Connect service worker: lets the home-screen app open even with no signal.
   Pages and styles are fetched fresh when online (network first) and fall back to the last copy when offline.
   Live data (/api, /v1) is never cached: messages and alerts must always be current. */
const CACHE = 'fire-connect-v1';
const SHELL = ['/connect.html', '/theme.css', '/theme.js', '/manifest.webmanifest', '/icons/icon-192.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/v1/')) return;
  e.respondWith(
    fetch(e.request)
      .then(res => {
        if (res.ok && SHELL.includes(url.pathname)) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match('/connect.html')))
  );
});
