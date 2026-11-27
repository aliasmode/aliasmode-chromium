// Lists the media devices before and after a capture permission, opens each
// listed microphone and camera, and posts what it read to the test server.
"use strict";

const token = new URLSearchParams(location.search).get("token");

const listed = async () => (await navigator.mediaDevices.enumerateDevices())
  .map(({ kind, deviceId, groupId, label }) => ({ kind, deviceId, groupId, label }));

async function capture(constraints) {
  try {
    const stream = await navigator.mediaDevices.getUserMedia(constraints);
    const tracks = stream.getTracks().map((track) => ({
      kind: track.kind,
      label: track.label,
      settings: track.getSettings(),
      capabilities: track.getCapabilities(),
    }));
    stream.getTracks().forEach((track) => track.stop());
    return { tracks };
  } catch (error) {
    return { error: `${error.name}: ${error.message}` };
  }
}

async function main() {
  const result = { before: await listed() };
  const kinds = new Set(result.before.map((device) => device.kind));
  result.audio = kinds.has("audioinput") ? await capture({ audio: true }) : null;
  result.video = kinds.has("videoinput") ? await capture({ video: true }) : null;
  result.after = await listed();
  result.each = [];
  for (const device of result.after) {
    if (device.kind === "audiooutput" || device.deviceId === "default" || device.deviceId === "communications") continue;
    const kind = device.kind === "audioinput" ? "audio" : "video";
    result.each.push({ deviceId: device.deviceId, ...(await capture({ [kind]: { deviceId: { exact: device.deviceId } } })) });
  }
  await fetch(`/result?token=${token}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(result) });
}

main().catch((error) => fetch(`/result?token=${token}`, { method: "POST", body: JSON.stringify({ error: String(error && error.stack || error) }) }));
