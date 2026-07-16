// Service worker minimal: cache shell agar app tetap terbuka saat jaringan lambat.
// Semua request API/WS tetap langsung ke network.
const CACHE = 'jarvis-shell-v2';
const SHELL = ['/', '/manifest.json', '/icon.svg'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ---- Web Push: tampilkan notifikasi + tombol Izinkan/Tolak ----
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) {}
  event.waitUntil(
    self.registration.showNotification(data.title || 'Jarvis', {
      body: data.body || '',
      icon: '/icon.svg',
      badge: '/icon.svg',
      tag: data.tag || undefined,
      renotify: true,
      vibrate: [120, 60, 120],
      requireInteraction: data.kind === 'chat_permission',
      actions: (data.actions || []).slice(0, 2),
      data,
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  const data = event.notification.data || {};
  event.notification.close();

  // Tombol Izinkan/Tolak pada notifikasi permission
  if ((event.action === 'allow' || event.action === 'deny') && data.kind === 'chat_permission') {
    event.waitUntil(
      fetch('/api/push/permission', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chat_id: data.chat_id,
          request_id: data.request_id,
          decision: event.action,
          nonce: data.nonce,
        }),
      }).catch(() => {})
    );
    return;
  }

  // Tap biasa: fokuskan/buka app
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ('focus' in client) return client.focus();
      }
      return clients.openWindow('/');
    })
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api') || url.pathname.startsWith('/ws')) {
    return; // network only
  }
  // network-first, fallback ke cache (biar update frontend langsung terlihat)
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
