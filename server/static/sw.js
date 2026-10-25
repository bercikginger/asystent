// Minimalny service worker – potrzebny, żeby Chrome pozwolił zainstalować apkę.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
