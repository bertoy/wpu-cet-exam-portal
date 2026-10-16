/* Service worker.
   -------------------------------------------------------------------------
   Deliberately conservative.  Hashed assets are cached forever because their
   URL pins their content; HTML and anything to do with an exam is always
   fetched from the network, so a student never sees a stale page and an exam
   is never served from a cache.
*/
var VERSION = "mechexam-v2";
var SHELL = VERSION + "-shell";
var ASSETS = VERSION + "-assets";

var OFFLINE_URL = "/offline";

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL).then(function (cache) {
      return cache.addAll([OFFLINE_URL]).catch(function () { return null; });
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(
        names.map(function (name) {
          if (name !== SHELL && name !== ASSETS) return caches.delete(name);
          return null;
        })
      );
    }).then(function () { return self.clients.claim(); })
  );
});

function isHashedAsset(url) {
  return url.pathname.indexOf("/asset/") === 0;
}

function neverCache(url) {
  var path = url.pathname;
  return (
    path.indexOf("/take/") === 0 ||
    path.indexOf("/api/") === 0 ||
    path.indexOf("/admin/") === 0 ||
    path.indexOf("/login") === 0 ||
    path.indexOf("/logout") === 0 ||
    path.indexOf("/service-worker.js") === 0
  );
}

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;

  var url;
  try { url = new URL(request.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;
  if (neverCache(url)) return;

  // Hashed assets: cache-first, and safe to keep, because a change to the
  // file changes its URL.
  if (isHashedAsset(url)) {
    event.respondWith(
      caches.match(request).then(function (hit) {
        if (hit) return hit;
        return fetch(request).then(function (response) {
          if (response && response.status === 200) {
            var copy = response.clone();
            caches.open(ASSETS).then(function (cache) {
              cache.put(request, copy);
            });
          }
          return response;
        });
      })
    );
    return;
  }

  // Pages: network first, falling back to the offline notice.
  if (request.headers.get("accept") &&
      request.headers.get("accept").indexOf("text/html") !== -1) {
    event.respondWith(
      fetch(request).catch(function () {
        return caches.match(OFFLINE_URL).then(function (hit) {
          return hit || new Response(
            "<h1>You are offline</h1><p>Reconnect and try again.</p>",
            { headers: { "Content-Type": "text/html" }, status: 503 }
          );
        });
      })
    );
  }
});
