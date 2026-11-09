import importlib.util
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("source_version", ROOT / ".github/scripts/source_version.py")
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)
BASELINE = "467d31de48a9567d54285784b611e5f0ac5c753b"


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".source-tests-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.copy_source()
        self.current = source.read(self.root)
        parts = list(map(int, self.current.split(".")))
        parts[2] += 1
        self.drift = ".".join(map(str, parts))

    def copy_source(self):
        for name in source.SOURCE_PATHS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, path)

    def plist(self, **values):
        path = self.root / source.PLIST
        data = plistlib.loads(path.read_bytes())
        data.update(values)
        path.write_bytes(plistlib.dumps(data))

    def snapshot(self):
        return {name: ((self.root / name).read_bytes(),
                       stat.S_IMODE((self.root / name).stat().st_mode))
                for name in source.SOURCE_PATHS if (self.root / name).exists()}

    def assert_rejected(self, error=ValueError):
        before = self.snapshot()
        with self.assertRaises(error):
            source.read(self.root)
        with self.assertRaises(error):
            source.bump("patch", self.root)
        self.assertEqual(self.snapshot(), before)

    def assert_bumped_bytes(self, before, old, new):
        for name, (data, permissions) in before.items():
            self.assertEqual(self.snapshot()[name],
                             (data.replace(old.encode(), new.encode()), permissions))
        self.assertEqual(source.read(self.root), new)

    def test_live_and_stable_all_modes(self):
        self.assertEqual(source.PROJECT, Path("kiosk.xcodeproj/project.pbxproj"))
        self.assertEqual(source.SOURCE_PATHS, (
            source.PROJECT, Path("Info.plist"), Path("English.lproj/InfoPlist.strings")))
        self.assertEqual(source.SETTING_COUNT, 6)
        for stable in (False, True):
            for mode, index in (("major", 0), ("minor", 1), ("patch", 2)):
                with self.subTest(stable=stable, mode=mode):
                    self.copy_source()
                    old = self.current
                    if stable:
                        old = "3.2.1"
                        (self.root / source.PROJECT).write_text(
                            ("MARKETING_VERSION = 3.2.1;\nCURRENT_PROJECT_VERSION = 3.2.1;\n") * 6)
                        self.plist(CFBundleShortVersionString=old, CFBundleVersion=old)
                        (self.root / source.LOCALIZED).write_text(
                            'CFBundleName = "kiosk";\nCFBundleShortVersionString = "3.2.1";\n'
                            'CFBundleGetInfoString = "kiosk version 3.2.1, Copyright 2022 miyako.";\n')
                    before = self.snapshot()
                    parts = list(map(int, old.split(".")))
                    parts[index] += 1
                    parts[index + 1:] = [0] * (2 - index)
                    expected = ".".join(map(str, parts))
                    self.assertEqual(source.bump(mode, self.root), expected)
                    self.assert_bumped_bytes(before, old, expected)
                    if not stable:
                        self.assertNotIn("CFBundleVersion",
                                         plistlib.loads((self.root / source.PLIST).read_bytes()))

    def test_actual_project_and_target_overrides(self):
        project = source.text(self.root / source.PROJECT)
        self.assertEqual(project.count("INFOPLIST_FILE = Info.plist;"), 3)
        self.assertIn("InfoPlist.strings in Resources", project)
        for key in source.KEYS:
            self.assertEqual(len(re.findall(rf"(?m)^[ \t]*{key} =", project)), 6)
            for count in (1, 3, 6):
                for bad in ("1.2", "7", "01.2.3", "v1.2.3", "1.2.3-beta", "",
                            self.drift, "$(UNKNOWN)", "${UNKNOWN}",
                            "$(CURRENT_PROJECT_VERSION)", "${CURRENT_PROJECT_VERSION}"):
                    with self.subTest(key=key, count=count, bad=bad):
                        self.copy_source()
                        (self.root / source.PROJECT).write_text(project.replace(
                            f"{key} = {self.current};", f"{key} = {bad};", count))
                        self.assert_rejected()

    def test_missing_extra_quoted_and_malformed_settings(self):
        path = self.root / source.PROJECT
        original = source.text(path)
        for key in source.KEYS:
            for extra in (f"{key} = {self.current};", f'"{key}" = "{self.current}";',
                          f"{key} = {self.current}", f"{key} nonsense"):
                path.write_text(original + "\n" + extra + "\n")
                self.assert_rejected()
            path.write_text(original.replace(f"{key} = {self.current};", "", 1))
            self.assert_rejected()
            path.write_text(original.replace(f"{key} =", f'"{key}" =', 1))
            self.assert_rejected()
            for bad in (f"{self.current}; junk", f"{self.current}; {key} = {self.current};",
                        f'"{self.current}', f'{self.current}"'):
                path.write_text(original.replace(f"{key} = {self.current};",
                                                 f"{key} = {bad};", 1))
                self.assert_rejected()

    def test_plist_invalid_missing_duplicate_and_serialization(self):
        for key in ("CFBundleShortVersionString", "CFBundleVersion"):
            for bad in ("", "7", "1.2", "01.2.3", "v1.2.3", "1.2.3-beta",
                        self.drift, "$(UNKNOWN)", "${UNKNOWN}", "$(PRODUCT_NAME)",
                        123, True, [], {}):
                with self.subTest(key=key, bad=bad):
                    self.copy_source()
                    self.plist(**{key: bad})
                    self.assert_rejected()
        for key in ("CFBundleShortVersionString", "CFBundleVersion", "CFBundleExecutable"):
            for value in (self.current, self.drift):
                self.copy_source()
                self.plist(**{key: self.current})
                path = self.root / source.PLIST
                path.write_text(source.text(path).replace(
                    "</dict>", f"<key>{key}</key><string>{value}</string>\n</dict>"))
                self.assert_rejected()
        self.copy_source()
        path = self.root / source.PLIST
        original = path.read_bytes()
        data = plistlib.loads(original)
        del data["CFBundleShortVersionString"]
        for bad in (plistlib.dumps(data), plistlib.dumps([]), b"not a plist", b"",
                    original.replace(b"</dict>", b""),
                    original.replace(self.current.encode(),
                                     f"&#{ord(self.current[0])};{self.current[1:]}".encode()),
                    plistlib.dumps(plistlib.loads(original), fmt=plistlib.FMT_BINARY)):
            path.write_bytes(bad)
            self.assert_rejected()

    def test_references_and_optional_absence_preserved(self):
        for reference in source.REFERENCES:
            for marketing in ("$(MARKETING_VERSION)", "${MARKETING_VERSION}"):
                for quoted in (False, True):
                    with self.subTest(reference=reference, marketing=marketing, quoted=quoted):
                        self.copy_source()
                        self.plist(CFBundleShortVersionString=reference, CFBundleVersion=reference)
                        path = self.root / source.PROJECT
                        value = f'"{marketing}"' if quoted else marketing
                        path.write_text(source.SETTING.sub(
                            lambda m: f'{m[1]}{value}{m[4]}'
                            if m[2] == "CURRENT_PROJECT_VERSION" else m[0], source.text(path)))
                        self.assertEqual(source.read(self.root), self.current)
                        before = self.snapshot()
                        new = source.bump("patch", self.root)
                        self.assert_bumped_bytes(before, self.current, new)
        for reference in source.REFERENCES:
            self.copy_source()
            self.plist(CFBundleShortVersionString=reference, CFBundleVersion=reference)
            before = self.snapshot()
            new = source.bump("minor", self.root)
            self.assert_bumped_bytes(before, self.current, new)
            self.copy_source()
            path = self.root / source.PROJECT
            path.write_text(source.text(path).replace(
                f"MARKETING_VERSION = {self.current};", f'MARKETING_VERSION = "{reference}";'))
            self.assert_rejected()
        self.copy_source()
        self.plist(CFBundleShortVersionString="$(CURRENT_PROJECT_VERSION)")
        path = self.root / source.PROJECT
        for build in ("7", self.drift, "$(CURRENT_PROJECT_VERSION)", "${UNKNOWN}"):
            self.copy_source()
            self.plist(CFBundleShortVersionString="$(CURRENT_PROJECT_VERSION)")
            path.write_text(source.text(path).replace(
                f"CURRENT_PROJECT_VERSION = {self.current};", f"CURRENT_PROJECT_VERSION = {build};"))
            self.assert_rejected()

    def test_mixed_current_literals_and_references_resolve_and_co_bump(self):
        self.plist(CFBundleShortVersionString="${CURRENT_PROJECT_VERSION}",
                   CFBundleVersion="$(MARKETING_VERSION)")
        path = self.root / source.PROJECT
        path.write_text(source.text(path).replace(
            f"CURRENT_PROJECT_VERSION = {self.current};",
            'CURRENT_PROJECT_VERSION = "$(MARKETING_VERSION)";', 3))
        before = self.snapshot()
        new = source.bump("patch", self.root)
        self.assert_bumped_bytes(before, self.current, new)

    def test_localized_validation_quoted_and_unquoted_duplicates(self):
        path = self.root / source.LOCALIZED
        original = source.text(path)
        for pattern in (source.LOCALIZED_SHORT, source.LOCALIZED_INFO):
            for bad in ("", "1.0", "01.2.3", self.drift, "$(MARKETING_VERSION)",
                        "${CURRENT_PROJECT_VERSION}", "invalid"):
                path.write_text(pattern.sub(lambda m: f"{m[1]}{bad}{m[3]}", original))
                self.assert_rejected()
            path.write_text(pattern.sub("", original))
            self.assert_rejected()
        for key in ("CFBundleShortVersionString", "CFBundleGetInfoString"):
            for quoted in (False, True):
                name = f'"{key}"' if quoted else key
                for value in (self.current, self.drift):
                    path.write_text(original + f'\n{name} = "{value}";\n')
                    self.assert_rejected()
            path.write_text(original.replace(key, "UnknownKey"))
            self.assert_rejected()
            path.write_text(original + f"\n{key} nonsense\n")
            self.assert_rejected()
        for old, new in (("kiosk", "Other"), ("Copyright 2022", "Copyright 2024"),
                         ("miyako", "someone")):
            path.write_text(original.replace(old, new))
            self.assert_rejected()

    def test_exact_bytes_endings_bom_quotes_and_modes(self):
        for ending in ("\n", "\r", "\r\n", "mixed"):
            for bom in (b"", b"\xef\xbb\xbf"):
                for quoted in (False, True):
                    for final_newline in (False, True):
                        with self.subTest(ending=ending, bom=bom, quoted=quoted,
                                          final_newline=final_newline):
                            self.copy_source()
                            for i, name in enumerate(source.SOURCE_PATHS):
                                path = self.root / name
                                value = source.text(path)
                                if name == source.PROJECT:
                                    for key in source.KEYS:
                                        literal = f'"{self.current}"' if quoted else self.current
                                        value = value.replace(f"{key} = {self.current};",
                                                              f"{key} \t= \t{literal} \t; \t")
                                if name == source.LOCALIZED:
                                    value = value.replace(" = ", " \t= \t").replace('";', '" \t; \t')
                                value = value.rstrip("\n") + ("\n" if final_newline else "")
                                value = "".join(
                                    line.replace("\n", ("\n", "\r", "\r\n")[j % 3]
                                                 if ending == "mixed" else ending)
                                    for j, line in enumerate(value.splitlines(keepends=True)))
                                path.write_bytes(bom + value.encode())
                                path.chmod((0o755, 0o640, 0o644)[i])
                            before = self.snapshot()
                            new = source.bump("minor", self.root)
                            self.assert_bumped_bytes(before, self.current, new)

    def test_unsupported_encodings_fail_before_writes(self):
        for name in source.SOURCE_PATHS:
            for encoding in ("utf-16", "utf-32"):
                self.copy_source()
                path = self.root / name
                path.write_bytes(source.text(path).encode(encoding))
                self.assert_rejected()

    def test_prebuilt_4d_schema_dependencies_and_counters_excluded(self):
        excluded = (
            "test/Plugins/kiosk.bundle/Contents/Info.plist",
            "test/Plugins/kiosk.bundle/Contents/Resources/English.lproj/InfoPlist.strings",
            "test/Plugins/kiosk.bundle/Contents/Resources/manifest.json",
            "test/Plugins/kiosk.bundle/Contents/Resources/constants.xlf",
            "test/Plugins/kiosk.bundle/Contents/MacOS/kiosk",
            "test/Plugins/kiosk.bundle/Contents/Windows64/kiosk.4DX",
            "test/Data/data.4DD", "test/Project/Sources/catalog.4DCatalog",
            "test/Project/Sources/settings.4DSettings", "test/Project/test.4DProject",
            "manifest.json", "constants.json", "constants.xlf",
            "4D Plugin API/4DPluginAPI.h", "4D Plugin API/Classes/C_TYPES.h",
            "4DPlugin-kiosk.h", "4DPlugin-kiosk.cpp", "kiosk.vcxproj",
            "kiosk.vcxproj.filters", "kiosk.sln", "kiosk.dmg", "kiosk.zip",
            "a/libjsoncpp.a", "lib/windows/jsoncpp.lib", "lib/windows64/jsoncpp.lib",
            "include/json/version.h",
        )
        for name in excluded:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, path)
        before = {name: ((self.root / name).read_bytes(), (self.root / name).stat().st_mode)
                  for name in excluded}
        source.bump("major", self.root)
        self.assertEqual(before, {name: ((self.root / name).read_bytes(),
                                        (self.root / name).stat().st_mode) for name in excluded})

    def test_equal_nonversion_literals_are_not_rewritten(self):
        project = self.root / source.PROJECT
        project.write_text(source.text(project) + f"\n// keep {self.current}\n")
        self.plist(UnrelatedVersion=self.current, CFBundleVersion=self.current)
        localized = self.root / source.LOCALIZED
        localized.write_text(source.text(localized) + f'\nUnrelatedVersion = "{self.current}";\n')
        before = self.snapshot()
        new = source.bump("patch", self.root)
        for name, (original, permissions) in before.items():
            if name == source.PROJECT:
                pattern = rb'((?:MARKETING_VERSION|CURRENT_PROJECT_VERSION) = )'
            elif name == source.PLIST:
                pattern = rb'(<key>CFBundle(?:ShortVersionString|Version)</key>\s*<string>)'
            else:
                pattern = rb'(CFBundleShortVersionString = "|kiosk version )'
            expected = re.sub(pattern + re.escape(self.current.encode()),
                              lambda m: m[1] + new.encode(), original)
            self.assertEqual(self.snapshot()[name], (expected, permissions))

    def test_original_authority_and_intake_repair_only_version_bytes(self):
        original_workflow = subprocess.check_output(
            ["git", "show", "fd41655:.github/workflows/bump-version.yml"], cwd=ROOT, text=True)
        header = original_workflow.split("name:")[0]
        self.assertIn("MARKETING_VERSION/CURRENT_PROJECT_VERSION AND", header)
        self.assertIn("CFBundleShortVersionString/CFBundleVersion", header)
        for name in source.SOURCE_PATHS:
            original = subprocess.check_output(["git", "show", f"{BASELINE}:{name}"], cwd=ROOT)
            version = self.current.encode()
            if name == source.PROJECT:
                self.assertEqual(original.count(b"CURRENT_PROJECT_VERSION = 1.0.0;"), 6)
                self.assertEqual(original.count(b"MARKETING_VERSION = 1.0.3;"), 6)
                expected = original.replace(b"CURRENT_PROJECT_VERSION = 1.0.0;",
                                            b"CURRENT_PROJECT_VERSION = " + version + b";")
                expected = expected.replace(b"MARKETING_VERSION = 1.0.3;",
                                            b"MARKETING_VERSION = " + version + b";")
            elif name == source.LOCALIZED:
                self.assertNotIn(b"\n", original)
                self.assertIn(b"Copyright 2022 miyako.", original)
                expected = original.replace(b'"1.0.0"', b'"' + version + b'"')
                expected = expected.replace(b"version 1.0.0,", b"version " + version + b",")
            else:
                expected = original.replace(b"<string>1.0.3</string>",
                                            b"<string>" + version + b"</string>")
            self.assertEqual((ROOT / name).read_bytes(), expected)
            mode = subprocess.check_output(["git", "ls-tree", BASELINE, "--", str(name)],
                                           cwd=ROOT, text=True).split()[0]
            self.assertEqual(stat.S_IMODE((ROOT / name).stat().st_mode), int(mode, 8) & 0o777)

    def test_missing_files_invalid_mode_cli_read_and_bumps(self):
        for name in source.SOURCE_PATHS:
            self.copy_source()
            (self.root / name).unlink()
            self.assert_rejected(FileNotFoundError)
        self.copy_source()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            source.bump("other", self.root)
        script = str(ROOT / ".github/scripts/source_version.py")
        output = subprocess.check_output([sys.executable, script], cwd=self.root, text=True)
        self.assertEqual(output.strip(), self.current)
        self.assertEqual(self.snapshot(), before)
        for mode in ("patch", "minor", "major"):
            self.copy_source()
            before = self.snapshot()
            output = subprocess.check_output([sys.executable, script, "--bump", mode],
                                             cwd=self.root, text=True).strip()
            self.assert_bumped_bytes(before, self.current, output)
        before = self.snapshot()
        result = subprocess.run([sys.executable, script, "--bump", "other"],
                                cwd=self.root, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def bundle(self):
        contents = self.root / "test/Plugins/kiosk.bundle/Contents"
        localized = contents / "Resources/English.lproj/InfoPlist.strings"
        localized.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.root / source.PLIST, contents / "Info.plist")
        shutil.copy2(self.root / source.LOCALIZED, localized)
        return contents.parent, contents / "Info.plist", localized

    def localized_values(self):
        return {
            "CFBundleName": "kiosk",
            "CFBundleShortVersionString": self.current,
            "CFBundleGetInfoString": f"kiosk version {self.current}, Copyright 2022 miyako.",
        }

    def test_verify_bundle_text_binary_and_xml_metadata_without_writes(self):
        bundle, plist, localized = self.bundle()
        before = self.snapshot()
        source.verify_bundle(bundle, self.root)
        for info_format in (plistlib.FMT_XML, plistlib.FMT_BINARY):
            for strings_format in (plistlib.FMT_XML, plistlib.FMT_BINARY):
                with self.subTest(info_format=info_format, strings_format=strings_format):
                    plist.write_bytes(plistlib.dumps(
                        {"CFBundleShortVersionString": self.current}, fmt=info_format))
                    localized.write_bytes(plistlib.dumps(
                        self.localized_values(), fmt=strings_format))
                    built_before = (plist.read_bytes(), localized.read_bytes())
                    source.verify_bundle(bundle, self.root)
                    self.assertEqual((plist.read_bytes(), localized.read_bytes()), built_before)
        self.assertEqual(self.snapshot(), before)

    def test_verify_bundle_optional_build_missing_stale_and_references(self):
        bundle, plist, _ = self.bundle()
        for source_build in (None, self.current, "$(CURRENT_PROJECT_VERSION)"):
            for compiled_build in (None, self.current, self.drift,
                                   "$(CURRENT_PROJECT_VERSION)", 123):
                with self.subTest(source_build=source_build, compiled_build=compiled_build):
                    self.copy_source()
                    if source_build is not None:
                        self.plist(CFBundleVersion=source_build)
                    compiled = {"CFBundleShortVersionString": self.current}
                    if compiled_build is not None:
                        compiled["CFBundleVersion"] = compiled_build
                    plist.write_bytes(plistlib.dumps(compiled))
                    before = self.snapshot()
                    if compiled_build == self.current or (
                            source_build is None and compiled_build is None):
                        source.verify_bundle(bundle, self.root)
                    else:
                        with self.assertRaisesRegex(ValueError, "build version"):
                            source.verify_bundle(bundle, self.root)
                    self.assertEqual(self.snapshot(), before)

    def test_verify_bundle_rejects_stale_missing_malformed_compiled_metadata(self):
        bundle, plist, localized = self.bundle()
        for data in ([], {}, {"CFBundleShortVersionString": self.drift},
                     {"CFBundleShortVersionString": "$(MARKETING_VERSION)"}):
            plist.write_bytes(plistlib.dumps(data))
            with self.assertRaisesRegex(ValueError, "display version"):
                source.verify_bundle(bundle, self.root)
        for malformed in (b"not a plist", b"bplist00invalid", b"<?xml invalid"):
            plist.write_bytes(malformed)
            with self.assertRaises(ValueError):
                source.verify_bundle(bundle, self.root)
        self.bundle()
        for fmt in (plistlib.FMT_XML, plistlib.FMT_BINARY):
            for key in ("CFBundleShortVersionString", "CFBundleGetInfoString"):
                for bad in (None, self.drift, "$(MARKETING_VERSION)", 123):
                    values = self.localized_values()
                    if bad is None:
                        values.pop(key)
                    else:
                        values[key] = bad
                    localized.write_bytes(plistlib.dumps(values, fmt=fmt))
                    with self.assertRaisesRegex(ValueError, "localized display"):
                        source.verify_bundle(bundle, self.root)
            localized.write_bytes(plistlib.dumps([], fmt=fmt))
            with self.assertRaises(ValueError):
                source.verify_bundle(bundle, self.root)
        for malformed in (b"bplist00invalid", b"<?xml invalid"):
            localized.write_bytes(malformed)
            with self.assertRaises(ValueError):
                source.verify_bundle(bundle, self.root)
        for name in (plist, localized):
            self.bundle()
            name.unlink()
            with self.assertRaises(FileNotFoundError):
                source.verify_bundle(bundle, self.root)

    def test_verify_bundle_rejects_text_and_xml_localization_duplicates(self):
        bundle, plist, localized = self.bundle()
        original = localized.read_bytes()
        for key in ("CFBundleShortVersionString", "CFBundleGetInfoString"):
            for quoted in (False, True):
                name = f'"{key}"' if quoted else key
                localized.write_bytes(original + f'\r{name} = "{self.current}";\r'.encode())
                with self.assertRaises(ValueError):
                    source.verify_bundle(bundle, self.root)
        self.bundle()
        for target, key in ((plist, "CFBundleShortVersionString"),
                            (localized, "CFBundleGetInfoString")):
            self.bundle()
            if target == localized:
                target.write_bytes(plistlib.dumps(self.localized_values()))
            target.write_bytes(target.read_bytes().replace(
                b"</dict>", f"<key>{key}</key><string>{self.current}</string></dict>".encode()))
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                source.verify_bundle(bundle, self.root)
        self.bundle()
        localized.write_bytes(original.replace(self.current.encode(), self.drift.encode()))
        with self.assertRaises(ValueError):
            source.verify_bundle(bundle, self.root)

    def test_verify_bundle_cli_matches_native_workflow_and_is_read_only(self):
        _, plist, localized = self.bundle()
        localized.write_bytes(plistlib.dumps(self.localized_values(), fmt=plistlib.FMT_BINARY))
        before = self.snapshot()
        built_before = (plist.read_bytes(), localized.read_bytes())
        command = [sys.executable, str(ROOT / ".github/scripts/source_version.py"),
                   "--verify-bundle", "test/Plugins/kiosk.bundle"]
        result = subprocess.run(command, cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((plist.read_bytes(), localized.read_bytes()), built_before)
        result = subprocess.run(command + ["--bump", "patch"],
                                cwd=self.root, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
