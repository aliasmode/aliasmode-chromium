#!/usr/bin/env python3
"""Old-profile identity gate: two browser builds must compose the same device.

An existing profile keeps its seed and its launch switches across an engine
update, so the new build must compose, for every seed, exactly the device the
old one composed. Only the browser version may move. This launches both
binaries on this host for the same seeds and switches and compares:

- the composed profile (the JSON a child process carries in --apostate-profile:
  GPU, screen, CPU, memory, fonts, media topology, voices, battery, ...), and
- the --fingerprint-explain report (chosen option per surface, root, limits).

Version strings are normalised before comparing, and only in the fields that
carry the browser version (browser.user_agent and the UA-CH brand lists): the
full browser version, the major version, and the GREASE brand, which Chrome
derives from the major version. In the explain report the chromium line must
name each binary's own version, and the catalogue line is compared except for
its tables digest: the tables embed the user agent, so the digest moves with
the major. Both digests are listed in summary.json. Any other difference fails
the gate.

--page-seeds N adds a page check on the new binary: for N seeds a local page
reads canvas and WebGL pixels back. With --fingerprint-noise the readback must
differ from the same launch without it (noise present), match a second launch
(stable), and differ between seeds. The same page on the old binary is
reported, not gated: a Chromium update may change how a page draws.

Example (Windows build server):
  python scripts/identity-gate.py --old D:/gate/155/chrome.exe \
    --new D:/gate/159/chrome.exe --seeds 200 --profile-personas windows,linux \
    --page-seeds 20 --out D:/gate/out

The "app" variant passes what launcher.ts passes for a profile: screen (in CSS
pixels at the Windows display scale), --window-size from launchWindowSize(),
timezone, locale, cores, --fingerprint-noise, the WebRTC policy and
--fingerprint-webrtc-ip, and on Windows --ignore-gpu-blocklist and
--force-device-scale-factor.

It launches each binary headless with a temporary user data directory, no
automation, and kills it as soon as the profile or the page result is read.
Exit status 0 means identical, 1 a difference (see <out>/diffs.json) or a page
check problem (<out>/page.json), 2 a launch that failed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

PERSONAS = ("windows", "macos", "linux")
# Screens the app stores for new profiles, so the "app" variant passes the
# overrides a real profile launch passes.
SCREENS = ((1920, 1080), (1366, 768), (1536, 864), (2560, 1440), (1440, 900), (1280, 720))
CORES = (4, 8, 12, 16)
# Windows display scales the app passes as --force-device-scale-factor.
SCALES = (1, 1.25, 1.5)
LOCALE = "en-US"
TIMEZONE = "America/New_York"
# The only profile fields that carry the browser version.
VERSION_FIELDS = (".browser.user_agent",)
VERSION_LISTS = ("brands", "full_version_list", "fullVersionList")


def seeds_for(persona: str, count: int) -> list[int]:
    """App seeds are unsigned 32-bit integers (fingerprint.ts deterministicSeed)."""
    rng = random.Random(f"identity-gate-{persona}")
    return [rng.randrange(1, 2 ** 32) for _ in range(count)]


def switches(persona: str, seed: int, variant: str) -> list[str]:
    out = [f"--fingerprint={seed}", f"--fingerprint-platform={persona}"]
    if variant == "app":
        # As launcher.ts: on Windows the screen is passed in CSS pixels at the display scale,
        # and the window is launchWindowSize() of that screen (fingerprint.ts).
        width, height = SCREENS[seed % len(SCREENS)]
        scale = SCALES[seed % len(SCALES)] if os.name == "nt" else 1
        width, height = js_round(width / scale), js_round(height / scale)
        cores = min(CORES[seed % len(CORES)], os.cpu_count() or 4)
        out += [f"--window-size={min(width, max(1024, js_round(width * 0.65)))},"
                f"{min(height, max(480, js_round(height * 0.9)))}", "--window-position=0,0",
                f"--fingerprint-screen-width={width}", f"--fingerprint-screen-height={height}",
                f"--fingerprint-timezone={TIMEZONE}", f"--fingerprint-locale={LOCALE}",
                f"--fingerprint-hardware-concurrency={cores}", "--fingerprint-noise"]
        if os.name == "nt":
            out += ["--ignore-gpu-blocklist", f"--force-device-scale-factor={scale:g}"]
        # Every app profile with AliasMode Chromium has a proxy; 203.0.113.0/24 is a documentation range.
        out += ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                f"--fingerprint-webrtc-ip=203.0.113.{seed % 254 + 1}"]
    return out


def js_round(value: float) -> int:
    """JavaScript Math.round for positive numbers (Python's round() rounds half to even)."""
    return int(value + 0.5)


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
        _stop(process, user_data_dir)


def _stop(process: subprocess.Popen, user_data_dir: str) -> None:
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


PAGE = b"""<!doctype html><meta charset="utf-8"><body><script>
"use strict";
async function sha(data) {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
const bytes = (url) => new TextEncoder().encode(url);
async function canvas2d() {
  const c = document.createElement("canvas");
  c.width = 280; c.height = 60;
  const x = c.getContext("2d");
  x.textBaseline = "top"; x.font = "16px Arial";
  x.fillStyle = "#f60"; x.fillRect(125, 1, 62, 20);
  x.fillStyle = "#069"; x.fillText("AliasMode identity gate", 2, 15);
  x.fillStyle = "rgba(102, 204, 0, 0.7)"; x.fillText("AliasMode identity gate", 4, 17);
  x.beginPath(); x.arc(240, 30, 20, 0, Math.PI * 2); x.strokeStyle = "#a0a"; x.stroke();
  return { pixels: await sha(x.getImageData(0, 0, c.width, c.height).data), dataUrl: await sha(bytes(c.toDataURL())) };
}
async function webgl() {
  const c = document.createElement("canvas");
  c.width = 64; c.height = 64;
  const gl = c.getContext("webgl", { preserveDrawingBuffer: true });
  if (!gl) return { error: "no webgl context" };
  const shader = (type, source) => { const s = gl.createShader(type); gl.shaderSource(s, source); gl.compileShader(s); return s; };
  const program = gl.createProgram();
  gl.attachShader(program, shader(gl.VERTEX_SHADER, "attribute vec2 p; varying vec2 v; void main() { v = p; gl_Position = vec4(p, 0.0, 1.0); }"));
  gl.attachShader(program, shader(gl.FRAGMENT_SHADER, "precision mediump float; varying vec2 v; void main() { gl_FragColor = vec4(v * 0.5 + 0.5, 0.3, 1.0); }"));
  gl.linkProgram(program); gl.useProgram(program);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-0.9, -0.9, 0.9, -0.7, 0.1, 0.9]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  gl.clearColor(0.1, 0.1, 0.1, 1); gl.clear(gl.COLOR_BUFFER_BIT); gl.drawArrays(gl.TRIANGLES, 0, 3);
  const pixels = new Uint8Array(64 * 64 * 4);
  gl.readPixels(0, 0, 64, 64, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
  return { pixels: await sha(pixels), dataUrl: await sha(bytes(c.toDataURL())), renderer: gl.getParameter(gl.RENDERER) };
}
(async () => {
  const result = {};
  for (const [name, read] of [["canvas", canvas2d], ["webgl", webgl]]) {
    try { result[name] = await read(); } catch (e) { result[name] = { error: String(e) }; }
  }
  await fetch("/result" + location.search, { method: "POST", body: JSON.stringify(result) });
})();
</script>"""


class PageServer:
    """Serves the readback page on 127.0.0.1 and collects what it posts, by token."""

    def __init__(self) -> None:
        self.results: dict[str, Any] = {}
        self.events: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _token(self) -> str:
                return self.path.partition("token=")[2]

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(PAGE)))
                self.end_headers()
                self.wfile.write(PAGE)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                with server.lock:
                    server.results[self._token()] = json.loads(body or b"{}")
                    server.events.setdefault(self._token(), threading.Event()).set()
                self.send_response(204)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def read(self, binary: str, args: list[str], timeout: float = 90) -> Any:
        token = binascii.hexlify(os.urandom(8)).decode()
        with self.lock:
            event = self.events.setdefault(token, threading.Event())
        user_data_dir = tempfile.mkdtemp(prefix="idgate-page-")
        command = [binary, f"--user-data-dir={user_data_dir}", "--no-first-run", "--no-default-browser-check",
                   "--headless=new", *args]
        if _no_sandbox():
            command.append("--no-sandbox")
        command.append(f"http://127.0.0.1:{self.httpd.server_address[1]}/?token={token}")
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            return self.results.pop(token) if event.wait(timeout) else {"error": "the page posted nothing"}
        finally:
            _stop(process, user_data_dir)


def page_check(old: str, new: str, personas: list[str], count: int, jobs: int) -> dict[str, Any]:
    """Readback noise on the new binary: present, stable across launches, different across seeds."""
    server = PageServer()
    cases = [(persona, seed) for persona in personas for seed in seeds_for(f"page-{persona}", count)]

    def run(case):
        persona, seed = case
        noisy = switches(persona, seed, "app")
        plain = [a for a in noisy if a != "--fingerprint-noise"]
        return {"persona": persona, "seed": seed, "first": server.read(new, noisy), "second": server.read(new, noisy),
                "plain": server.read(new, plain), "old": server.read(old, noisy)}

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        rows = list(pool.map(run, cases))
    server.httpd.shutdown()
    problems: list[str] = []
    counts: dict[str, Any] = {}
    for surface in ("canvas", "webgl"):
        for read in ("pixels", "dataUrl"):
            key = f"{surface}.{read}"
            values = [(r, r["first"].get(surface, {}).get(read)) for r in rows]
            for row, value in values:
                label = f"{key} {row['persona']} seed {row['seed']}"
                if value is None:
                    problems.append(f"{label}: no readback ({row['first'].get(surface, row['first'])})")
                    continue
                if value != row["second"].get(surface, {}).get(read):
                    problems.append(f"{label}: differs between two launches")
                if value == row["plain"].get(surface, {}).get(read):
                    problems.append(f"{label}: same as without --fingerprint-noise")
            distinct = {v for _, v in values if v is not None}
            if len(distinct) != len(rows):
                problems.append(f"{key}: {len(distinct)} distinct values for {len(rows)} seeds")
            counts[key] = {"seeds": len(rows), "distinct": len(distinct),
                           "same_as_old": sum(1 for r, v in values if v and v == r["old"].get(surface, {}).get(read))}
    renderers = sorted({str(r["first"].get("webgl", {}).get("renderer")) for r in rows})
    return {"seeds": len(rows), "personas": personas, "counts": counts, "webgl_renderers": renderers,
            "problems": problems, "rows": rows}


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
            if path.endswith(VERSION_LISTS):
                kept = [i for i in items if not (isinstance(i, dict) and "Brand" in json.dumps(i)
                                                 and "Chrom" not in json.dumps(i))]
                if len(kept) != len(items):
                    touched.add(path + " (GREASE brand)")
                items = kept
            return items
        version_field = path in VERSION_FIELDS or any(f".{name}[]" in path for name in VERSION_LISTS)
        if version_field and isinstance(value, str) and text(value) != value:
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


TABLES = re.compile(r"\(tables ([0-9a-f]{64})\)")


def explain_lines(report: str, version: str, tables: set[str]) -> list[str]:
    """The report's lines, compared as they are except for the two that name the build.

    The chromium line must name this binary's version. The catalogue line keeps its
    catalogue version; its tables digest is collected in *tables* and replaced.
    """
    lines = []
    for line in report.splitlines():
        field = line.strip()
        if field.startswith("chromium "):
            if field.split()[-1] != version:
                lines.append(f"chromium line names {field.split()[-1]}, the binary is {version}")
            continue
        if field.startswith("catalogue "):
            tables.update(TABLES.findall(line))
            line = TABLES.sub("(tables <DIGEST>)", line)
        lines.append(line)
    return lines


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
    parser.add_argument("--page-seeds", type=int, default=0, help="seeds per page persona for the readback page check")
    parser.add_argument("--page-personas", default={"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux"),
                        help="personas for the page check; default: this host's OS")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    old_version, new_version = binary_version(args.old), binary_version(args.new)
    touched: set[str] = set()
    walk, _ = normaliser(old_version, new_version, touched)
    tables: dict[str, set[str]] = {"old": set(), "new": set()}
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
            old_lines = explain_lines(reports["old"][1], old_version, tables["old"])
            new_lines = explain_lines(reports["new"][1], new_version, tables["new"])
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
        "catalogue_tables": {side: sorted(digests) for side, digests in tables.items()},
        "seconds": round(time.time() - started),
    }
    page_problems: list[str] = []
    if args.page_seeds:
        page = page_check(args.old, args.new, args.page_personas.split(","), args.page_seeds, args.jobs)
        (args.out / "page.json").write_text(json.dumps(page, indent=1), encoding="utf-8")
        page_problems = page["problems"]
        summary["page"] = {k: v for k, v in page.items() if k != "rows"}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (args.out / "diffs.json").write_text(json.dumps(differing + failed, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 2 if failed else 1 if differing or page_problems else 0


if __name__ == "__main__":
    sys.exit(main())
