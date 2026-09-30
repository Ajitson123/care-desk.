const CACHE_NAME = 'caredesk-shell-v1';
const SHELL_ASSETS = [
    '/static/app.css',
    '/static/app.js',
    '/static/patient-booking.js',
    '/static/hospital-mark.svg',
    '/static/hospital-192.png',
    '/static/hospital-512.png',
    '/static/manifest.webmanifest',
    '/static/offline.html'
];

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)));
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
    );
    self.clients.claim();
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method !== 'GET' || new URL(request.url).origin !== self.location.origin) return;

    if (new URL(request.url).pathname.startsWith('/static/')) {
        event.respondWith(caches.match(request).then((cached) => cached || fetch(request)));
        return;
    }

    if (request.mode === 'navigate') {
        event.respondWith(fetch(request).catch(() => caches.match('/static/offline.html')));
    }
});