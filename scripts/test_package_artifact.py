"""Exercise Windows packaging without a Chromium build."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("7z") or shutil.which("ditto"), "requires 7z or ditto")
class WindowsPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        output = self.workspace / "src/out/windows-x64"
        output.mkdir(parents=True)
        for name in [
            "chrome.exe", "chrome.dll", "chrome_elf.dll", "icudtl.dat",
            "resources.pak", "chrome_100_percent.pak", "chrome_200_percent.pak",
            "v8_context_snapshot.bin",
        ]:
            (output / name).touch()
        (output / "locales").mkdir()
        for locale in ["en-US", "fr"]:
            (output / "locales" / f"{locale}.pak").touch()
        version = (ROOT / "build/CHROMIUM_VERSION").read_text().strip()
        self.manifest = output / f"{version}.manifest"
        self.manifest_bytes = (
            '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">\r\n'
            f'  <assemblyIdentity name="{version}" version="{version}" type="win32"/>\r\n'
            '  <file name="chrome_elf.dll"/>\r\n'
            '</assembly>\r\n'
        ).encode()
        self.manifest.write_bytes(self.manifest_bytes)
        self.archive = self.workspace / f"apostate-{version}-windows-x64.zip"

    def package(self):
        return subprocess.run(
            ["bash", str(ROOT / "scripts/package-artifact.sh"), "windows-x64", str(self.archive)],
            cwd=ROOT,
            env={**os.environ, "APOSTATE_WORKSPACE": str(self.workspace)},
            capture_output=True,
            text=True,
        )

    def test_windows_archive_includes_version_manifest_unchanged(self):
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(self.archive) as archive:
            member = f"{self.archive.stem}/{self.manifest.name}"
            self.assertIn(member, archive.namelist())
            self.assertEqual(archive.read(member), self.manifest_bytes)

    def test_windows_package_requires_version_manifest(self):
        self.manifest.unlink()
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(self.manifest.name, result.stderr)
        self.assertFalse(self.archive.exists())


@unittest.skipUnless(shutil.which("gcc") and shutil.which("strip") and shutil.which("zstd"), "requires gcc, strip and zstd")
class LinuxPackageTests(unittest.TestCase):
    def test_linux_payload_is_stripped_and_out_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            output = workspace / "src/out/linux-x64"
            (output / "locales").mkdir(parents=True)
            for name in [
                "chrome_crashpad_handler", "icudtl.dat", "resources.pak",
                "chrome_100_percent.pak", "chrome_200_percent.pak", "v8_context_snapshot.bin",
                "locales/en-US.pak", "locales/fr.pak",
            ]:
                (output / name).touch()
            source = workspace / "main.c"
            source.write_text("int apostate_marker(void) { return 1; }\nint main(void) { return apostate_marker(); }\n")
            subprocess.run(["gcc", "-o", str(output / "chrome"), str(source)], check=True)
            tools = workspace / "src/third_party/llvm-build/Release+Asserts/bin"
            tools.mkdir(parents=True)
            (tools / "llvm-strip").symlink_to(shutil.which("strip"))
            (ROOT / "build/MANIFEST.lock").exists() or self.skipTest("no build/MANIFEST.lock")
            version = (ROOT / "build/CHROMIUM_VERSION").read_text().strip()
            archive = workspace / f"apostate-{version}-linux-x64.tar.zst"
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/package-artifact.sh"), "linux-x64", str(archive)],
                cwd=ROOT,
                env={**os.environ, "APOSTATE_WORKSPACE": str(workspace)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            staged = workspace / f"apostate-{version}-linux-x64/chrome"
            self.assertNotIn(b"apostate_marker", staged.read_bytes())
            self.assertIn(b"apostate_marker", (output / "chrome").read_bytes())


if __name__ == "__main__":
    unittest.main()
