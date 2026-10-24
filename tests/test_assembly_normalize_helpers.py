import importlib.util
import pathlib
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMANDS_PATH = ROOT / "vwx-plugin" / "commands.py"


def load_commands():
    sys.modules.setdefault("vs", types.SimpleNamespace())
    spec = importlib.util.spec_from_file_location("vwx_commands_normalize_test", COMMANDS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AssemblyNormalizeHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = load_commands()

    def test_classifier_routes_loci_degenerate_and_nonplot_to_helpers(self):
        dims = {"x": 10.0, "y": 5.0, "z": 2.0}
        center = {"x": 1.0, "y": 2.0, "z": 3.0}
        classify = self.commands._normalize_object_kind
        self.assertEqual(classify(25, "", "None", dims, center), "helper")
        self.assertEqual(classify(11, "HELPERS", "None", dims, center, "HELPERS"), "helper")
        self.assertEqual(classify(11, "", "NonPlot", dims, center), "helper")
        self.assertEqual(
            classify(11, "", "None", {"x": 0, "y": 0, "z": 0}, center),
            "helper",
        )
        self.assertEqual(classify(86, "FRAME-01", "None", dims, center), "model")

    def test_named_view_uses_SetViewVector(self):
        # VW2027: set_3d_view uses SetViewVector(loc, target, up), not SetView.
        calls = []
        original_vs = self.commands.vs
        self.commands.vs = types.SimpleNamespace(
            SetViewVector=lambda loc, tgt, up: calls.append((loc, tgt, up)))
        try:
            result = self.commands.set_3d_view({"view": "right"})
        finally:
            self.commands.vs = original_vs
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 1)
        loc, tgt, up = calls[0]
        self.assertEqual(loc, (1, 0, 0))   # right = camera at +X
        self.assertEqual(tgt, (0, 0, 0))
        self.assertEqual(up, (0, 0, 1))

    def test_named_view_falls_back_to_top_on_unknown_value(self):
        # Unknown views default to 'top' (no error — upstream behavior).
        calls = []
        original_vs = self.commands.vs
        self.commands.vs = types.SimpleNamespace(
            SetViewVector=lambda loc, tgt, up: calls.append((loc, tgt, up)))
        try:
            result = self.commands.set_3d_view({"view": "diagonalish"})
        finally:
            self.commands.vs = original_vs
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], (0, 0, 1))  # defaulted to top

    def test_projection_fails_closed_without_calling_vectorworks(self):
        calls = []
        original_vs = self.commands.vs
        self.commands.vs = types.SimpleNamespace(Projection=lambda *args: calls.append(args))
        try:
            result = self.commands.set_projection({
                "projection": 0,
                "render_mode": 11,
                "view_distance": 100,
                "clip1": 5,
                "clip2": 250,
            })
        finally:
            self.commands.vs = original_vs
        self.assertEqual(calls, [])
        self.assertIn("disabled", result["error"])

    def test_normalize_refuses_editable_pios_in_new_group(self):
        risk = self.commands._normalize_pio_group_move_risk
        self.assertTrue(risk("group", 0, 58))
        self.assertFalse(risk("group", 0, 0))

    def test_normalize_refuses_existing_group_but_allows_symbol(self):
        risk = self.commands._normalize_pio_group_move_risk
        self.assertTrue(risk("realign_existing", 11, 45))
        self.assertFalse(risk("realign_existing", 15, 45))

    def test_new_group_resolves_parent_from_disposable_child(self):
        calls = []
        parents = {"child": "new-group"}
        def set_parent(obj, parent):
            parents[obj] = parent
            calls.append(("parent", obj, parent))
        fake = types.SimpleNamespace(
            BeginGroup=lambda: calls.append("begin"),
            Locus=lambda x, y: calls.append(("locus", x, y)),
            LNewObj=lambda: "child",
            EndGroup=lambda: calls.append("end"),
            GetParent=lambda obj: parents.get(obj),
            DelObject=lambda child: calls.append(("delete", child)),
            SetParent=set_parent,
            SetName=lambda obj, name: calls.append(("name", obj, name)),
            SetClass=lambda obj, cls: calls.append(("class", obj, cls)),
        )
        original_vs = self.commands.vs
        self.commands.vs = fake
        try:
            group = self.commands._normalize_new_group(
                "target-layer", "ASSEMBLY", "NonPlot"
            )
        finally:
            self.commands.vs = original_vs
        self.assertEqual(group, "new-group")
        self.assertIn(("delete", "child"), calls)
        self.assertIn(("parent", "new-group", "target-layer"), calls)
        self.assertIn(("name", "new-group", "ASSEMBLY"), calls)


if __name__ == "__main__":
    unittest.main()
