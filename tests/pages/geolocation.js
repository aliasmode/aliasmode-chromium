// What a page reads from navigator.geolocation. tests/test_geolocation.py
// evaluates this file and calls these functions; geolocation.html loads it so
// the functions can also be run by hand from the console.

async function geolocationPermission() {
  return (await navigator.permissions.query({ name: "geolocation" })).state;
}

function geolocationCoords(position) {
  const c = position.coords;
  return {
    latitude: c.latitude, longitude: c.longitude, accuracy: c.accuracy,
    altitude: c.altitude, altitudeAccuracy: c.altitudeAccuracy, heading: c.heading, speed: c.speed,
  };
}

// One getCurrentPosition call: the fix or the error, and how long it took.
function geolocationRead() {
  const started = performance.now();
  return new Promise((resolve) => navigator.geolocation.getCurrentPosition(
    (position) => resolve({
      ok: true, ms: performance.now() - started, coords: geolocationCoords(position),
      timestamp: position.timestamp, now: Date.now(),
    }),
    (error) => resolve({ ok: false, ms: performance.now() - started, code: error.code, message: error.message }),
    { enableHighAccuracy: false, timeout: 15000, maximumAge: 0 },
  ));
}

// Every callback watchPosition makes in `windowMs`.
function geolocationWatch(windowMs) {
  return new Promise((resolve) => {
    const seen = [];
    const id = navigator.geolocation.watchPosition(
      (position) => seen.push({ ok: true, coords: geolocationCoords(position) }),
      (error) => seen.push({ ok: false, code: error.code }),
    );
    setTimeout(() => { navigator.geolocation.clearWatch(id); resolve(seen); }, windowMs);
  });
}
