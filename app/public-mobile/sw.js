// Service worker for notifications.
//
// It exists only to receive pushes: there is no offline caching here, because
// a cached shell of a remote-control app is a lie — every screen in it is live
// state from a machine that may not be reachable.

self.addEventListener("push", (event) => {
  let payload = { title: "Daimon", body: "A session is waiting on you." };
  try {
    if (event.data) payload = { ...payload, ...event.data.json() };
  } catch {
    // A push with no readable payload still deserves to be shown — silently
    // dropping it is worse than showing the default.
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      // Collapses repeats: several questions while you are away should be one
      // line in the shade, not a stack of them.
      tag: "daimon-ask",
      renotify: true,
      data: { url: payload.url || "/" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      // Focus the app if it is already open rather than stacking another copy.
      for (const client of clients) {
        if ("focus" in client) return client.focus();
      }
      return self.clients.openWindow(target);
    }),
  );
});
