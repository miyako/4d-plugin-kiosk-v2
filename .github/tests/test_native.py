"""Exercise the actual release shell blocks without native builds or Apple tools."""

import importlib.util
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/release.yml").read_text()
SPEC = importlib.util.spec_from_file_location(
    "native_source_version", ROOT / ".github/scripts/source_version.py"
)
source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source)


def step(name):
    """Extract one literal run block, failing rather than testing a copied script."""
    matches = re.findall(
        rf"^      - name: {re.escape(name)}\n"
        r"(?:(?!      - ).*\n)*?        run: \|\n"
        r"((?:          .*(?:\n|$)|[ \t]*\n)+)",
        WORKFLOW,
        re.MULTILINE,
    )
    if len(matches) != 1:
        raise AssertionError(f"Expected one shell block for {name!r}")
    return textwrap.dedent(matches[0])


STUB = r'''
import json
import os
from pathlib import Path
import plistlib
import re
import sys

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path.cwd()
with open(os.environ["CALL_LOG"], "a") as log:
    log.write(json.dumps([tool, *args]) + "\n")
bundle = root / os.environ["TEST_BUNDLE"]
contents = bundle / "Contents"
mode = os.environ.get("BUILD_MODE", "complete")

if tool == "xcodebuild":
    for path in (
        contents / "MacOS/kiosk", contents / "Info.plist",
        contents / "Resources/English.lproj/InfoPlist.strings",
        contents / "_CodeSignature", bundle.parent / "Default",
        bundle.parent / "kiosk.build",
    ):
        assert not path.exists(), "stale output survived: " + str(path)
    if mode != "missing-binary":
        (contents / "MacOS").mkdir(parents=True, exist_ok=True)
        (contents / "MacOS/kiosk").write_bytes(b"rebuilt-universal")
    if mode != "missing-plist":
        data = plistlib.loads((root / "Info.plist").read_bytes())
        for key in ("CFBundleShortVersionString", "CFBundleVersion"):
            if key in data:
                data[key] = re.sub(
                    r"\$\([^)]+\)|\$\{[^}]+\}", os.environ["SOURCE_VERSION"], data[key]
                )
        if mode == "stale-plist":
            data["CFBundleShortVersionString"] = os.environ["STALE_VERSION"]
        if mode == "stale-build-version":
            data["CFBundleVersion"] = os.environ["STALE_VERSION"]
        (contents / "Info.plist").write_bytes(plistlib.dumps(data))
    if mode != "missing-localized":
        target = contents / "Resources/English.lproj/InfoPlist.strings"
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (root / "English.lproj/InfoPlist.strings").read_bytes()
        if mode == "stale-localized":
            data = data.replace(
                os.environ["SOURCE_VERSION"].encode(), os.environ["STALE_VERSION"].encode()
            )
        target.write_bytes(data)
elif tool == "lipo":
    assert args == ["-info", os.environ["TEST_BUNDLE"] + "/Contents/MacOS/kiosk"]
    print("Architectures in the fat file: " + os.environ.get("ARCHS", "arm64 x86_64"))
elif tool == "python3":
    assert args == [".github/scripts/source_version.py", "--verify-bundle",
                    os.environ["TEST_BUNDLE"]]
    os.execv(os.environ["REAL_PYTHON"], [os.environ["REAL_PYTHON"], *args])
elif tool in ("ditto", "hdiutil"):
    assert Path(os.environ["BUNDLE_PATH"]).is_dir()
    if tool == "ditto":
        assert args[:-1] == ["-c", "-k", "--keepParent", os.environ["BUNDLE_PATH"]]
    else:
        assert args[:-1] == ["create", "-format", "UDBZ", "-volname", "kiosk",
                            "-srcfolder", os.environ["BUNDLE_PATH"]]
    Path(args[-1]).write_bytes(b"stub archive")
elif tool == "xcrun":
    scenario = os.environ.get("NOTARY_MODE", "accepted")
    if args[:2] == ["notarytool", "submit"]:
        assert args[3:] == [
            "--keychain-profile", "notarytool-profile", "--keychain",
            os.environ["KEYCHAIN_PATH"], "--wait", "--output-format", "json"
        ]
        assert Path(args[2]).is_file()
        second = args[2].endswith(".dmg")
        if scenario == "transport" or (scenario == "second-transport" and second):
            sys.exit(71)
        if scenario == "malformed" or (scenario == "second-malformed" and second):
            print("this is not JSON")
        else:
            status = "Invalid" if (
                scenario == "invalid" or (scenario == "second-invalid" and second)
            ) else "Accepted"
            print(json.dumps({"id": "submission-dmg" if second else "submission-zip",
                              "status": status}))
    elif args[:2] == ["notarytool", "log"]:
        assert args[3:] == ["--keychain-profile", "notarytool-profile",
                            "--keychain", os.environ["KEYCHAIN_PATH"]]
        # A diagnostic failure must not mask the Invalid status.
        sys.exit(72)
    elif args[:2] == ["stapler", "staple"]:
        assert len(args) == 3 and args[2].endswith(".dmg")
        if scenario == "staple":
            sys.exit(73)
    else:
        raise AssertionError(args)
elif tool == "plutil":
    assert args[0] == "-extract" and args[2:] == ["raw", "-o", "-", "-"]
    try:
        value = json.load(sys.stdin)[args[1]]
        assert isinstance(value, str)
        print(value)
    except (ValueError, KeyError, AssertionError):
        sys.exit(74)
else:
    raise AssertionError("Unexpected tool: " + tool)
'''


class NativeShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".native-tests-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for path in (*source.SOURCE_PATHS, Path(".github/scripts/source_version.py")):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, target)
        self.version = source.read(self.root)
        major, minor, patch = map(int, self.version.split("."))
        self.stale = f"{major}.{minor}.{patch + 1}"
        self.relative_bundle = "test/Plugins/kiosk.bundle"
        self.bundle = self.root / self.relative_bundle
        self.contents = self.bundle / "Contents"
        self.log = self.root / "calls.jsonl"
        self.env_file = self.root / "github-env"
        self.env_file.touch()
        self.bin = self.root / "stub-bin"
        self.bin.mkdir()
        for name in ("xcodebuild", "lipo", "python3", "ditto", "hdiutil", "xcrun", "plutil"):
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n" + textwrap.dedent(STUB))
            executable.chmod(0o755)
        # A closed PATH prevents fallback to any installed native tool.
        for name in ("rm", "ls", "grep", "mkdir", "cp"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable)
            (self.bin / name).symlink_to(executable)
        self.env = {
            "PATH": str(self.bin),
            "CALL_LOG": str(self.log),
            "REAL_PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
            "SOURCE_VERSION": self.version,
            "STALE_VERSION": self.stale,
            "TEST_BUNDLE": self.relative_bundle,
            "BUNDLE_PATH": self.relative_bundle,
            "KEYCHAIN_PATH": str(self.root / "fake.keychain-db"),
            "GITHUB_ENV": str(self.env_file),
        }
        self.seed()

    def seed(self):
        self.preserved = {}
        for relative in (
            "QuickLook/Thumbnail.png", "Resources/manifest.json", "Resources/constants.xlf",
            "Windows/kiosk.4DX", "Windows64/kiosk.4DX",
        ):
            data = (ROOT / self.relative_bundle / "Contents" / relative).read_bytes()
            self.write(relative, data)
            self.preserved[relative] = data
        for relative in (
            "Frameworks/Dependency.framework/Versions/A/Dependency",
            "Frameworks/Dependency.framework/Versions/A/Resources/dependency.dat",
        ):
            data = b"preserved framework dependency"
            self.write(relative, data)
            self.preserved[relative] = data
        (self.contents / "Frameworks/Dependency.framework/Versions/Current").symlink_to("A")
        for relative in (
            "MacOS/kiosk", "Info.plist", "Resources/English.lproj/InfoPlist.strings",
            "_CodeSignature/CodeResources",
        ):
            self.write(relative, b"stale build output")
        for relative in ("Default/stale", "kiosk.build/stale"):
            path = self.bundle.parent / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stale intermediate")

    def write(self, relative, data):
        path = self.contents / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def run_step(self, name, **env):
        script = step(name)
        values = {
            "env.PRODUCT_NAME": "kiosk", "env.BASE_DIR": "test",
            "env.MACOS_DEPLOYMENT_TARGET": "11.0",
            "needs.version.outputs.version": self.version,
        }
        for key, value in values.items():
            script = script.replace("${{ " + key + " }}", value)
        self.assertNotIn("${{", script)
        return subprocess.run(
            ["/bin/bash", "-c", script], cwd=self.root, env={**self.env, **env},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def calls(self, tool):
        if not self.log.exists():
            return []
        return [call[1:] for line in self.log.read_text().splitlines()
                if (call := json.loads(line))[0] == tool]

    def build(self, **env):
        self.assert_ok(self.run_step("build macOS plugin (universal)", **env))

    def test_fresh_universal_build_preserves_resources_and_dependencies(self):
        self.build()
        self.assertEqual((self.contents / "MacOS/kiosk").read_bytes(), b"rebuilt-universal")
        for relative, data in self.preserved.items():
            self.assertEqual((self.contents / relative).read_bytes(), data, relative)
        link = self.contents / "Frameworks/Dependency.framework/Versions/Current"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), Path("A"))
        for path in (self.contents / "_CodeSignature", self.bundle.parent / "Default",
                     self.bundle.parent / "kiosk.build"):
            self.assertFalse(path.exists())
        original = plistlib.loads((self.root / "Info.plist").read_bytes())
        rebuilt = plistlib.loads((self.contents / "Info.plist").read_bytes())
        self.assertNotIn("CFBundleVersion", original)
        self.assertNotIn("CFBundleVersion", rebuilt)
        source.verify_bundle(self.bundle, self.root)
        self.assert_ok(self.run_step("verify universal binary"))
        self.assertEqual(len(self.calls("python3")), 1)
        self.assertEqual(len(self.calls("lipo")), 3)
        self.assertEqual(self.calls("xcodebuild"), [[
            "-project", "kiosk.xcodeproj", "-target", "kiosk",
            "-configuration", "Default", "ARCHS=arm64 x86_64", "ONLY_ACTIVE_ARCH=NO",
            "MACOSX_DEPLOYMENT_TARGET=11.0", "CODE_SIGNING_ALLOWED=NO",
            f"CONFIGURATION_BUILD_DIR={self.root}/test/Plugins", "build",
        ]])

    def test_missing_rebuilt_binary_is_rejected(self):
        self.build(BUILD_MODE="missing-binary")
        self.assertFalse((self.contents / "MacOS/kiosk").exists())
        result = self.run_step("verify universal binary")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected binary not found", result.stdout)
        self.assertEqual(self.calls("lipo"), [])
        self.assertEqual(self.calls("python3"), [])

    def test_missing_rebuilt_metadata_is_rejected(self):
        for mode, relative in (
            ("missing-plist", "Info.plist"),
            ("missing-localized", "Resources/English.lproj/InfoPlist.strings"),
        ):
            with self.subTest(mode=mode):
                self.build(BUILD_MODE=mode)
                self.assertFalse((self.contents / relative).exists())
                result = self.run_step("verify universal binary")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("FileNotFoundError", result.stderr)

    def test_stale_rebuilt_metadata_is_rejected(self):
        for mode in ("stale-plist", "stale-build-version", "stale-localized"):
            with self.subTest(mode=mode):
                self.build(BUILD_MODE=mode)
                result = self.run_step("verify universal binary")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("differs from selected source", result.stderr)

    def test_each_universal_architecture_is_required(self):
        self.build()
        for architectures in ("arm64", "x86_64", ""):
            with self.subTest(architectures=architectures):
                result = self.run_step("verify universal binary", ARCHS=architectures)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("python3"), [])

    def compiled_localization(self):
        localized = source.text(self.root / source.LOCALIZED)
        values = dict(re.findall(r'(?m)^(\w+)\s*=\s*"([^"]*)";', localized))
        self.assertEqual(values["CFBundleShortVersionString"], self.version)
        self.assertIn(self.version, values["CFBundleGetInfoString"])
        return values

    def test_compiled_binary_and_xml_plist_and_localization_are_accepted(self):
        self.build()
        values = self.compiled_localization()
        metadata = plistlib.loads((self.contents / "Info.plist").read_bytes())
        for serialization, signature in (
            (plistlib.FMT_BINARY, b"bplist00"),
            (plistlib.FMT_XML, b"<?xml"),
        ):
            with self.subTest(serialization=serialization):
                data = plistlib.dumps(values, fmt=serialization)
                self.assertTrue(data.startswith(signature))
                self.write("Resources/English.lproj/InfoPlist.strings", data)
                self.write("Info.plist", plistlib.dumps(metadata, fmt=serialization))
                self.assert_ok(self.run_step("verify universal binary"))
        self.assertEqual(len(self.calls("python3")), 2)

    def test_stale_compiled_plist_is_rejected(self):
        self.build()
        metadata = plistlib.loads((self.contents / "Info.plist").read_bytes())
        for serialization in (plistlib.FMT_BINARY, plistlib.FMT_XML):
            for key in ("CFBundleShortVersionString", "CFBundleVersion"):
                with self.subTest(serialization=serialization, key=key):
                    self.write("Info.plist", plistlib.dumps(
                        {**metadata, key: self.stale}, fmt=serialization,
                    ))
                    result = self.run_step("verify universal binary")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("differs from selected source", result.stderr)
        self.assertEqual(len(self.calls("python3")), 4)

    def test_stale_compiled_localization_is_rejected(self):
        self.build()
        values = self.compiled_localization()
        for serialization in (plistlib.FMT_BINARY, plistlib.FMT_XML):
            for key in ("CFBundleShortVersionString", "CFBundleGetInfoString"):
                with self.subTest(serialization=serialization, key=key):
                    stale = {**values, key: values[key].replace(self.version, self.stale)}
                    self.write("Resources/English.lproj/InfoPlist.strings",
                               plistlib.dumps(stale, fmt=serialization))
                    result = self.run_step("verify universal binary")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Built localized display differs from selected source",
                                  result.stderr)
        self.assertEqual(len(self.calls("python3")), 4)

    def test_exact_windows_artifact_is_merged_without_touching_resources(self):
        self.build()
        artifact = self.root / "winbuild/kiosk.4DX"
        artifact.parent.mkdir()
        artifact.write_bytes(b"fresh windows product")
        decoy = artifact.parent / "nested/kiosk.4DX"
        decoy.parent.mkdir()
        decoy.write_bytes(b"wrong recursive match")
        self.assert_ok(self.run_step("merge Windows binaries into the bundle"))
        self.assertEqual((self.contents / "Windows64/kiosk.4DX").read_bytes(),
                         b"fresh windows product")
        for relative, data in self.preserved.items():
            if relative != "Windows64/kiosk.4DX":
                self.assertEqual((self.contents / relative).read_bytes(), data, relative)
        self.assertEqual(self.env_file.read_text(),
                         f"BUNDLE_PATH={self.relative_bundle}\n")

    def test_missing_or_empty_exact_windows_artifact_blocks_merge(self):
        decoy = self.root / "winbuild/nested/kiosk.4DX"
        decoy.parent.mkdir(parents=True)
        decoy.write_bytes(b"wrong recursive match")
        for empty in (False, True):
            with self.subTest(empty=empty):
                if empty:
                    (self.root / "winbuild/kiosk.4DX").touch()
                result = self.run_step("merge Windows binaries into the bundle")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Windows binary missing at exact artifact path", result.stdout)
                self.assertEqual(self.env_file.read_text(), "")
                self.assertEqual((self.contents / "Windows64/kiosk.4DX").read_bytes(),
                                 self.preserved["Windows64/kiosk.4DX"])

    def assert_package_calls(self, submissions, staple=False, diagnostic=None):
        zip_path = f"kiosk-{self.version}.zip"
        dmg_path = f"kiosk-{self.version}.dmg"
        self.assertEqual(len(self.calls("ditto")), 1)
        self.assertEqual(len(self.calls("hdiutil")), 1)
        calls = self.calls("xcrun")
        submitted = [call[2] for call in calls if call[:2] == ["notarytool", "submit"]]
        self.assertEqual(submitted, [zip_path, dmg_path][:submissions])
        self.assertEqual(
            [call for call in calls if call[:2] == ["stapler", "staple"]],
            [["stapler", "staple", dmg_path]] if staple else [],
        )
        logs = [call[2] for call in calls if call[:2] == ["notarytool", "log"]]
        self.assertEqual(logs, [diagnostic] if diagnostic else [])
        if staple:
            self.assertEqual(calls[-1], ["stapler", "staple", dmg_path])

    def test_both_accepted_submissions_then_staple_publish_paths(self):
        self.assert_ok(self.run_step("package, notarize & staple"))
        self.assert_package_calls(2, staple=True)
        self.assertEqual(self.env_file.read_text().splitlines(), [
            f"ZIP_PATH=kiosk-{self.version}.zip",
            f"DMG_PATH=kiosk-{self.version}.dmg",
        ])

    def test_invalid_zero_exit_status_blocks_staple_and_publication(self):
        result = self.run_step("package, notarize & staple", NOTARY_MODE="invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("returned 'Invalid'", result.stdout)
        self.assert_package_calls(1, diagnostic="submission-zip")
        self.assertEqual(self.env_file.read_text(), "")

    def test_transport_failure_blocks_staple_and_publication(self):
        result = self.run_step("package, notarize & staple", NOTARY_MODE="transport")
        self.assertNotEqual(result.returncode, 0)
        self.assert_package_calls(1)
        self.assertEqual(self.env_file.read_text(), "")

    def test_malformed_json_blocks_staple_and_publication(self):
        result = self.run_step("package, notarize & staple", NOTARY_MODE="malformed")
        self.assertNotEqual(result.returncode, 0)
        self.assert_package_calls(1)
        self.assertTrue(self.calls("plutil"))
        self.assertEqual(self.env_file.read_text(), "")

    def test_second_submission_failure_blocks_staple_and_publication(self):
        for scenario, diagnostic in (
            ("second-transport", None), ("second-invalid", "submission-dmg"),
            ("second-malformed", None),
        ):
            with self.subTest(scenario=scenario):
                self.log.unlink(missing_ok=True)
                result = self.run_step("package, notarize & staple", NOTARY_MODE=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assert_package_calls(2, diagnostic=diagnostic)
                self.assertEqual(self.env_file.read_text(), "")

    def test_staple_failure_blocks_publication(self):
        result = self.run_step("package, notarize & staple", NOTARY_MODE="staple")
        self.assertNotEqual(result.returncode, 0)
        self.assert_package_calls(2, staple=True)
        self.assertEqual(self.env_file.read_text(), "")


class NativeConfigurationTests(unittest.TestCase):
    def test_windows_exact_output_matches_release_x64_project(self):
        project = ET.parse(ROOT / "kiosk.vcxproj")
        namespace = {"m": "http://schemas.microsoft.com/developer/msbuild/2003"}
        condition = "'$(Configuration)|$(Platform)'=='Release|x64'"
        outdirs = [node.text for node in project.findall(".//m:OutDir", namespace)
                   if node.get("Condition") == condition]
        extensions = [node.text for node in project.findall(".//m:TargetExt", namespace)
                      if node.get("Condition") == condition]
        self.assertEqual(len(outdirs), 1)
        self.assertEqual(extensions, [".4DX"])
        actual = (outdirs[0].replace("$(ProjectDir)", "")
                  .replace("$(ProjectName)", "kiosk").replace("\\", "/")
                  + "kiosk" + extensions[0])
        normalize = step("normalize windows output to canonical case")
        match = re.search(r'^\$source = "([^"]+)"$', normalize, re.MULTILINE)
        self.assertIsNotNone(match)
        expected = match[1].replace("$base", "test").replace("$product", "kiosk")
        self.assertEqual(actual, expected)
        self.assertIn("Test-Path -LiteralPath $source -PathType Leaf", normalize)
        self.assertIn("throw ", normalize)
        self.assertNotIn("-Recurse", normalize)
        self.assertIn("$sourceResolved -ieq $destResolved", normalize)
        self.assertIn("Copy-Item -LiteralPath $source $destination -Force", normalize)

    def test_windows_fresh_invalidation_and_original_platform_settings(self):
        clean = step("clean stale outputs")
        removals = re.findall(r"^Remove-Item (.+)$", clean, re.MULTILINE)
        self.assertEqual(removals, [
            '-Force -ErrorAction SilentlyContinue "$base/Plugins/$product.bundle/Contents/Windows64/$product.4DX"',
            '-Recurse -Force -ErrorAction SilentlyContinue "$base/Plugins/Default"',
            '-Recurse -Force -ErrorAction SilentlyContinue "Objs"',
        ])
        self.assertLess(WORKFLOW.index("- name: clean stale outputs"),
                        WORKFLOW.index("- name: build x64"))
        self.assertIn("/p:Configuration=Release /p:Platform=x64", step("build x64"))
        self.assertIn("/p:PlatformToolset=v143 /p:WindowsTargetPlatformVersion=10.0",
                      step("build x64"))
        project = ET.parse(ROOT / "kiosk.vcxproj")
        ns = {"m": "http://schemas.microsoft.com/developer/msbuild/2003"}
        self.assertEqual(
            {node.get("Include") for node in project.findall(".//m:ProjectConfiguration", ns)},
            {"Debug|Win32", "Debug|x64", "Release|Win32", "Release|x64"},
        )
        self.assertEqual({node.text for node in project.findall(".//m:PlatformToolset", ns)},
                         {"v140"})

    def test_windows_artifact_uses_exact_canonical_product_path(self):
        upload = re.search(
            r"      - name: upload windows binaries\n(.*?)(?=\n  build-macos:)",
            WORKFLOW, re.DOTALL,
        )
        self.assertIsNotNone(upload)
        paths = re.findall(r"^          path: (.+)$", upload[1], re.MULTILINE)
        self.assertEqual(paths, [
            "${{ env.BASE_DIR }}/Plugins/${{ env.PRODUCT_NAME }}.bundle/"
            "Contents/Windows64/${{ env.PRODUCT_NAME }}.4DX",
        ])
        self.assertIn("if-no-files-found: error", upload[1])
        merge = step("merge Windows binaries into the bundle")
        self.assertIn('SRC="winbuild/${PRODUCT}.4DX"', merge)
        self.assertIn('if [ ! -s "$SRC" ]; then', merge)
        self.assertNotIn("find ", merge)

    def test_original_macos_settings_and_release_secrets(self):
        project = (ROOT / source.PROJECT).read_text()
        for setting, value, count in (
            ("SDKROOT", "macosx", 6),
            ("MACOSX_DEPLOYMENT_TARGET", "11.5", 3),
            ("MACOSX_DEPLOYMENT_TARGET", "10.8", 3),
            ("CLANG_CXX_LIBRARY", '"libc++"', 6),
            ("INFOPLIST_FILE", "Info.plist", 3),
            ("WRAPPER_EXTENSION", "bundle", 3),
        ):
            self.assertEqual(len(re.findall(
                rf"(?m)^\s*{setting} = {re.escape(value)};", project
            )), count)
        for name in ("Default", "Development", "Deployment"):
            self.assertEqual(len(re.findall(rf"(?m)^\s*name = {name};", project)), 2)
        self.assertIn("English.lproj/InfoPlist.strings", project)
        for resource in ("manifest.json in CopyFiles", "constants.xlf in CopyFiles",
                         "Cocoa.framework in Frameworks", "InfoPlist.strings in Resources"):
            self.assertIn(resource, project)
        self.assertEqual(set(re.findall(r"\$\{\{ secrets\.([A-Z_]+) \}\}", WORKFLOW)), {
            "APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
            "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_TEAM_ID",
            "NOTARYTOOL_PASSWORD",
        })
        self.assertIn("runs-on: windows-latest", WORKFLOW)
        self.assertIn("runs-on: macos-latest", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
