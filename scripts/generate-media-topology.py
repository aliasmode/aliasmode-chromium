#!/usr/bin/env python3
"""Write resources/profiles/dispersion/media_topology.json from a device model.

The model is the one AliasMode Firefox draws its media devices from
(`firefox-data/media-devices.json` in the app, taken from the Camoufox
launcher): per operating system, the sound cards, headsets, display audio
outputs and cameras common on desktops, with relative weights and the share of
machines that add each kind of peripheral. It is not committed here. Pass its
path:

    scripts/generate-media-topology.py --model PATH/media-devices.json

The table this writes is derived from that model, not measured on Chrome, and
every option says so. What the derivation adds:

  * **Machine classes.** The table is conditioned on `machine_class`, so a
    laptop always has its built-in microphone and camera, a desktop never has
    a built-in camera, a Mac has its own model's speakers, and a class sold
    with a Studio Display always has the display's devices.
  * **Chrome's labels.** Audio labels are the operating system's names, which
    Chrome shows as they are. A USB camera's label ends in " (vid:pid)", as
    Chrome's VideoCaptureDeviceDescriptor::GetNameAndModel() writes it on
    Windows, macOS and Linux. The Linux entries of the model carry their
    vid:pid; a camera on another system gets the vid:pid of the Linux entry
    for the same product (USB_IDS). A camera with no known vid:pid is left
    out rather than given an invented one, and so are the cameras whose Chrome
    label is not the model's (an iPhone's Continuity Camera carries its
    owner's device name).
  * **Size classes and facing.** What an open camera reports: its largest
    frame size (`formats`) and, for a Windows built-in camera, the front panel
    that Windows reports as facingMode "user".
  * **Spare devices.** For each kind the class's first device in option
    order, listed only when the drawn machine has none of that kind and the
    host has one (patch 0177).
  * **One peripheral at most.** The model adds a headset, a display and a
    webcam independently. An option here holds at most one of them, weighted
    by the probability of exactly that one; machines with two or more are
    left out. Options below MIN_SHARE of their class are left out too, so the
    compiled table stays small; weights are relative, so the rest keep their
    proportions.

Output is byte-identical across runs: options are emitted in a fixed order and
the JSON is serialised with sorted keys, compact separators and ASCII escapes,
the form the other dispersion tables use.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "resources" / "profiles" / "dispersion" / "media_topology.json"

# Share of machines with no microphone, from the same model
# (firefoxMediaDevices in the app). It applies to desktops: a laptop has a
# built-in microphone.
NO_MICROPHONE = {"win": 0.08, "mac": 0.0, "lin": 0.2}

# Options with a smaller share of their class than this are dropped.
MIN_SHARE = 0.002
WEIGHT_SCALE = 1_000_000

# vid:pid of a camera product, from the model's Linux entries for the same
# product ("Integrated Camera: Integrated C (04f2:b6d9)" and so on). The C270's
# Linux entry is named "UVC Camera" and is identified by its microphone,
# "Webcam C270".
USB_IDS = {
    "Integrated Camera": "04f2:b6d9",
    "HP TrueVision HD Camera": "0408:5365",
    "USB2.0 HD UVC WebCam": "13d3:56a2",
    "Chicony USB2.0 Camera": "04f2:b6ea",
    "HD Pro Webcam C920": "046d:082d",
    "c922 Pro Stream Webcam": "046d:085c",
    "C922 Pro Stream Webcam": "046d:085c",
    "Logitech HD Webcam C270": "046d:0825",
    "Logitech BRIO": "046d:085e",
    "Microsoft® LifeCam HD-3000": "045e:0779",
}

# A camera's largest frame size, from the product's specification, by vid:pid
# or, for a camera that is not a USB device, by name: "hd" is 1280x720, "fhd"
# 1920x1080, "uhd" 3840x2160. A camera not listed gets the default, "fhd".
# Apple's silicon-era FaceTime HD camera and the Studio Display camera deliver
# 1080p; the first MacBook Air with an M1 has a 720p camera, which a table
# keyed on the machine class cannot separate.
SIZE_CLASSES = {
    "04f2:b6d9": "hd",  # Integrated Camera
    "0408:5365": "hd",  # HP TrueVision HD Camera
    "13d3:56a2": "hd",  # USB2.0 HD UVC WebCam
    "04f2:b6ea": "hd",  # Chicony USB2.0 Camera
    "046d:0825": "hd",  # Logitech C270
    "045e:0779": "hd",  # Microsoft LifeCam HD-3000
    "046d:082d": "fhd",  # Logitech C920
    "046d:085c": "fhd",  # Logitech C922
    "046d:085e": "uhd",  # Logitech BRIO
    "FaceTime HD Camera": "fhd",
    "Studio Display Camera": "fhd",
}

# Machine classes: the operating system, whether the machine is a laptop, the
# Mac model whose sound card it has, and whether it comes with a Studio
# Display. linux-server is not here: a server has no media devices, and its
# option is written as it is.
CLASSES = {
    "mac-air13": ("mac", True, "MacBook Air", False),
    "mac-air15": ("mac", True, "MacBook Air", False),
    "mac-mbp14": ("mac", True, "MacBook Pro", False),
    "mac-mbp16": ("mac", True, "MacBook Pro", False),
    "mac-mbp14-studio-display": ("mac", True, "MacBook Pro", True),
    "mac-mbp16-studio-display": ("mac", True, "MacBook Pro", True),
    "mac-mini-studio-display": ("mac", False, "Mac mini", True),
    "mac-mini-external": ("mac", False, "Mac mini", False),
    "mac-studio-studio-display": ("mac", False, "Mac Studio", True),
    "win-desktop-tower": ("win", False, None, False),
    "win-sff-desktop": ("win", False, None, False),
    "win-laptop-15": ("win", True, None, False),
    "linux-desktop-tower": ("lin", False, None, False),
}

# The Linux desktop classes pair with NVIDIA GPUs, and a desktop has no
# laptop chipset or digital microphone array. Sound devices that name another
# GPU vendor's HDMI audio, a mobile chipset or a microphone array are kept off
# desktops, and NVIDIA or AMD display audio off the Windows laptop, whose GPU
# is integrated. AirPods names stay on macOS, the rule
# scripts/test_profile_resolver.py holds the table to.
DESKTOP_EXCLUDED = re.compile(r"Microphone Array|Lake-LP|Lake-P|Lake PCH-P|Rembrandt|Navi |Raven")
LAPTOP_EXCLUDED = re.compile(r"NVIDIA|AMD")
OFF_MAC = re.compile(r"AirPods")

SOURCE = (
    "AliasMode Firefox device model (firefox-data/media-devices.json, from the "
    "Camoufox launcher), via scripts/generate-media-topology.py"
)
NOTE = (
    "DERIVED, NOT MEASURED ON CHROME: drawn from the device model AliasMode "
    "Firefox uses, with Chrome's label forms and the machine class's chassis. "
    "Camera size classes and facing are from product specifications."
)


def camera_label(os_key: str, name: str) -> str | None:
    """The label Chrome shows for a camera, or None if it is not known."""
    if os_key == "lin":
        return name
    if name in ("FaceTime HD Camera", "Studio Display Camera"):
        return name
    usb = USB_IDS.get(name)
    return f"{name} ({usb})" if usb else None


def camera_device(os_key: str, name: str, group: str, built_in: bool) -> dict:
    label = camera_label(os_key, name)
    assert label is not None, name
    device = {"kind": "videoinput", "label": label, "group": group}
    usb = re.search(r"\(([0-9a-f]{4}:[0-9a-f]{4})\)$", label)
    size = SIZE_CLASSES.get(usb.group(1) if usb else label)
    if size:
        device["formats"] = size
    if os_key == "win" and built_in:
        device["facing"] = "user"
    return device


def audio_devices(item: dict, group: str, mics: bool = True) -> list[dict]:
    devices = [{"kind": "audioinput", "label": label, "group": group} for label in item.get("mics", [])] if mics else []
    devices += [{"kind": "audiooutput", "label": label, "group": group} for label in item.get("outs", [])]
    return devices


def weighted(items: list[dict]) -> list[tuple[dict, float]]:
    total = sum(item.get("w", 1) for item in items)
    return [(item, item.get("w", 1) / total) for item in items]


def allowed(os_key: str, laptop: bool, item: dict) -> bool:
    text = " ".join(item.get("mics", []) + item.get("outs", []) + [item.get("cam") or "", item.get("mic") or ""])
    if os_key != "mac" and OFF_MAC.search(text):
        return False
    if not laptop and DESKTOP_EXCLUDED.search(text):
        return False
    if laptop and os_key == "win" and LAPTOP_EXCLUDED.search(text):
        return False
    return True


def class_options(machine_class: str, model: dict) -> list[dict]:
    os_key, laptop, mac_model, studio_display = CLASSES[machine_class]
    system = model[os_key]
    keep = lambda items: [item for item in items if allowed(os_key, laptop, item)]  # noqa: E731

    # The machine's own sound card, with or without its microphone.
    if os_key == "mac":
        cards = [card for card in system["cards"] if card["model"] == mac_model and card.get("cam") != "FaceTime HD Camera (Built-in)"]
    else:
        cards = keep(system["cards"])
    bases = []
    for card, share in weighted(cards):
        variants = [(True, 1.0)] if laptop or NO_MICROPHONE[os_key] == 0 else [(True, 1 - NO_MICROPHONE[os_key]), (False, NO_MICROPHONE[os_key])]
        for with_mic, mic_share in variants:
            if not with_mic and not card.get("mics"):
                continue
            devices = audio_devices(card, "card", mics=with_mic)
            if card.get("cam"):
                devices.append(camera_device(os_key, card["cam"], "camera", True))
            bases.append((f"card{cards.index(card)}{'' if with_mic else '.nomic'}", devices, share * mic_share))

    # A Windows laptop's built-in camera. A Mac's comes with its card.
    if laptop and os_key == "win":
        built_in = [camera for camera in system["cameras"] if not camera.get("mic") and camera_label(os_key, camera["cam"])]
        bases = [
            (f"{base_id}.cam{built_in.index(camera)}", devices + [camera_device(os_key, camera["cam"], "camera", True)], share * camera_share)
            for (base_id, devices, share), (camera, camera_share) in itertools.product(bases, weighted(built_in))
        ]

    if studio_display:
        display = next(item for item in system["displays"] if "Studio Display Speakers" in item.get("outs", []))
        for _, devices, _ in bases:
            devices += audio_devices(display, "studio-display")
            devices.append(camera_device(os_key, display["cam"], "studio-display", False))

    # At most one peripheral, weighted by the chance of exactly that one.
    p_headset = system.get("p_headset", 0.0)
    p_display = 0.0 if studio_display else system.get("p_display", 0.0)
    p_camera = system.get("p_extra_camera", 0.0)
    peripherals = [("", [], (1 - p_headset) * (1 - p_display) * (1 - p_camera))]
    for index, (headset, share) in enumerate(weighted(keep(system["headsets"]))):
        peripherals.append((f".headset{index}", audio_devices(headset, "headset"), p_headset * (1 - p_display) * (1 - p_camera) * share))
    if p_display:
        displays = [item for item in keep(system["displays"]) if "Studio Display Speakers" not in item.get("outs", [])]
        for index, (display, share) in enumerate(weighted(displays)):
            peripherals.append((f".display{index}", audio_devices(display, "display"), (1 - p_headset) * p_display * (1 - p_camera) * share))
    webcams = [camera for camera in keep(system["cameras"]) if camera.get("mic") and camera_label(os_key, camera["cam"])]
    for index, (camera, share) in enumerate(weighted(webcams)):
        devices = [camera_device(os_key, camera["cam"], "webcam", False), {"kind": "audioinput", "label": camera["mic"], "group": "webcam"}]
        peripherals.append((f".webcam{index}", devices, (1 - p_headset) * (1 - p_display) * p_camera * share))

    options = []
    for (base_id, base_devices, base_share), (peripheral_id, peripheral_devices, peripheral_share) in itertools.product(bases, peripherals):
        devices = [dict(device) for device in base_devices + peripheral_devices]
        # PulseAudio lists a monitor source of every output as a microphone.
        if system.get("monitor_sources"):
            devices += [{"kind": "audioinput", "label": f"Monitor of {device['label']}", "group": device["group"]}
                        for device in list(devices) if device["kind"] == "audiooutput"]
        options.append((f"{base_id}{peripheral_id}", devices, base_share * peripheral_share))

    total = sum(share for _, _, share in options)
    options = [(option_id, devices, share / total) for option_id, devices, share in options if share / total >= MIN_SHARE]
    return [{"id": option_id, "devices": order(devices), "weight": max(1, round(share * WEIGHT_SCALE))} for option_id, devices, share in options]


def order(devices: list[dict]) -> list[dict]:
    """Microphones, then speakers, then cameras, each in the order drawn."""
    rank = {"audioinput": 0, "audiooutput": 1, "videoinput": 2}
    return sorted(devices, key=lambda device: rank[device["kind"]])


def option_value(devices: list[dict], spares: list[dict]) -> dict:
    media = {f"{kind}_count": sum(1 for device in devices if device["kind"] == kind)
             for kind in ("audioinput", "audiooutput", "videoinput")}
    media["devices"] = devices
    missing = [spare for spare in spares if media[f"{spare['kind']}_count"] == 0]
    if missing:
        media["spare_devices"] = missing
    return {"media": media}


def build(model: dict, previous: dict) -> dict:
    option_sets = []
    for machine_class in CLASSES:
        options = class_options(machine_class, model)
        spares = []
        for kind in ("audioinput", "audiooutput", "videoinput"):
            first = next((device for option in options for device in option["devices"] if device["kind"] == kind), None)
            if first:
                spares.append(dict(first))
        option_sets.append({
            "key": {"machine_class": machine_class},
            "options": [{
                "evidence": "catalogue-value",
                "id": option["id"],
                "note": NOTE,
                "source": SOURCE,
                "value": option_value(option["devices"], spares),
                "weight": option["weight"],
            } for option in options],
        })
    # Classes this script does not model keep their option sets as they are.
    for option_set in previous["option_sets"]:
        if option_set["key"]["machine_class"] not in CLASSES:
            option_sets.append(option_set)
    table = dict(previous)
    table["option_sets"] = option_sets
    table["description"] = (
        "enumerateDevices counts and labels, conditioned on the machine class. The labels name "
        "the machine, so they follow the machine class rather than the platform persona. The "
        "persona's list replaces the host's (patch 0177). Written by "
        "scripts/generate-media-topology.py from the device model AliasMode Firefox uses; derived, "
        "not measured on Chrome."
    )
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", type=Path, required=True, help="the device model, media-devices.json")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    model = json.loads(args.model.read_text(encoding="utf-8"))
    previous = json.loads(args.out.read_text(encoding="utf-8"))
    table = build(model, previous)
    args.out.write_text(json.dumps(table, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n", encoding="utf-8")
    counts = {option_set["key"]["machine_class"]: len(option_set["options"]) for option_set in table["option_sets"]}
    print(f"generate-media-topology: {sum(counts.values())} options: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
