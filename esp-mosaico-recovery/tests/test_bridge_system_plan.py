#!/usr/bin/env python3
"""Execute Bridge transport preflight using a local upstream cJSON checkout.

CJSON_COMPONENT_DIR=/path/to/espressif__cjson pytest tests/test_bridge_system_plan.py
Only esp_err_t constants are shimmed; manifest policy remains backend-owned.
"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class SystemPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="iris-device-policy-")
        cls.addClassCleanup(cls.cleanup_fixture)
        out = Path(cls.directory.name)
        root = Path(__file__).resolve().parents[1]
        cjson = Path(os.environ.get("CJSON_COMPONENT_DIR", str(root / "firmware/recovery/managed_components/espressif__cjson")))
        headers = list(cjson.rglob("cJSON.h"))
        assert headers, "Build Recovery first or set CJSON_COMPONENT_DIR"
        source = headers[0].with_name("cJSON.c")
        (out / "esp_err.h").write_text(
            "typedef int esp_err_t;\n#define ESP_OK 0\n"
            "#define ESP_ERR_INVALID_ARG 1\n#define ESP_ERR_NOT_SUPPORTED 2\n"
        )
        library = out / ("validator.dll" if os.name == "nt" else "validator.so")
        command = [
            os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-shared", "-fPIC", "-I", str(out), "-I", str(source.parent),
            str(root / "firmware/recovery/components/iris_bridge/system_plan.c"), str(source),
            "-o", str(library),
        ]
        if os.name == "nt":
            command.append("-Wl,--export-all-symbols")
        subprocess.run(command, check=True)
        cls.lib = ctypes.CDLL(str(library))
        cls.lib.cJSON_Parse.argtypes = [ctypes.c_char_p]
        cls.lib.cJSON_Parse.restype = ctypes.c_void_p
        cls.lib.cJSON_Delete.argtypes = [ctypes.c_void_p]
        cls.lib.iris_bridge_validate_system_plan.argtypes = [
            ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool,
        ]
        cls.lib.iris_bridge_validate_system_plan.restype = ctypes.c_int

    @classmethod
    def cleanup_fixture(cls):
        if hasattr(cls, "lib"):
            if os.name == "nt":
                import _ctypes
                _ctypes.FreeLibrary(cls.lib._handle)
            del cls.lib
        cls.directory.cleanup()

    def plan(self, kinds=("partition_table", "bootloader")):
        components, images = [], []
        for index, kind in enumerate(kinds, 1):
            component = dict(id=index, kind=kind, file=f"upload-{index}",
                             target_offset=index * 4096, size=4096, sha256="a" * 64)
            components.append(component)
            images.append(dict(component_id=index, kind=kind, upload_id=component["file"],
                               offset=component["target_offset"], size=4096, sha256="a" * 64))
        return dict(system_manifest=dict(target_layout_sha256="b" * 64, components=components),
                    target_table_sha256="b" * 64, images=images)

    def check(self, plan, bootloader=True, recovery=True):
        node = self.lib.cJSON_Parse(json.dumps(plan).encode())
        self.assertTrue(node)
        try:
            return self.lib.iris_bridge_validate_system_plan(node, bootloader, recovery)
        finally:
            self.lib.cJSON_Delete(node)

    def test_normal_bundle_bindings(self):
        self.assertEqual(self.check(self.plan()), 0)
        self.assertEqual(self.check(self.plan(("application", "data")), False, False), 0)

    def test_local_bootloader_gate(self):
        self.assertNotEqual(self.check(self.plan(), False), 0)
        plan = self.plan()
        plan["system_manifest"]["enable_bootloader_update"] = True
        self.assertNotEqual(self.check(plan, False), 0)

    def test_recovery_gate_and_isolation(self):
        self.assertEqual(self.check(self.plan(("recovery",))), 0)
        self.assertNotEqual(self.check(self.plan(("recovery",)), recovery=False), 0)

    def test_backend_owned_manifest_policy_is_not_duplicated(self):
        # Transport preflight binds leased files and local permissions. The
        # shared backend remains authoritative for manifest-only policy.
        self.assertEqual(self.check(self.plan(("bootloader",))), 0)
        self.assertEqual(self.check(self.plan(("application", "partition_table"))), 0)
        self.assertEqual(self.check(self.plan(("recovery", "data"))), 0)
        self.assertEqual(self.check(self.plan(("unknown",))), 0)

    def test_binding_mismatches(self):
        for key, value in dict(component_id=20, kind="data", offset=0, size=1, sha256="b" * 64,
                               upload_id="missing").items():
            with self.subTest(key=key):
                plan = self.plan()
                plan["images"][0][key] = value
                self.assertNotEqual(self.check(plan), 0)

    def test_bad_numbers_and_names(self):
        for key, value in (("id", 0), ("id", 256), ("id", 1.5), ("size", 0),
                           ("size", -1), ("target_offset", 2**32), ("file", "../x"),
                           ("file", "a?x=y"), ("file", "a" * 129)):
            with self.subTest(key=key, value=value):
                plan = self.plan()
                plan["system_manifest"]["components"][0][key] = value
                self.assertNotEqual(self.check(plan), 0)

    def test_missing_extra_duplicate_images(self):
        for images in ([], [self.plan()["images"][0]] * 2, self.plan()["images"] * 2):
            plan = self.plan()
            plan["images"] = images
            self.assertNotEqual(self.check(plan), 0)

    def test_counts_and_layout_binding(self):
        # Component-count bounds are manifest policy enforced by the backend.
        self.assertEqual(self.check(self.plan(())), 0)
        self.assertEqual(self.check(self.plan(("data",) * 97)), 0)
        plan = self.plan()
        plan["target_table_sha256"] = "c" * 64
        self.assertNotEqual(self.check(plan), 0)

    def test_unsupported_kind_and_missing_arrays(self):
        for key in ("images", "system_manifest"):
            plan = self.plan()
            del plan[key]
            self.assertNotEqual(self.check(plan), 0)


if __name__ == "__main__":
    unittest.main()
