import pathlib
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "vwx-plugin"
sys.path.insert(0, str(PLUGIN))
sys.modules.setdefault("vs", types.SimpleNamespace())

import assembly_templates as templates  # noqa: E402


class AssemblyTemplateTests(unittest.TestCase):
    def test_catalog_is_versioned_and_exact(self):
        self.assertEqual([row["id"] for row in templates.catalog()], [
            "case_box@1", "platform_deck@1", "stud_wall@1"])

    def test_stud_wall_expansion_is_deterministic_and_relocatable(self):
        params = {"prefix": "WALL-A", "layer": "WORK", "class": "LUMBER",
                  "width": 72, "height": 96, "spacing": 16}
        first, error = templates.expand("stud_wall@1", params)
        second, error2 = templates.expand("stud_wall@1", params)
        self.assertIsNone(error); self.assertIsNone(error2)
        self.assertEqual(first, second)
        self.assertEqual(first["count"], 9)
        self.assertEqual(first["bbox"]["max"], {"x": 72.0, "y": 3.5, "z": 96.0})

        moved, error = templates.expand("stud_wall@1", {**params, "x": 10, "z": 5})
        self.assertIsNone(error)
        self.assertEqual([row["name"] for row in first["items"]],
                         [row["name"] for row in moved["items"]])
        self.assertNotEqual(first["template_token"], moved["template_token"])

    def test_structural_parameter_change_mints_new_names(self):
        base = {"prefix": "DECK-A", "layer": "WORK",
                "width": 72, "depth": 48, "skin_thickness": 0.75}
        first, _ = templates.expand("platform_deck@1", base)
        changed, _ = templates.expand("platform_deck@1", {**base, "spacing": 12})
        self.assertEqual(first["count"], 9)
        self.assertTrue(set(row["name"] for row in first["items"]).isdisjoint(
            row["name"] for row in changed["items"]))

    def test_case_box_shelves_and_bounds(self):
        result, error = templates.expand("case_box@1", {
            "prefix": "CAB-A", "layer": "CASEWORK", "width": 30,
            "depth": 24, "height": 36, "shelves": 2})
        self.assertIsNone(error)
        self.assertEqual(result["count"], 7)
        self.assertEqual(result["bbox"]["min"], {"x": 0.0, "y": 0.0, "z": 0.0})
        self.assertEqual(result["bbox"]["max"], {"x": 30.0, "y": 24.0, "z": 36.0})
        self.assertEqual(result["geometry"]["name_prefix"], "CAB-A-")

    def test_validation_fails_closed(self):
        result, error = templates.expand("stud_wall@2", {})
        self.assertIsNone(result); self.assertIn("unknown template", error)
        result, error = templates.expand("stud_wall@1", {
            "prefix": "X", "layer": "WORK", "width": 72, "height": 96,
            "surprise": True})
        self.assertIsNone(result); self.assertIn("unknown parameters", error)
        result, error = templates.expand("case_box@1", {
            "prefix": "X", "layer": "WORK", "width": 1, "depth": 1,
            "height": 1, "thickness": 1})
        self.assertIsNone(result); self.assertIn("too small", error)

        result, error = templates.expand("case_box@1", {
            "prefix": "X", "layer": "WORK", "width": 24, "depth": 12,
            "height": 12, "shelves": 20})
        self.assertIsNone(result); self.assertIn("shelf pitch", error)

        result, error = templates.expand("stud_wall@1", {
            "prefix": "X", "layer": "WORK", "width": 100000000,
            "height": 96, "spacing": 1})
        self.assertIsNone(result); self.assertIn("max_items", error)

    def test_end_members_never_overlap(self):
        result, error = templates.expand("stud_wall@1", {
            "prefix": "EDGE", "layer": "WORK", "width": 33.5000001,
            "height": 96, "stud_width": 1.5, "spacing": 16})
        self.assertIsNone(error)
        studs = sorted((row for row in result["items"] if "-STUD-" in row["name"]),
                       key=lambda row: row["x"])
        self.assertEqual(len(studs), 3)
        self.assertTrue(all(right["x"] - left["x"] >= left["width"] - 1e-9
                            for left, right in zip(studs, studs[1:])))


if __name__ == "__main__":
    unittest.main()
