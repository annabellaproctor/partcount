// LAB::INV Service Worker
// Enables offline functionality and PWA capabilities

const CACHE_VERSION = 'v1';
const CACHE_NAME = `labinv-${CACHE_VERSION}`;

const STATIC_ASSETS = [
  '/',
  '/static/logo.svg',
  '/static/manifest.json',
  '/static/css/',
  '/offline.html',
];

// Install event - cache essential assets
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      console.log('[SW] Caching static assets');
      return cache.addAll(STATIC_ASSETS).catch(() => {
        console.log('[SW] Some assets could not be cached (offline during install is ok)');
      });
    })
  );
  self.skipWaiting();
});

// Activate event - clean up old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          if (cacheName !== CACHE_NAME) {
            console.log('[SW] Deleting old cache:', cacheName);
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
  self.clients.claim();
});

// Fetch event - network first, fallback to cache
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip non-GET requests
  if (request.method !== 'GET') {
    return;
  }

  // For API calls: network first, timeout after 5s, fallback to cache
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      Promise.race([
        fetch(request).then(response => {
          // Cache successful responses
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(request, clone));
          }
          return response;
        }),
        new Promise(resolve => {
          setTimeout(() => {
            caches.match(request).then(response => {
              resolve(response || new Response('Offline', { status: 503 }));
            });
          }, 5000);
        }),
      ])
    );
    return;
  }

  // For HTML pages: network first
  if (request.headers.get('accept')?.includes('text/html')) {
    event.respondWith(
      fetch(request)
        .then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(request, clone));
          }
          return response;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // For static assets: cache first, fallback to network
  event.respondWith(
    caches.match(request)
      .then(response => response || fetch(request))
      .catch(() => new Response('Resource offline', { status: 503 }))
  );
});

// Background sync for critical operations (when online again)
self.addEventListener('sync', event => {
  if (event.tag === 'sync-inventory') {
    event.waitUntil(
      // Retry pending inventory updates
      fetch('/api/components/sync', { method: 'POST' })
        .catch(() => console.log('[SW] Sync failed, will retry later'))
    );
  }
});
