// Probes two loopback ports the way a port-scanning page does: fetch, WebSocket
// and TURN over TCP, each `rounds` times per port, alternating the order. Posts
// every outcome and its time to /result. tests/test_closed_loopback_ports.py
// loads it with ?token=&listed=&free=&rounds=.

const params = new URLSearchParams(location.search);
const token = params.get("token");
const ports = { listed: Number(params.get("listed")), free: Number(params.get("free")) };
const rounds = Number(params.get("rounds") || 20);
// A probe that has not settled by then reached something that accepted it.
const SETTLE_MS = 10000;

function settle(promise, onTimeout) {
  return Promise.race([promise, new Promise((resolve) => setTimeout(() => resolve(onTimeout), SETTLE_MS))]);
}

async function viaFetch(port) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SETTLE_MS);
  try {
    await fetch(`http://127.0.0.1:${port}/`, { mode: "no-cors", cache: "no-store", signal: controller.signal });
    return "response";
  } catch (error) {
    return error.name;
  } finally {
    clearTimeout(timer);
  }
}

function viaWebSocket(port) {
  let socket;
  const outcome = new Promise((resolve) => {
    socket = new WebSocket(`ws://127.0.0.1:${port}/`);
    let errored = false;
    socket.onopen = () => resolve("open");
    socket.onerror = () => { errored = true; };
    socket.onclose = (event) => resolve(`close ${event.code}${errored ? " after error" : ""}`);
  });
  return settle(outcome, "timeout").finally(() => socket.close());
}

async function viaTurn(port) {
  const connection = new RTCPeerConnection({
    iceServers: [{ urls: `turn:127.0.0.1:${port}?transport=tcp`, username: "probe", credential: "probe" }],
    iceTransportPolicy: "relay",
  });
  const errors = new Set();
  let candidates = 0;
  connection.onicecandidateerror = (event) => errors.add(event.errorCode);
  connection.onicecandidate = (event) => { if (event.candidate && event.candidate.candidate) candidates += 1; };
  const complete = new Promise((resolve) => {
    connection.onicegatheringstatechange = () => { if (connection.iceGatheringState === "complete") resolve("complete"); };
  });
  connection.createDataChannel("probe");
  await connection.setLocalDescription(await connection.createOffer());
  const state = await settle(complete, "timeout");
  connection.close();
  return `${state} errors=${[...errors].sort().join("|")} candidates=${candidates}`;
}

const METHODS = { fetch: viaFetch, websocket: viaWebSocket, turn: viaTurn };

async function main() {
  const result = { token, ports };
  for (const name of Object.keys(METHODS)) {
    result[name] = { listed: [], free: [] };
  }
  for (let round = 0; round < rounds; round += 1) {
    for (const [name, probe] of Object.entries(METHODS)) {
      const order = round % 2 ? ["free", "listed"] : ["listed", "free"];
      for (const which of order) {
        const started = performance.now();
        const outcome = await probe(ports[which]);
        result[name][which].push({ outcome, ms: performance.now() - started });
      }
    }
  }
  await fetch(`/result?token=${token}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(result) });
}

main().catch((error) => fetch(`/result?token=${token}`, { method: "POST", body: JSON.stringify({ token, error: String(error && error.stack || error) }) }));
