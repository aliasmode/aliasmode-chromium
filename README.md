# AliasMode Chromium

AliasMode's open-source Chromium engine for antidetect browsing, built on top of
[Apostate](https://github.com/heretic-tech/apostate).

[Releases](https://github.com/aliasmode/aliasmode-chromium/releases) ·
[AliasMode](https://aliasmode.com) · [License](LICENSE)

## What it is

A patched Chromium 155 that composes a complete, consistent machine from a
fingerprint seed: platform, GPU and WebGL, screen, CPU and memory, fonts,
media devices, locale and timezone. The same seed gives the same machine on
every launch, and pages read that machine from every context with no
automation trace. Web-facing identity is Chrome's.

The AliasMode desktop app uses it for the **Chromium 155 beta** option when
you create a Chrome profile.

## Status

| Platform | Build |
| --- | --- |
| Windows x64 | [`v155.0.8059.85-aliasmode.1`](https://github.com/aliasmode/aliasmode-chromium/releases/tag/v155.0.8059.85-aliasmode.1), built by AliasMode from this repository |
| macOS arm64 | Not built by AliasMode yet |
| Linux | Not built by AliasMode yet |

Each release names its source revision, and
[`build/MANIFEST.lock`](build/MANIFEST.lock) records the Chromium commit,
patch series, toolchain and output hashes of the latest build.

## Launch switches

```text
chrome.exe --user-data-dir=PROFILE_DIR --fingerprint=42 --fingerprint-platform=windows
```

| Switch | Effect |
| --- | --- |
| `--fingerprint=SEED` | Composes the machine from the seed |
| `--fingerprint-platform=windows\|macos\|linux` | Persona platform |
| `--fingerprint-timezone`, `--fingerprint-locale` | Clock and language, usually matched to the proxy |
| `--fingerprint-screen-width`, `--fingerprint-screen-height` | Screen size |
| `--fingerprint-explain` | Prints the machine a launch would present |

The full list is in [`docs/reference/switches.mdx`](docs/reference/switches.mdx).

## Building

`scripts/` has one script per step: `bootstrap.sh`, `fetch-sources.sh`,
`apply-patches.sh`, `configure.sh`, `build.sh`, `smoke-binary.sh` and
`package-artifact.sh`. A Windows build needs Visual Studio 2022 Build Tools
(C++ tools and ATL), about 70 GB of free disk and several hours on a large
machine. `scripts/provision-windows-toolchain.sh` installs the pinned Windows
SDK.

| Path | Holds |
| --- | --- |
| `patches/` | The Chromium changes, applied in the order `patches/series` lists |
| `build/`, `scripts/` | Pinned build inputs and the build, release and check scripts |
| `resources/`, `corpus/`, `capture/` | The machine catalogue, measured GPU families and capture tools |
| `tests/` | The browser test suite |
| `docs/` | Reference documentation |

## Upstream updates

The project linked above is the `upstream` remote. Upstream releases are
merged deliberately on an update branch, keeping AliasMode's branding and
packaging changes, then built, tested and reviewed before a new release.

## License

GPL-3.0. See [LICENSE](LICENSE).
