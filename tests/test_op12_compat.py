"""Regression checks using metadata captured from the failed OP12 Image."""

import copy
import importlib.util
import json
import struct
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "op12_compat", ROOT / "scripts/check_op12_kernel_compat.py"
)
compat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compat)


class OP12ImageEntryTests(unittest.TestCase):
    def read_image(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Image"
            path.write_bytes(data)
            return compat.read_image(path)

    def make_image(self, instruction=0x1400000F, efi=True):
        data = bytearray(128)
        data[56:60] = b"ARM\x64"
        struct.pack_into("<Q", data, 16, 4096)
        if efi:
            data[:4] = b"MZ\x40\xfa"
        struct.pack_into("<I", data, 4 if efi else 0, instruction)
        return data

    def test_actual_broken_kpm_entry_is_rejected(self):
        fixture = json.loads(
            (ROOT / "tests/fixtures/op12-incomplete-kpm.json").read_text()
        )
        data = bytearray(fixture["file_size"])
        header = bytes.fromhex(fixture["header_hex"])
        data[: len(header)] = header
        with self.assertRaisesRegex(ValueError, "entry.*outside"):
            self.read_image(data)

    def test_valid_efi_entry_with_bss_beyond_file_is_allowed(self):
        data = self.make_image()
        self.assertEqual(self.read_image(data), data)

    def test_valid_non_efi_entry_is_allowed(self):
        data = self.make_image(instruction=0x14000010, efi=False)
        self.assertEqual(self.read_image(data), data)

    def test_negative_entry_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "entry.*outside"):
            self.read_image(self.make_image(instruction=0x17FFFFFE))

    def test_entry_into_header_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "entry.*outside"):
            self.read_image(self.make_image(instruction=0x14000000))

    def test_non_branch_entry_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "entry.*branch"):
            self.read_image(self.make_image(instruction=0xD503201F))


class OP12CompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = compat.load_reference(
            ROOT / "scripts/compat/op12-oos16.0.0.201.json"
        )
        cls.failed_image = json.loads(
            (ROOT / "tests/fixtures/op12-incompatible-slim.json").read_text()
        )

    def test_stock_control_passes(self):
        exports = self.reference["exports"]
        layouts = self.reference["scheduler_btf"]
        self.assertEqual(compat.compare_exports(exports, exports), ([], {}))
        self.assertEqual(compat.compare_scheduler_layouts(layouts, layouts), [])

    def test_failed_image_export_contract_is_rejected(self):
        exports = self.reference["exports"].copy()
        for name in self.failed_image["missing_exports"]:
            del exports[name]
        for name, values in self.failed_image["changed_export_crcs"].items():
            exports[name] = int(values["candidate"], 16)
        missing, changed = compat.compare_exports(self.reference["exports"], exports)
        self.assertEqual(set(missing), set(self.failed_image["missing_exports"]))
        self.assertIn("task_is_scx", missing)
        self.assertIn("sk_filter_trim_cap", changed)
        self.assertEqual(len(missing), 32)
        self.assertEqual(len(changed), 5)

    def test_failed_image_scheduler_layout_is_rejected(self):
        changes = compat.compare_scheduler_layouts(
            self.reference["scheduler_btf"], self.failed_image["scheduler_btf"]
        )
        fields = {change["field"] for change in changes}
        self.assertTrue(
            {
                "task_struct.scx.bit_offset",
                "sched_ext_entity.sizeof",
                "scx_dispatch_q.sizeof",
                "scx_iso_masks.presence",
            }.issubset(fields)
        )

    def test_new_exports_are_allowed(self):
        exports = {**self.reference["exports"], "new_builtin_root_export": 0x12345678}
        self.assertEqual(
            compat.compare_exports(self.reference["exports"], exports), ([], {})
        )

    def test_consuming_a_reserved_slot_preserves_the_layout_contract(self):
        layouts = copy.deepcopy(self.reference["scheduler_btf"])
        fields = layouts["task_struct"]["member_bit_offsets"]
        slot = next(name for name in fields if name.startswith("android_kabi_reserved"))
        fields["new_kabi_field"] = fields.pop(slot)
        self.assertEqual(
            compat.compare_scheduler_layouts(self.reference["scheduler_btf"], layouts),
            [],
        )


if __name__ == "__main__":
    unittest.main()
