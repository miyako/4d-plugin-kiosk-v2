"""Compile actual dormant helpers and the unchanged SDK; simulate only Call4D."""

import os
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(platform.system() == "Darwin", "requires Apple's frameworks and clang")
class OwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="kiosk-ownership-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.binary = Path(cls.temp.name) / "ownership"
        # A separate source root permits running the exact same harness on an immutable baseline.
        source = Path(os.environ.get("KIOSK_OWNERSHIP_SOURCE", ROOT))
        result = subprocess.run([
            "xcrun", "clang++", "-std=c++11", "-g", "-O1",
            "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
            "-Wno-deprecated-declarations", "-Wno-pointer-sign",
            "-x", "objective-c++", "-I", str(source / "4D Plugin API"),
            "-I", str(source / "4D Plugin API/Classes"), "-I", str(source / "support"),
            str(ROOT / ".github/tests/ownership.cpp"),
            str(source / "4D Plugin API/4DPluginAPI.c"),
            str(source / "4D Plugin API/Classes/ARRAY_TEXT.cpp"),
            str(source / "support/4DPlugin-JSON.cpp"),
            "-framework", "Cocoa", "-framework", "CoreFoundation",
            "-framework", "CoreGraphics", "-o", str(cls.binary),
        ], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_case(self, case):
        for empty in ("allocated", "null"):
            with self.subTest(case=case, empty=empty):
                result = subprocess.run(
                    [str(self.binary), case, empty], capture_output=True, text=True,
                    env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
                         "UBSAN_OPTIONS": "halt_on_error=1"},
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_setters(self):
        for overload in range(4):
            self.run_case(f"setter{overload}")

    def test_getters(self):
        for getter in range(5):
            self.run_case(f"getter{getter}")

    def test_mismatches(self):
        for getter in range(5):
            self.run_case(f"mismatch{getter}")

    def test_arrays(self):
        self.run_case("arrays")

    def test_stringify(self):
        self.run_case("stringify")


if __name__ == "__main__":
    unittest.main()
