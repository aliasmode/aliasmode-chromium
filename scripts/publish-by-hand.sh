#!/usr/bin/env bash
# Stage an AliasMode Chromium release by hand and print the command that publishes it.
#
# The app installs an archive only when its top folder is
# aliasmode-chromium-<version>-<platform>/ and the archive and executable
# SHA-256 match its pins. Server builds already have that name. Nightly
# zips from build-nightly.yml (the Mac builds) have the top folder
# apostate-<version>-<platform>/, so this script repacks them: it renames
# every entry and keeps each entry's bytes, mode and symlink type. Renaming
# only the file would break the app's install.
#
# For each archive it checks the top folder, hashes the executable before and
# after a repack, and compares that hash with the MANIFEST.lock next to the
# input (Windows, Linux) and with any --expect value. Then it writes
# SHA256SUMS.txt, a manifest JSON and release notes into the staging
# directory. It publishes nothing.
#
# usage: scripts/publish-by-hand.sh --version 159.0.8154.34 --stage /tmp/acr-stage \
#          [--tag v159.0.8154.34-aliasmode.1] [--title "AliasMode Chromium ... (build 1)"] \
#          [--windows-x64 FILE] [--linux-x64 FILE] [--macos-arm64 FILE] [--macos-x64 FILE] \
#          [--expect PLATFORM=SHA256]...
# An input's upstream <input>.manifest.json, if present, adds its source
# revision and build digests to the manifest JSON.
set -euo pipefail

version="" stage="" tag="" title=""
inputs=() expects=()
while [ $# -gt 0 ]; do
  case "$1" in
    --version) version="$2"; shift 2 ;;
    --stage) stage="$2"; shift 2 ;;
    --tag) tag="$2"; shift 2 ;;
    --title) title="$2"; shift 2 ;;
    --windows-x64|--linux-x64|--macos-arm64|--macos-x64) inputs+=("${1#--}=$2"); shift 2 ;;
    --expect) expects+=("$2"); shift 2 ;;
    *) sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;$d' >&2; exit 2 ;;
  esac
done
[ -n "$version" ] && [ -n "$stage" ] && [ ${#inputs[@]} -gt 0 ] || { echo "need --version, --stage and at least one archive" >&2; exit 2; }
tag="${tag:-v$version-aliasmode.1}"
title="${title:-AliasMode Chromium $version (build ${tag##*.})}"
mkdir -p "$stage"

python3 -I - "$version" "$stage" "$tag" "$title" "${#inputs[@]}" "${inputs[@]}" "${expects[@]}" <<'PY'
import hashlib, json, os, shutil, subprocess, sys, zipfile
from pathlib import Path

version, stage, tag, title, count = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], int(sys.argv[5])
inputs = dict(a.split("=", 1) for a in sys.argv[6:6 + count])
expects = dict(a.split("=", 1) for a in sys.argv[6 + count:])
EXE = {"windows-x64": "chrome.exe", "linux-x64": "chrome", "linux-arm64": "chrome",
       "macos-arm64": "Chromium.app/Contents/MacOS/Chromium", "macos-x64": "Chromium.app/Contents/MacOS/Chromium"}
LOCK_KEY = {"windows-x64": "chrome.exe", "linux-x64": "chrome", "linux-arm64": "chrome"}


def die(message):
    sys.exit(f"publish-by-hand: {message}")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def zip_top(path):
    with zipfile.ZipFile(path) as z:
        tops = {n.split("/", 1)[0] for n in z.namelist()}
    if len(tops) != 1:
        die(f"{path}: expected one top folder, found {sorted(tops)}")
    return tops.pop()


def zip_exe_sha(path, root, exe):
    with zipfile.ZipFile(path) as z, z.open(f"{root}/{exe}") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tar_top_and_exe_sha(path, exe):
    names = subprocess.run(f"zstd -dc '{path}' | tar -t", shell=True, check=True,
                           capture_output=True, text=True).stdout.split()
    tops = {n.strip("/").split("/", 1)[0] for n in names}
    if len(tops) != 1:
        die(f"{path}: expected one top folder, found {sorted(tops)[:5]}")
    root = tops.pop()
    data = subprocess.run(f"zstd -dc '{path}' | tar -xO '{root}/{exe}'", shell=True, check=True,
                          capture_output=True).stdout
    return root, hashlib.sha256(data).hexdigest()


def repack_zip(source, target, old_root, new_root):
    """Rename the top folder; each entry keeps its bytes, mode bits, symlink type and time."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as dst:
        for info in src.infolist():
            name = new_root + info.filename[len(old_root):]
            out = zipfile.ZipInfo(name, date_time=info.date_time)
            out.external_attr, out.create_system = info.external_attr, info.create_system
            out.compress_type = zipfile.ZIP_STORED if info.is_dir() else info.compress_type
            if info.is_dir():
                dst.writestr(out, b"")
                continue
            with src.open(info) as fin, dst.open(out, "w") as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)


def lock_outputs(path):
    lock = Path(path).with_name("MANIFEST.lock")
    if not lock.is_file():
        return None, {}
    outputs, section = {}, None
    for line in lock.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[outputs]" and "=" in line:
            key, value = (p.strip().strip('"') for p in line.split("=", 1))
            outputs[key] = value
    return lock, outputs


assets = []
for platform in ("windows-x64", "linux-x64", "linux-arm64", "macos-arm64", "macos-x64"):
    if platform not in inputs:
        continue
    source = Path(inputs[platform])
    if not source.is_file():
        die(f"{source}: missing")
    ext = ".tar.zst" if source.name.endswith(".tar.zst") else ".zip"
    root = f"aliasmode-chromium-{version}-{platform}"
    name = root + ext
    target = stage / name
    exe = EXE[platform]
    if ext == ".zip":
        top = zip_top(source)
        before = zip_exe_sha(source, top, exe)
        if top == root:
            shutil.copyfile(source, target)
        elif top == f"apostate-{version}-{platform}":
            print(f"repacking {source.name}: {top}/ -> {root}/", file=sys.stderr)
            repack_zip(source, target, top, root)
            with zipfile.ZipFile(source) as a, zipfile.ZipFile(target) as b:
                old = [(i.filename[len(top):], i.external_attr, i.CRC, i.file_size) for i in a.infolist()]
                new = [(i.filename[len(root):], i.external_attr, i.CRC, i.file_size) for i in b.infolist()]
                if old != new:
                    die(f"{target}: repacked entries differ from {source}")
                if b.testzip() is not None:
                    die(f"{target}: CRC check failed")
        else:
            die(f"{source}: top folder {top!r} is neither {root!r} nor apostate-{version}-{platform}")
        after = zip_exe_sha(target, root, exe)
    else:
        top, before = tar_top_and_exe_sha(source, exe)
        if top != root:
            die(f"{source}: top folder {top!r} is not {root!r}; repack it on the build host")
        shutil.copyfile(source, target)
        after = tar_top_and_exe_sha(target, exe)[1]
    if before != after:
        die(f"{name}: executable hash changed in staging ({before} -> {after})")
    lock, outputs = lock_outputs(source)
    checked = ["input archive"]
    if LOCK_KEY.get(platform) in outputs:
        if outputs[LOCK_KEY[platform]] != after:
            die(f"{name}: {exe} is {after}, {lock} records {outputs[LOCK_KEY[platform]]}")
        checked.append("MANIFEST.lock")
    if platform in expects:
        if expects[platform] != after:
            die(f"{name}: {exe} is {after}, expected {expects[platform]}")
        checked.append("--expect")
    upstream = {}
    manifest = Path(str(source) + ".manifest.json")
    if not manifest.is_file():
        manifest = source.with_name(f"apostate-{version}-{platform}{ext}.manifest.json")
    if manifest.is_file():
        upstream = json.loads(manifest.read_text(encoding="utf-8"))
        if upstream.get("chromium_version") != version or upstream.get("platform") != platform:
            die(f"{manifest}: names {upstream.get('chromium_version')} {upstream.get('platform')}")
    assets.append({
        "name": name, "platform": platform, "size": target.stat().st_size, "sha256": sha256(target),
        "archive_root": root, "executable": exe, "executable_sha256": after,
        "executable_checked_against": checked,
        "source_revision": upstream.get("source_revision"),
        "upstream_artifact": upstream.get("artifact"), "upstream_sha256": upstream.get("sha256"),
        "build_manifest_sha256": upstream.get("build_manifest_sha256"),
        "patch_series_sha256": upstream.get("patch_series_sha256"),
        "catalogue_version": upstream.get("catalogue_version"),
        "package_version": upstream.get("package_version"),
    })

manifest_name = f"aliasmode-chromium-{version}-manifest.json"
(stage / manifest_name).write_text(json.dumps({"chromium_version": version, "release_tag": tag, "assets": assets},
                                              indent=1) + "\n", encoding="utf-8")
sums = "".join(f"{a['sha256']}  {a['name']}\n" for a in assets)
(stage / "SHA256SUMS.txt").write_text(sums, encoding="utf-8")
labels = {"windows-x64": "Windows x64", "linux-x64": "Linux x64", "linux-arm64": "Linux arm64",
          "macos-arm64": "macOS arm64 (Apple Silicon)", "macos-x64": "macOS x64 (Intel)"}
revisions = sorted({a["source_revision"][:7] for a in assets if a["source_revision"]})
notes = [f"AliasMode Chromium {version}. " + ", ".join(labels[a["platform"]] for a in assets) + ".", "",
         "Download the archive for your platform and verify it with `SHA256SUMS.txt`.", "",
         f"- Built by AliasMode from source revision {', '.join(f'`{r}`' for r in revisions) or 'unknown'}.",
         "- Executable SHA-256: " + "; ".join(f"{labels[a['platform']]} `{a['executable']}` `{a['executable_sha256']}`"
                                         for a in assets) + "."]
if any(a["platform"].startswith("macos") for a in assets):
    notes.append("- macOS is not notarized. Unpack it with `ditto -x -k` (keeps the framework symlinks) and remove "
                 "the quarantine mark with `xattr -dr com.apple.quarantine Chromium.app` before the first start.")
(stage / "NOTES.md").write_text("\n".join(notes) + "\n", encoding="utf-8")

print(f"{'asset':58} {'bytes':>10}  archive sha256 / executable sha256")
for a in assets:
    print(f"{a['name']:58} {a['size']:>10}  {a['sha256']}\n{'':70}{a['executable_sha256']}  {a['executable']}")
files = " ".join(f"'{stage / a['name']}'" for a in assets) + f" '{stage / 'SHA256SUMS.txt'}' '{stage / manifest_name}'"
print("\nNot published. To publish (needs the user's go), push the tag at the release commit, then:")
print(f"gh release create '{tag}' --repo aliasmode/aliasmode-chromium --prerelease --verify-tag"
      + f" --title '{title}' --notes-file '{stage / 'NOTES.md'}' {files}")
PY
