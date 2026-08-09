import importlib.util
import pathlib
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMANDS_PATH = ROOT / "vwx-plugin" / "commands.py"


def load_commands():
    sys.modules.setdefault("vs", types.SimpleNamespace())
    spec = importlib.util.spec_from_file_location("vwx_commands_test", COMMANDS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeRebuildHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = load_commands()

    def test_region_normalizes_axes_and_optional_z(self):
        region = self.commands._native_rebuild_region(
            {"x1": 8, "y1": 9, "x2": -2, "y2": 1, "z1": 7, "z2": 3}
        )
        self.assertEqual(
            region,
            {"x1": -2.0, "x2": 8.0, "y1": 1.0, "y2": 9.0,
             "z1": 3.0, "z2": 7.0},
        )
        self.assertTrue(
            self.commands._native_rebuild_in_region(
                {"x": 0, "y": 5, "z": 4}, region
            )
        )
        self.assertFalse(
            self.commands._native_rebuild_in_region(
                {"x": 0, "y": 5, "z": 8}, region
            )
        )
        with self.assertRaisesRegex(ValueError, "both z1 and z2"):
            self.commands._native_rebuild_region(
                {"x1": 0, "y1": 0, "x2": 1, "y2": 1, "z1": 0}
            )

    def test_profile_rules_are_ordered_and_support_dict_shorthand(self):
        rules = self.commands._native_rebuild_rules([
            {"contains": "ANGLE", "profile": "L_1X1"},
            {"starts_with": "GC-REF-TUBE", "profile": "Tube_1x1"},
        ])
        self.assertEqual(
            self.commands._native_rebuild_profile("GC-REF-ANGLE-X-01", rules),
            "L_1X1",
        )
        shorthand = self.commands._native_rebuild_rules(
            {"TUBE-1X2": "Tube_2x1"}
        )
        self.assertEqual(
            self.commands._native_rebuild_profile("GC-REF-TUBE-1X2-Y", shorthand),
            "Tube_2x1",
        )

    def test_vertical_source_falls_back_to_same_profile_horizontal_source(self):
        source_map = {
            "Tube_1x1|x": "HC-TUBE-1X1-X-03",
            "Tube_1x1|y": "HC-TUBE-1X1-Y-01",
        }
        self.assertEqual(
            self.commands._native_rebuild_source(
                source_map, "Tube_1x1", "z"
            ),
            "HC-TUBE-1X1-X-03",
        )

    def test_angle_parser_accepts_vectorworks_degree_strings(self):
        self.assertEqual(self.commands._native_rebuild_angle("180°"), 180.0)
        self.assertEqual(self.commands._native_rebuild_angle("90"), 90.0)
        self.assertEqual(self.commands._native_rebuild_angle("bad"), 0.0)


if __name__ == "__main__":
    unittest.main()
