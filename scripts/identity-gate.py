#!/usr/bin/env python3
"""Old-profile identity gate: two browser builds must compose the same device.

An existing profile keeps its seed and its launch switches across an engine
update, so the new build must compose, for every seed, exactly the device the
old one composed. Only the browser version may move. This launches both
binaries on this host for the same seeds and switches and compares:

- the composed profile (the JSON a child process carries in --apostate-profile:
  GPU, screen, CPU, memory, fonts, media topology, voices, battery, ...), and
- the --fingerprint-explain report (chosen option per surface, root, limits).

Version strings are normalised before comparing: the full browser version, the
major version, and the GREASE brand of the UA-CH brand lists, which Chrome
derives from the major version. Any other difference fails the gate.

Example (Windows build server):
  python scripts/identity-gate.py --old D:/gate/155/chrome.exe \
    --new D:/gate/159/chrome.exe --seeds 200 --out D:/gate/out

It launches each binary headless with a temporary user data directory, no
automation, and kills it as soon as the profile is read. Exit status 0 means
identical, 1 a difference (see <out>/diffs.json), 2 a launch that failed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

PERSONAS = ("windows", "macos", "linux")
# Screens the app stores for new profiles, so the "app" variant passes the
# overrides a real profile launch passes.
SCREENS = ((1920, 1080), (1366, 768), (1536, 864), (2560, 1440), (1440, 900), (1280, 720))
CORES = (4, 8, 12, 16)
LOCALE = "en-US"
TIMEZONE = "America/New_York"


def seeds_for(persona: str, count: int) -> list[int]:
    """App seeds are unsigned 32-bit integers (fingerprint.ts deterministicSeed)."""
    rng = random.Random(f"identity-gate-{persona}")
    return [rng.randrange(1, 2 ** 32) for _ in range(count)]


def switches(persona: str, seed: int, variant: str) -> list[str]:
    out = [f"--fingerprint={seed}", f"--fingerprint-platform={persona}"]
    if variant == "app":
        width, height = SCREENS[seed % len(SCREENS)]
        cores = min(CORES[seed % len(CORES)], os.cpu_count() or 4)
        out += [f"--fingerprint-screen-width={width}", f"--fingerprint-screen-height={height}",
                f"--fingerprint-timezone={TIMEZONE}", f"--fingerprint-locale={LOCALE}",
                f"--fingerprint-hardware-concurrency={cores}", "--fingerprint-noise"]
    return out


def _children(pid: int) -> list[str]:
    """Command lines of the browser's descendants."""
    if os.name == "nt":
        script = ("$all = Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine;"
                  f"$ids = @({pid}); $more = $true; while ($more) {{ $more = $false;"
                  " foreach ($p in $all) { if ($ids -contains $p.ParentProcessId -and -not ($ids -contains $p.ProcessId))"
                  " { $ids += $p.ProcessId; $more = $true } } };"
                  f" $all | Where-Object {{ $_.ProcessId -ne {pid} -and $ids -contains $_.ProcessId }} |"
                  " ForEach-Object { $_.CommandLine }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, check=False).stdout
        return out.splitlines()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    import harness  # noqa: PLC0415  (Linux: reads rewritten process titles whole)
    return harness._descendants(harness._processes(), pid)


def _profile_from(commands: list[str], user_data_dir: str) -> dict[str, Any] | None:
    # Only this launch's children: Windows reuses process ids at once, so a
    # child of an earlier, dying browser can name the same parent id.
    for command in commands:
        if user_data_dir not in command:
            continue
        for token in command.split():
            if token.startswith("--apostate-profile="):
                try:
                    payload = json.loads(base64.b64decode(token.split("=", 1)[1], validate=True))
                except (ValueError, binascii.Error):
                    continue
                return payload.get("device_profile", payload)
    return None


def _no_sandbox() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    import harness  # noqa: PLC0415
    return harness._sandbox_unavailable()


def explain(binary: str, args: list[str]) -> str:
    return subprocess.run([binary, *args, "--fingerprint-explain"], capture_output=True, text=True,
                          timeout=120, check=False).stdout


def composed(binary: str, args: list[str], timeout: float = 60) -> dict[str, Any] | None:
    user_data_dir = tempfile.mkdtemp(prefix="idgate-")
    command = [binary, f"--user-data-dir={user_data_dir}", "--no-first-run", "--no-default-browser-check",
               "--headless=new", *args]
    if _no_sandbox():
        command.append("--no-sandbox")
    command.append("about:blank")
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1.5)
            profile = _profile_from(_children(process.pid), user_data_dir)
            if profile is not None:
                return profile
            if process.poll() is not None:
                return None
        return None
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        for _ in range(10):
            shutil.rmtree(user_data_dir, ignore_errors=True)
            if not os.path.exists(user_data_dir):
                break
            time.sleep(1)


def normaliser(old_version: str, new_version: str, touched: set[str]):
    majors = {old_version.split(".")[0], new_version.split(".")[0]}
    full = re.compile("|".join(re.escape(v) for v in (old_version, new_version)))
    major = re.compile(r"(?<![\d.])(" + "|".join(majors) + r")(?=\.0\.0\.0|\b(?!\.\d))")

    def text(value: str) -> str:
        return major.sub("<MAJOR>", full.sub("<VERSION>", value))

    def walk(value: Any, path: str = "") -> Any:
        if isinstance(value, dict):
            return {k: walk(v, f"{path}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            items = [walk(v, f"{path}[]") for v in value]
            # Chrome's GREASE brand (name and version) is a function of the
            # major version, so it is a version string too.
            if path.endswith(("brands", "full_version_list", "fullVersionList")):
                kept = [i for i in items if not (isinstance(i, dict) and "Brand" in json.dumps(i)
                                                 and "Chrom" not in json.dumps(i))]
                if len(kept) != len(items):
                    touched.add(path + " (GREASE brand)")
                items = kept
            return items
        if isinstance(value, str) and text(value) != value:
            touched.add(path)
            return text(value)
        return value

    return walk, text


def diff(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append(f"{path}.{key}: only in {'new' if key in b else 'old'}")
            else:
                out += diff(a[key], b[key], f"{path}.{key}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: {json.dumps(a)[:200]} != {json.dumps(b)[:200]}"]


def explain_lines(report: str, text) -> list[str]:
    # The chromium and catalogue lines name the build, not the device.
    skip = ("chromium ", "catalogue ")
    return [text(line) for line in report.splitlines() if not line.lstrip().startswith(skip)]


def binary_version(binary: str) -> str:
    if os.name == "nt":
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-Item '{binary}').VersionInfo.FileVersion"],
                             capture_output=True, text=True, check=False).stdout.strip()
    else:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False).stdout
    found = re.search(r"\d+\.\d+\.\d+\.\d+", out)
    if not found:
        raise SystemExit(f"cannot read the version of {binary}")
    return found.group(0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--old", required=True, help="the released browser binary")
    parser.add_argument("--new", required=True, help="the candidate browser binary")
    parser.add_argument("--seeds", type=int, default=200, help="seeds per persona OS")
    parser.add_argument("--personas", default=",".join(PERSONAS))
    parser.add_argument("--variants", default="bare,app", help="bare: seed and OS only; app: plus the app's switches")
    parser.add_argument("--profile-personas", default=",".join(PERSONAS),
                        help="personas whose full profile is compared; the rest compare the explain report only."
                             " On Windows a macOS persona's profile is longer than a child command line can be"
                             " (32767 characters), so no child starts and only the report can be compared there")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    old_version, new_version = binary_version(args.old), binary_version(args.new)
    touched: set[str] = set()
    walk, text = normaliser(old_version, new_version, touched)
    args.out.mkdir(parents=True, exist_ok=True)
    cases = [(persona, seed, variant) for persona in args.personas.split(",")
             for seed in seeds_for(persona, args.seeds) for variant in args.variants.split(",")]

    def run(case):
        persona, seed, variant = case
        launch = switches(persona, seed, variant)
        result = {"persona": persona, "seed": seed, "variant": variant, "diffs": [], "failed": []}
        reports = {}
        full = persona in args.profile_personas.split(",")
        for side, binary in (("old", args.old), ("new", args.new)):
            profile = composed(binary, launch) if full else {}
            if profile is None:
                profile = composed(binary, launch)  # one retry for a slow start
            if profile is None:
                result["failed"].append(f"{side}: no --apostate-profile in any child process")
            reports[side] = (profile, explain(binary, launch))
        result["compared"] = "profile+explain" if full else "explain"
        if not result["failed"]:
            result["diffs"] += ["profile" + d for d in diff(walk(reports["old"][0]), walk(reports["new"][0]))]
            old_lines = explain_lines(reports["old"][1], text)
            new_lines = explain_lines(reports["new"][1], text)
            if not old_lines or old_lines != new_lines:
                result["diffs"] += [f"explain: {o!r} != {n!r}" for o, n in zip(old_lines, new_lines) if o != n]
                if len(old_lines) != len(new_lines) or not old_lines:
                    result["diffs"].append(f"explain: {len(old_lines)} lines != {len(new_lines)} lines")
            result["groups"] = sorted(reports["new"][0])
            sample = args.out / f"sample-{persona}-{variant}.json"
            if full and not sample.exists():
                sample.write_text(json.dumps({side: reports[side][0] for side in reports}, indent=1),
                                  encoding="utf-8")
        return result

    started = time.time()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(run, cases))
    differing = [r for r in results if r["diffs"]]
    failed = [r for r in results if r["failed"]]
    groups = sorted({g for r in results for g in r.get("groups", [])})
    summary = {
        "old": {"binary": args.old, "version": old_version},
        "new": {"binary": args.new, "version": new_version},
        "host": sys.platform, "cpu_count": os.cpu_count(),
        "cases": len(results), "seeds_per_persona": args.seeds, "personas": args.personas.split(","),
        "variants": args.variants.split(","), "profile_personas": args.profile_personas.split(","),
        "identical": len(results) - len(differing) - len(failed),
        "differing": len(differing), "failed": len(failed), "profile_groups": groups,
        "normalised_paths": sorted(touched),
        "seconds": round(time.time() - started),
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (args.out / "diffs.json").write_text(json.dumps(differing + failed, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 2 if failed else 1 if differing else 0


if __name__ == "__main__":
    sys.exit(main())
