"""The persona's media devices replace the host's, before and after a capture permission (patch 0177).

The page enumerates, calls getUserMedia() and enumerates again. The browser
runs with --use-fake-ui-for-media-stream, which only accepts the permission
prompt. --use-fake-device-for-media-stream is not used: it replaces the whole
capture stack with Chromium's fake devices and would bypass the layer under
test.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import tempfile
from collections import Counter

import pytest

import harness

KINDS = ("audioinput", "audiooutput", "videoinput")
_RUNS: dict[tuple[str | None, str], tuple[dict, dict | None]] = {}


def _run(server, binary, persona, seed, timeout=90):
    """The bare binary on media.html, no automation, as harness.run_probe launches it."""
    key = (persona, seed)
    if key in _RUNS:
        return _RUNS[key]
    token = secrets.token_hex(8)
    launch = harness.Launch(persona, seed, "bare", args=["--use-fake-ui-for-media-stream"])
    user_data_dir = tempfile.mkdtemp(prefix="apostate-media-")
    command = [binary, f"--user-data-dir={user_data_dir}", "--no-first-run", "--no-default-browser-check",
               "--headless=new", *harness.switches(launch)]
    if harness._sandbox_unavailable():
        command.append("--no-sandbox")
    command.append(f"{server.origin}/media.html?token={token}")
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        values = server.wait(token, timeout)
        profile = None if launch.host_mode else harness.composed_profile(process.pid)
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(user_data_dir, ignore_errors=True)
    assert "error" not in values, values["error"]
    _RUNS[key] = (values, profile)
    return _RUNS[key]


def _chrome_label(label):
    """The label as Chrome shows it: AudioDeviceDescription::RedactDeviceName rewrites AirPods names."""
    if "AirPods" not in label:
        return label
    for profile in ("Hands-Free AG Audio", "Hands-Free", "Stereo"):
        if profile in label:
            return f"AirPods {profile}"
    return "AirPods"


def _device_name(label):
    """The device a label names, without the prefix of a virtual default or communications entry."""
    for prefix in ("Default - ", "Communications - "):
        if label.startswith(prefix):
            return label[len(prefix):]
    return label


def _expected(profile, host_kinds):
    """Per kind, the labels the persona lists after a permission grant."""
    media = profile.get("media") or {}
    windows = profile["platform"]["name"] == "Windows"
    want = {}
    for kind in KINDS:
        devices = [_chrome_label(device["label"]) for device in media.get("devices", []) if device["kind"] == kind]
        if not devices and kind in host_kinds:
            # A machine with none of this kind shows its spare when the host has one.
            devices = [_chrome_label(device["label"]) for device in media.get("spare_devices", []) if device["kind"] == kind][:1]
        if devices and kind != "videoinput":
            virtual = [f"Default - {devices[0]}"] + ([f"Communications - {devices[0]}"] if windows else [])
            devices = virtual + devices
        want[kind] = devices
    return want


def _host(server, binary):
    values, _ = _run(server, binary, None, "host")
    return {device["kind"] for device in values["before"]}, {device["label"] for device in values["after"] if device["label"]}


def test_the_page_sees_only_the_persona_devices(persona_seed, server, binary, record):
    persona, seed = persona_seed
    host_kinds, host_labels = _host(server, binary)
    values, profile = _run(server, binary, persona, seed)
    assert profile is not None, "no composed profile was found on a child process"
    want = _expected(profile, host_kinds)
    got = {kind: [device["label"] for device in values["after"] if device["kind"] == kind] for kind in KINDS}
    record.update(host_kinds=sorted(host_kinds), want=want, got=got)

    # No host label, unless the persona names the same device.
    page = {_device_name(label) for labels in got.values() for label in labels}
    named = {_device_name(label) for labels in want.values() for label in labels}
    leaked = page & ({_device_name(label) for label in host_labels} - named)
    assert not leaked, f"host labels on the page: {sorted(leaked)}"

    # Before a permission: one entry per kind the persona lists, with no id, group or label.
    before = Counter(device["kind"] for device in values["before"])
    assert before == Counter(kind for kind in KINDS if want[kind]), values["before"]
    assert all(not (device["deviceId"] or device["groupId"] or device["label"]) for device in values["before"])

    # After it: exactly the persona's labels and counts.
    assert {kind: Counter(labels) for kind, labels in got.items()} == {kind: Counter(labels) for kind, labels in want.items()}


def test_a_track_is_the_device_it_opened(persona_seed, server, binary, record):
    persona, seed = persona_seed
    values, _ = _run(server, binary, persona, seed)
    by_id = {device["deviceId"]: device for device in values["after"]}
    opened = [entry for entry in (values["audio"], values["video"], *values["each"]) if entry]
    record.update(opened=opened)
    if not opened:
        pytest.skip("the persona lists no microphone or camera on this host")
    for entry in opened:
        assert "error" not in entry, entry
        for track in entry["tracks"]:
            device = by_id.get(track["settings"].get("deviceId"))
            assert device is not None, f"settings.deviceId names no listed device: {track}"
            assert track["label"] == device["label"], (track, device)
            if track["kind"] == "video":
                # What the camera delivers is in what it said it offers.
                capabilities = track["capabilities"]
                assert capabilities["width"]["min"] <= track["settings"]["width"] <= capabilities["width"]["max"]
                assert capabilities["height"]["min"] <= track["settings"]["height"] <= capabilities["height"]["max"]
                assert not {"pan", "tilt", "zoom"} & set(capabilities), capabilities
    for entry in values["each"]:
        assert all(track["settings"]["deviceId"] == entry["deviceId"] for track in entry["tracks"]), entry


def test_same_seed_same_devices(persona_seed, server, binary, record):
    persona, seed = persona_seed
    first, _ = _run(server, binary, persona, seed)
    _RUNS.pop((persona, seed))
    second, _ = _run(server, binary, persona, seed)
    labels = lambda values: sorted((device["kind"], device["label"]) for device in values["after"])  # noqa: E731
    record.update(first=labels(first), second=labels(second))
    assert labels(first) == labels(second)


def test_different_seeds_differ(persona, server, binary, pytestconfig, record):
    seeds = [seed.strip() for seed in pytestconfig.getoption("--seeds").split(",") if seed.strip()]
    runs = {seed: _run(server, binary, persona, seed) for seed in seeds}
    devices = {seed: sorted((d["kind"], d["label"]) for d in (profile.get("media") or {}).get("devices", []))
               for seed, (_, profile) in runs.items()}
    labels = {seed: sorted((d["kind"], d["label"]) for d in values["after"]) for seed, (values, _) in runs.items()}
    record.update(labels=labels)
    pairs = [(a, b) for i, a in enumerate(seeds) for b in seeds[i + 1:] if devices[a] != devices[b]]
    if not pairs:
        pytest.skip(f"seeds {seeds} compose the same devices for {persona}; pass other --seeds")
    for a, b in pairs:
        assert labels[a] != labels[b], f"seeds {a} and {b} compose different devices but the page lists the same"
