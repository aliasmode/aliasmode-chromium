# AliasMode Chromium

AliasMode's open-source Chromium engine, built on top of
[Apostate](https://github.com/heretic-tech/apostate).

[Source](https://github.com/aliasmode/aliasmode-chromium) ·
[Upstream documentation](https://docs.apostate.dev) · [License](LICENSE)

## Development status

This repository contains the initial source setup, not a released AliasMode
Chromium browser. It starts from upstream `v0.5.1`, with Chromium
`155.0.8059.31`. Exact source pins are in
[`settings/aliasmode-provenance.json`](settings/aliasmode-provenance.json).

The downstream changes add native display labels and fix the missing Windows
version manifest in package assembly. Browser executable names, macOS bundle
paths, fingerprint APIs, and web-facing Chrome identity remain unchanged.
Native compilation, full binary branding, and AliasMode app integration are
not done yet. Existing app profiles are unchanged.

GitHub Actions is disabled during setup. Build, browser-test, and release
workflows are manual-only; no paid build or publication runs automatically.
`build/MANIFEST.lock` remains the inherited upstream build record, not evidence
of an AliasMode build.

## Upstream packages

The commands and examples below use upstream packages and binaries, not
AliasMode releases. Package names, imports, archive names, and download URLs
remain unchanged until an owned browser build is available.

## Install

```bash
pip install apostate                  # or: npm install @heretic-tech/apostate
apostate install                      # download and verify the browser
apostate fonts install windows        # Linux and macOS hosts, for a Windows persona
```

## Launch

```python
from apostate import launch

browser = launch(fingerprint=42, fingerprint_platform="windows")
page = browser.new_page()
page.goto("https://example.com")
print(page.evaluate("navigator.platform"))   # Win32, on any host
browser.close()
```

```javascript
import { launch } from "@heretic-tech/apostate";

const browser = await launch({ fingerprint: 42, fingerprintPlatform: "windows" });
const page = await browser.newPage();
await page.goto("https://example.com");
console.log(await page.evaluate(() => navigator.platform));
await browser.close();
```

The same seed gives the same machine on every launch and host. A persistent
context keeps a machine with its cookies and logins:
`launch_persistent_context("./profiles/alice")`. `--fingerprint-explain` prints
the machine a launch would present:

```bash
apostate run -- --fingerprint=42 --fingerprint-platform=windows --fingerprint-explain
```

## AI agents

```bash
claude mcp add apostate -- npx -y @heretic-tech/apostate-mcp --platform windows
```

The MCP server gives Claude Code, Codex, Cursor and other MCP clients browser
tools on a headless Apostate browser. See
[AI agents](https://docs.apostate.dev/agents/overview).

## Upstream test results

A Windows persona on an Apple silicon Mac, Chromium 152.0.7977.83, on
2026-09-27. The IP address and location fields are hidden.

<table>
  <tr>
    <td width="50%"><img src="docs/images/proof/fpjs.webp" alt="FingerprintJS Pro playground: Chrome 152 on Windows 11, suspect score 0, every smart signal not detected"></td>
    <td width="50%"><img src="docs/images/proof/browserscan.webp" alt="BrowserScan: browser fingerprint authenticity 100%, Windows 11"><br><br><img src="docs/images/proof/deviceandbrowserinfo.webp" alt="deviceandbrowserinfo.com: You are human, isBot false"></td>
  </tr>
  <tr>
    <td colspan="2"><img src="docs/images/proof/sannysoft.webp" alt="bot.sannysoft.com: every test passed, WebGL on an NVIDIA GPU"></td>
  </tr>
</table>

FingerprintJS Pro: suspect score 0. BrowserScan: 100% authentic.
deviceandbrowserinfo.com: human. bot.sannysoft.com: every test passed.

[`tests/`](tests/) checks, on a real browser, that pages read exactly the
machine the browser composed, from every context, with no automation trace,
and records what public detectors say beside control browsers.
[Latest results](https://docs.apostate.dev/testing/results) are generated from
the dated result files in [`tests/results/`](tests/results/).
[Known gaps](https://docs.apostate.dev/known-gaps) lists what a page can still
tell.

## Upstream binaries

| Host | Archive |
| --- | --- |
| Linux x64 | `apostate-155.0.8059.31-linux-x64.tar.zst` |
| Linux arm64 | `apostate-155.0.8059.31-linux-arm64.tar.zst` |
| macOS arm64 | `apostate-155.0.8059.31-macos-arm64.zip` |
| Windows x64 | `apostate-155.0.8059.31-windows-x64.zip` |

Any host can present any persona. Windows personas look most real on an x86
host, macOS personas on a Mac. See
[Choosing a host](https://docs.apostate.dev/concepts/hosts).

## Repository

| Path | Holds |
| --- | --- |
| `patches/` | The Chromium changes, applied in the order `patches/series` lists |
| `build/`, `scripts/` | Pinned build inputs, and a script for every build, release and check step |
| `resources/`, `corpus/`, `capture/` | The catalogue, measured GPU families, captures of real machines and the capture tools |
| `python/`, `npm/`, `mcp/` | The Python package, the Node package and the MCP server |
| `docs/` | The documentation site (Mintlify) |
| `examples/` | Runnable examples: Python, Node, use cases, AI agents, Docker |
| `tests/` | The test suite and its published results |

[Contributing](https://docs.apostate.dev/contributing/overview) covers building
the browser, writing a patch and cutting a release.

## Upstream updates

Keep `origin` on this repository and add
`https://github.com/heretic-tech/apostate.git` as the `upstream` remote.
The `upstream-main` branch preserves the upstream main revision recorded at
initialization; it is not merged into the tested release baseline.

1. Fetch upstream branches and tags with `git fetch upstream --tags`.
2. Create an update branch from `origin/main` and review the chosen upstream release.
3. Merge that release, retaining the downstream branding and Windows packaging fix.
4. Update provenance, run the source checks, then build and test the native browser.
5. Review the result before updating `main` or publishing an owned release.

Upstream merges and releases are deliberate, not automatic. The source checks
in [`.github/workflows/check.yml`](.github/workflows/check.yml) do not replace
native browser testing.

## Licence

GPL-3.0. See [LICENSE](LICENSE).
