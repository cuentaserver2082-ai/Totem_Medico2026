// Service worker mínimo: Chrome lo exige para ofrecer "Instalar app".
// No guarda nada en caché a propósito (hay datos de pacientes): todo va siempre a la red.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => { e.respondWith(fetch(e.request)); });
