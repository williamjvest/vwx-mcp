import importlib.util
import json
import pathlib
import sys
import tempfile
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMANDS_PATH = ROOT / "vwx-plugin" / "commands.py"
sys.path.insert(0, str(COMMANDS_PATH.parent))


def load_commands():
    sys.modules.setdefault("vs", types.SimpleNamespace())
    spec = importlib.util.spec_from_file_location("vwx_commands_fast_test", COMMANDS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FastCompoundWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = load_commands()

    def test_box_specs_accept_center_and_dimensions(self):
        rows, error = self.commands._normalize_native_box_specs({
            "expected_count": 1,
            "items": [{
                "name": "STRAP-01",
                "center": {"x": 10, "y": 20, "z": 30},
                "dimensions": {"x": 2, "y": 4, "z": 6},
            }],
        })
        self.assertIsNone(error)
        self.assertEqual(rows[0]["x"], 9.0)
        self.assertEqual(rows[0]["y"], 18.0)
        self.assertEqual(rows[0]["z"], 27.0)
        self.assertEqual(rows[0]["target_center"], {"x": 10.0, "y": 20.0, "z": 30.0})

    def test_box_specs_reject_count_and_name_collisions(self):
        rows, error = self.commands._normalize_native_box_specs({
            "expected_count": 2,
            "items": [{"name": "ONE", "x": 0, "y": 0, "z": 0,
                       "width": 1, "depth": 1, "height": 1}],
        })
        self.assertIsNone(rows)
        self.assertIn("expected_count", error)

        rows, error = self.commands._normalize_native_box_specs({
            "items": [
                {"name": "SAME", "x": 0, "y": 0, "z": 0,
                 "width": 1, "depth": 1, "height": 1},
                {"name": "SAME", "x": 1, "y": 1, "z": 1,
                 "width": 1, "depth": 1, "height": 1},
            ],
        })
        self.assertIsNone(rows)
        self.assertIn("duplicate item name", error)

    def test_native_box_batch_is_dry_run_without_confirmation(self):
        original_vs = self.commands.vs
        self.commands.vs = types.SimpleNamespace(GetObject=lambda _name: None)
        try:
            result = self.commands.create_verified_native_boxes({
                "expected_count": 1,
                "items": [{"name": "SAFE-BOX", "x": 0, "y": 0, "z": 0,
                           "width": 1, "depth": 2, "height": 3}],
            })
        finally:
            self.commands.vs = original_vs
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["count"], 1)

    def test_native_box_reconcile_plans_create_update_replace_and_prune(self):
        desired, error = self.commands._normalize_native_box_specs({
            "layer": "WORK", "class": "LUMBER",
            "items": [
                {"name": "ASM-NEW", "x": 0, "y": 0, "z": 0,
                 "width": 1, "depth": 2, "height": 3},
                {"name": "ASM-MOVE", "center": {"x": 5, "y": 0, "z": 0},
                 "dimensions": {"x": 1, "y": 2, "z": 3}},
                {"name": "ASM-RESIZE", "center": {"x": 10, "y": 0, "z": 0},
                 "dimensions": {"x": 2, "y": 2, "z": 3}},
            ],
        })
        self.assertIsNone(error)
        actual = [
            {"name": "ASM-MOVE", "object_id": "move", "type": 24,
             "center": {"x": 4, "y": 0, "z": 0},
             "dimensions": {"x": 1, "y": 2, "z": 3}, "class": "LUMBER"},
            {"name": "ASM-RESIZE", "object_id": "resize", "type": 24,
             "center": {"x": 10, "y": 0, "z": 0},
             "dimensions": {"x": 1, "y": 2, "z": 3}, "class": "LUMBER"},
            {"name": "ASM-OLD", "object_id": "old", "type": 24,
             "center": {"x": 20, "y": 0, "z": 0},
             "dimensions": {"x": 1, "y": 2, "z": 3}, "class": "LUMBER"},
        ]
        plan, error = self.commands._native_box_reconcile_plan(
            desired, actual, tolerance=0.01, prune=True)
        self.assertIsNone(error)
        self.assertEqual([r["name"] for r in plan["actions"]["create"]], ["ASM-NEW"])
        self.assertEqual([r["name"] for r in plan["actions"]["update"]], ["ASM-MOVE"])
        self.assertEqual([r["name"] for r in plan["actions"]["replace"]], ["ASM-RESIZE"])
        self.assertEqual([r["name"] for r in plan["actions"]["delete"]], ["ASM-OLD"])

    def test_native_box_reconcile_rejects_ambiguous_names(self):
        desired, error = self.commands._normalize_native_box_specs({
            "items": [{"name": "ASM-ONE", "x": 0, "y": 0, "z": 0,
                       "width": 1, "depth": 1, "height": 1}],
        })
        self.assertIsNone(error)
        actual = [
            {"name": "ASM-ONE", "object_id": "one", "type": 24},
            {"name": "ASM-ONE", "object_id": "two", "type": 24},
        ]
        plan, error = self.commands._native_box_reconcile_plan(desired, actual)
        self.assertIsNone(plan)
        self.assertIn("ambiguous", error)

    def test_native_box_reconcile_token_changes_with_live_state(self):
        desired, error = self.commands._normalize_native_box_specs({
            "items": [{"name": "ASM-ONE", "x": 0, "y": 0, "z": 0,
                       "width": 1, "depth": 1, "height": 1}],
        })
        self.assertIsNone(error)
        plan_a, _ = self.commands._native_box_reconcile_plan(desired, [])
        live = [{"name": "ASM-ONE", "object_id": "one", "type": 24,
                 "center": {"x": 0.5, "y": 0.5, "z": 0.5},
                 "dimensions": {"x": 1, "y": 1, "z": 1}, "class": None}]
        plan_b, _ = self.commands._native_box_reconcile_plan(desired, live)
        token_a = self.commands._native_box_plan_token("WORK", "ASM-", False, desired, plan_a)
        token_b = self.commands._native_box_plan_token("WORK", "ASM-", False, desired, plan_b)
        self.assertNotEqual(token_a, token_b)

    def test_native_box_reconcile_restores_selection_and_maps_replacements(self):
        original_selected = self.commands._selected_object_ids
        original_h = self.commands._h
        original_vs = self.commands.vs
        state = {"keep", "old", "pruned", "auto-created"}
        self.commands._selected_object_ids = lambda *_args, **_kwargs: (sorted(state), None)
        self.commands._h = lambda object_id: object_id
        self.commands.vs = types.SimpleNamespace(
            SetDSelect=lambda h: state.discard(h),
            SetSelect=lambda h: state.add(h),
        )
        try:
            result = self.commands._restore_selection_ids(
                ["keep", "old", "pruned"],
                replacements={"old": "new"}, dropped={"pruned"})
        finally:
            self.commands._selected_object_ids = original_selected
            self.commands._h = original_h
            self.commands.vs = original_vs
        self.assertTrue(result["restored"])
        self.assertEqual(state, {"keep", "new"})
        self.assertEqual(result["replacement_count"], 1)
        self.assertEqual(result["dropped_count"], 1)

    def test_selection_restore_reports_vectorworks_failure_without_raising(self):
        original_selected = self.commands._selected_object_ids
        original_h = self.commands._h
        original_vs = self.commands.vs
        self.commands._selected_object_ids = lambda *_args, **_kwargs: (["one"], None)
        self.commands._h = lambda object_id: object_id
        self.commands.vs = types.SimpleNamespace(
            SetDSelect=lambda _h: (_ for _ in ()).throw(RuntimeError("selection failure")),
            SetSelect=lambda _h: None,
        )
        try:
            result = self.commands._restore_selection_ids(["one"])
        finally:
            self.commands._selected_object_ids = original_selected
            self.commands._h = original_h
            self.commands.vs = original_vs
        self.assertFalse(result["restored"])
        self.assertIn("selection failure", result["error"])

    def test_resource_items_accept_only_live_verified_kinds(self):
        rows, error = self.commands._normalize_resource_items({
            "items": [
                {"kind": "symbol", "name": "Chair"},
                {"kind": "hatch", "name": "Steel HF"},
                {"kind": "texture", "name": "Wood RT"},
            ],
        })
        self.assertIsNone(error)
        self.assertEqual([row["type"] for row in rows], [16, 66, 97])

        rows, error = self.commands._normalize_resource_items({
            "items": [{"kind": "material", "name": "Steel"}],
        })
        self.assertIsNone(rows)
        self.assertIn("unsupported resource kind", error)

    def test_resource_library_search_separates_exact_and_suggestions(self):
        names = {
            101: ["Event Folding Chair", "Event Wooden Seat"],
            102: ["Chair Event Folding", "Event Folding Chair"],
        }
        fake = types.SimpleNamespace(
            BuildResourceListN=lambda _type, path: (101 if path == "a.vwx" else 102, 2),
            ResourceListSize=lambda list_id: len(names[list_id]),
            GetActualNameFromResourceList=lambda list_id, index: names[list_id][index - 1],
        )
        original_vs = self.commands.vs
        self.commands.vs = fake
        try:
            result = self.commands._search_resource_paths(
                "symbol", "Event Folding Chair", ["a.vwx", "b.vwx"])
        finally:
            self.commands.vs = original_vs
        self.assertEqual(len(result["exact"]), 2)
        self.assertEqual(result["files_scanned"], 2)
        self.assertEqual([row["name"] for row in result["suggestions"]],
                         ["Chair Event Folding"])

    def test_resource_plan_classifies_existing_and_safe_import(self):
        with tempfile.NamedTemporaryFile(suffix=".vwx") as source:
            names = {201: ["Existing", "Import Me", "Duplicate"]}
            fake = types.SimpleNamespace(
                GetObject=lambda name: "existing-h" if name == "Existing" else None,
                GetTypeN=lambda _h: 16,
                GetObjectUuid=lambda _h: "existing-id",
                BuildResourceListN=lambda _type, _path: (201, 3),
                ResourceListSize=lambda list_id: len(names[list_id]),
                GetActualNameFromResourceList=lambda list_id, index: names[list_id][index - 1],
            )
            original_vs = self.commands.vs
            self.commands.vs = fake
            try:
                rows, error = self.commands._normalize_resource_items({
                    "items": [
                        {"kind": "symbol", "name": "Existing", "source_path": source.name},
                        {"kind": "texture", "name": "Import Me", "source_path": source.name},
                    ],
                })
                self.assertIsNone(error)
                plan = self.commands._plan_ensure_resources(rows)
            finally:
                self.commands.vs = original_vs
        self.assertEqual([row["state"] for row in plan], ["existing", "import"])

    def test_resource_plan_marks_missing_symbol_manual_required(self):
        with tempfile.NamedTemporaryFile(suffix=".vwx") as source:
            fake = types.SimpleNamespace(
                GetObject=lambda _name: None,
                BuildResourceListN=lambda _type, _path: (211, 1),
                ResourceListSize=lambda _list_id: 1,
                GetActualNameFromResourceList=lambda _list_id, _index: "Chair",
            )
            original_vs = self.commands.vs
            original_cache = self.commands._RESOURCE_INDEX_CACHE
            self.commands.vs = fake
            self.commands._RESOURCE_INDEX_CACHE = {}
            try:
                rows, error = self.commands._normalize_resource_items({
                    "items": [{"kind": "symbol", "name": "Chair",
                               "source_path": source.name}],
                })
                self.assertIsNone(error)
                plan = self.commands._plan_ensure_resources(rows)
            finally:
                self.commands.vs = original_vs
                self.commands._RESOURCE_INDEX_CACHE = original_cache
        self.assertEqual(plan[0]["state"], "manual_required")
        self.assertIn("modal-blocked", plan[0]["reason"])

    def test_project_standard_normalizes_manifest_and_rejects_cross_collisions(self):
        standard, error = self.commands._normalize_project_standard({
            "standard": {
                "document": {"units": "inches"},
                "layers": [{"name": "MODEL", "scale": 48, "visible": True}],
                "classes": [{"name": "CASEWORK", "fill_rgb": [120, 80, 40],
                             "line_weight": 0.25}],
                "saved_views": [{"name": "QA-All"}],
                "worksheets": [{"name": "Starter", "rows": 3, "columns": 2,
                                "cells": [{"row": 1, "col": 1, "value": "Name"}]}],
            },
        })
        self.assertIsNone(error)
        self.assertEqual(standard["document"]["units"], "inch")
        self.assertEqual(standard["layers"][0]["type"], "design")
        self.assertEqual(standard["worksheets"][0]["cells"][0]["value"], "Name")

        standard, error = self.commands._normalize_project_standard({
            "standard": {
                "layers": [{"name": "DUPLICATE"}],
                "worksheets": [{"name": "duplicate"}],
            },
        })
        self.assertIsNone(standard)
        self.assertIn("both layers and worksheets", error)

        standard, error = self.commands._normalize_project_standard({
            "standard": {"worksheets": [{"name": "Unsafe", "cells": [
                {"row": 1, "col": 1, "value": "=RUNSCRIPT('Anything')"},
            ]}]},
        })
        self.assertIsNone(standard)
        self.assertIn("static text", error)

    def test_project_standard_plan_is_conservative_and_idempotent(self):
        standard, error = self.commands._normalize_project_standard({
            "standard": {
                "document": {"units": "mm"},
                "layers": [
                    {"name": "EXISTING", "scale": 50},
                    {"name": "NEW", "scale": 25},
                ],
                "classes": [{"name": "CASEWORK", "visible": True}],
            },
        })
        self.assertIsNone(error)
        originals = {
            "_current_unit_key": self.commands._current_unit_key,
            "_layer_state_map": self.commands._layer_state_map,
            "_class_state_map": self.commands._class_state_map,
            "_saved_view_names_strict": self.commands._saved_view_names_strict,
            "_worksheet_names": self.commands._worksheet_names,
            "vs": self.commands.vs,
        }
        self.commands._current_unit_key = lambda: "inch"
        self.commands._layer_state_map = lambda: {
            "EXISTING": {"name": "EXISTING", "type": "design", "scale": 50.0,
                         "visible": True},
        }
        self.commands._class_state_map = lambda: {
            "CASEWORK": {"fill": None, "pen": None, "lineweight": 0,
                         "visible": True},
        }
        self.commands._saved_view_names_strict = lambda: []
        self.commands._worksheet_names = lambda: []
        self.commands.vs = types.SimpleNamespace(GetObject=lambda _name: None)
        try:
            guarded = self.commands._plan_project_standard(standard, False)
            approved = self.commands._plan_project_standard(standard, True)
        finally:
            for name, value in originals.items():
                setattr(self.commands, name, value)
        self.assertEqual(guarded["sections"]["document"][0]["state"],
                         "approval_required")
        self.assertEqual(approved["sections"]["document"][0]["state"], "update")
        self.assertEqual([row["state"] for row in approved["sections"]["layers"]],
                         ["existing", "create"])
        self.assertEqual(approved["sections"]["classes"][0]["state"], "existing")

    def test_project_standard_case_only_existing_names_fail_closed(self):
        standard, error = self.commands._normalize_project_standard({
            "standard": {
                "layers": [{"name": "walls"}],
                "classes": [{"name": "casework"}],
            },
        })
        self.assertIsNone(error)
        originals = (self.commands._layer_state_map, self.commands._class_state_map,
                     self.commands._saved_view_names_strict, self.commands._worksheet_names,
                     self.commands.vs)
        self.commands._layer_state_map = lambda: {
            "Walls": {"name": "Walls", "type": "design", "scale": 1.0,
                      "visible": True},
        }
        self.commands._class_state_map = lambda: {
            "Casework": {"fill": [0, 0, 0], "pen": [0, 0, 0],
                         "lineweight": 0, "visible": True},
        }
        self.commands._saved_view_names_strict = lambda: []
        self.commands._worksheet_names = lambda: []
        self.commands.vs = types.SimpleNamespace(GetObject=lambda _name: None)
        try:
            plan = self.commands._plan_project_standard(standard)
        finally:
            (self.commands._layer_state_map, self.commands._class_state_map,
             self.commands._saved_view_names_strict, self.commands._worksheet_names,
             self.commands.vs) = originals
        self.assertEqual(plan["sections"]["layers"][0]["state"], "collision")
        self.assertEqual(plan["sections"]["classes"][0]["state"], "collision")

    def test_project_standard_confirm_requires_fresh_token(self):
        original_plan = self.commands._plan_project_standard
        original_token = self.commands._project_standard_token
        self.commands._plan_project_standard = lambda *_args, **_kwargs: {
            "sections": {name: [] for name in (
                "document", "layers", "classes", "resources",
                "saved_views", "worksheets")},
            "summary": {"converged": True},
        }
        self.commands._project_standard_token = lambda *_args: "fresh-token"
        try:
            result = self.commands.ensure_project_standards({
                "standard": {"layers": []},
                "confirm": True,
                "plan_token": "stale-token",
            })
        finally:
            self.commands._plan_project_standard = original_plan
            self.commands._project_standard_token = original_token
        self.assertEqual(result["status"], "blocked")
        self.assertIn("stale", result["error"])

    def test_project_compiler_defers_geometry_until_declared_layer_exists(self):
        project, error = self.commands._normalize_project_compile({
            "project": {
                "standard": {"layers": [{"name": "WORK", "type": "design"}]},
                "geometry": {
                    "layer": "work", "name_prefix": "PC-",
                    "items": [{"name": "PC-A", "x": 0, "y": 0, "z": 0,
                               "width": 10, "depth": 2, "height": 3}],
                },
            },
        })
        self.assertIsNone(error)
        self.assertEqual(project["geometry"]["layer"], "WORK")
        original_plan = self.commands._plan_project_standard
        original_vs = self.commands.vs
        self.commands._plan_project_standard = lambda *_args, **_kwargs: {
            "sections": {name: [] for name in (
                "document", "layers", "classes", "resources",
                "saved_views", "worksheets")},
            "summary": {"converged": False, "create": 1},
        }
        self.commands.vs = types.SimpleNamespace(GetLayerByName=lambda _name: None)
        try:
            plan = self.commands._project_compile_plan(project, {
                "allow_units_change": False, "library_roots": None,
                "max_files": 700, "suggestion_limit": 8, "tolerance": 0.01,
            })
        finally:
            self.commands._plan_project_standard = original_plan
            self.commands.vs = original_vs
        self.assertEqual(plan["stages"]["standard"]["state"], "ready")
        self.assertEqual(plan["stages"]["geometry"]["state"], "deferred")
        self.assertEqual(plan["summary"]["deferred"], ["geometry"])

    def test_project_compiler_rejects_prune_and_stale_outer_token(self):
        project, error = self.commands._normalize_project_compile({
            "project": {"geometry": {
                "layer": "WORK", "name_prefix": "PC-", "prune": True,
                "items": [{"name": "PC-A", "x": 0, "y": 0, "z": 0,
                           "width": 1, "depth": 1, "height": 1}],
            }},
        })
        self.assertIsNone(project)
        self.assertIn("forbids geometry prune", error)

        original_plan = self.commands._project_compile_plan
        original_token = self.commands._project_compile_token
        self.commands._project_compile_plan = lambda *_args: {
            "stages": {}, "summary": {"converged": True,
                                         "blocked": [], "deferred": []}}
        self.commands._project_compile_token = lambda *_args: "fresh-token"
        try:
            result = self.commands.compile_vw_project({
                "project": {"standard": {"layers": []}},
                "confirm": True, "plan_token": "stale-token",
            })
        finally:
            self.commands._project_compile_plan = original_plan
            self.commands._project_compile_token = original_token
        self.assertEqual(result["status"], "blocked")
        self.assertIn("stale", result["error"])

    def test_project_compiler_preserves_dirty_failed_stage_diagnostics(self):
        original_plan = self.commands._project_compile_plan
        original_token = self.commands._project_compile_token
        original_ensure = self.commands.ensure_project_standards
        self.commands._project_compile_plan = lambda *_args: {
            "stages": {"standard": {"state": "ready"}},
            "summary": {"converged": False, "blocked": [], "deferred": []}}
        self.commands._project_compile_token = lambda *_args: "fresh-token"
        calls = {"count": 0}
        def fake_ensure(_payload):
            calls["count"] += 1
            if calls["count"] == 1:
                return {"status": "dry_run", "plan_token": "inner-token"}
            return {"status": "error", "error": "apply failed",
                    "rollback_top_level_clean": False,
                    "rollback_errors": [{"error": "active layer leaked"}]}
        self.commands.ensure_project_standards = fake_ensure
        try:
            result = self.commands.compile_vw_project({
                "project": {"standard": {"layers": []}},
                "confirm": True, "plan_token": "fresh-token",
            })
        finally:
            self.commands._project_compile_plan = original_plan
            self.commands._project_compile_token = original_token
            self.commands.ensure_project_standards = original_ensure
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["failed_stage_result"]["rollback_top_level_clean"])
        self.assertIn("incomplete rollback", result["manual_recovery"])

    def test_auto_dimensions_are_deterministic_and_read_only(self):
        self.assertAlmostEqual(self.commands._dimension_text_mm("1'-4\""), 406.4)
        original_manifest = self.commands.get_assembly_manifest
        original_vs = self.commands.vs
        self.commands.get_assembly_manifest = lambda _p: {
            "status": "ok", "count": 2, "truncated": False,
            "objects": [
                {"name": "FRAME-B", "center": {"x": 9, "y": 1, "z": 1},
                 "dimensions": {"x": 2, "y": 2, "z": 2}},
                {"name": "FRAME-A", "center": {"x": 1, "y": 1, "z": 1},
                 "dimensions": {"x": 2, "y": 2, "z": 2}},
            ],
        }
        self.commands.vs = types.SimpleNamespace(
            GetFPathName=lambda: "/tmp/test.vwx",
            GetObject=lambda name: "vp" if name == "VP-PLAN" else None,
            GetTypeN=lambda _h: 122,
            GetObjectVariableInt=lambda _h, selector: 6 if selector == 1000 else 7,
        )
        params = {"layer": "WORK", "name_prefix": "FRAME-",
                  "viewport": "VP-PLAN", "plane": "plan",
                  "rules": ["overall", "centers", "members"]}
        try:
            first = self.commands.plan_auto_dimensions(params)
            second = self.commands.plan_auto_dimensions(params)
        finally:
            self.commands.get_assembly_manifest = original_manifest
            self.commands.vs = original_vs
        self.assertTrue(first["read_only"])
        self.assertEqual(first["snapshot_digest"], second["snapshot_digest"])
        self.assertEqual(first["dimension_count"], 7)
        names = [row["name"] for row in first["package"]["dimensions"]]
        self.assertEqual(names, sorted(names))

    def test_auto_dimensions_fail_closed_on_ambiguous_center_axis(self):
        original_manifest = self.commands.get_assembly_manifest
        original_vs = self.commands.vs
        self.commands.vs = types.SimpleNamespace(
            GetFPathName=lambda: "/tmp/test.vwx", GetObject=lambda _name: "vp",
            GetTypeN=lambda _h: 122,
            GetObjectVariableInt=lambda _h, selector: 6 if selector == 1000 else 7,
        )
        self.commands.get_assembly_manifest = lambda _p: {
            "status": "ok", "count": 2, "truncated": False,
            "objects": [
                {"name": "FRAME-A", "center": {"x": 0, "y": 0, "z": 0},
                 "dimensions": {"x": 1, "y": 1, "z": 1}},
                {"name": "FRAME-B", "center": {"x": 10, "y": 10, "z": 0},
                 "dimensions": {"x": 1, "y": 1, "z": 1}},
            ],
        }
        try:
            result = self.commands.plan_auto_dimensions({
                "layer": "WORK", "name_prefix": "FRAME-",
                "viewport": "VP", "rules": ["centers"]})
        finally:
            self.commands.get_assembly_manifest = original_manifest
        self.assertIn("ambiguous", result["error"])

        self.commands.get_assembly_manifest = lambda _p: {
            "status": "ok", "count": 2, "truncated": False,
            "objects": [
                {"name": "FRAME-A", "center": {"x": 0, "y": 0, "z": 0},
                 "dimensions": {"x": 1, "y": 1, "z": 1}},
                {"name": "FRAME-B", "center": {"x": 10, "y": 10, "z": 0},
                 "dimensions": {"x": 1, "y": 1, "z": 1}},
            ],
        }
        try:
            resolved = self.commands.plan_auto_dimensions({
                "layer": "WORK", "name_prefix": "FRAME-", "viewport": "VP",
                "rules": ["centers"], "center_axis": "x"})
        finally:
            self.commands.get_assembly_manifest = original_manifest
            self.commands.vs = original_vs
        self.assertEqual(resolved["dimension_count"], 1)

    def test_revision_snapshot_and_diff_are_deterministic(self):
        original_manifest = self.commands.get_assembly_manifest
        original_vs = self.commands.vs
        self.commands.get_assembly_manifest = lambda _p: {
            "status": "ok", "truncated": False,
            "objects": [{"name": "FRAME-A", "type": 24, "type_name": "extrude",
                         "class": "LUMBER", "layer": "WORK", "role": "tube",
                         "axis": "x", "center": {"x": 1.00004, "y": 2, "z": 3},
                         "dimensions": {"x": 10, "y": 2, "z": 3},
                         "length": 10, "material": None, "texture": None}],
        }
        self.commands.vs = types.SimpleNamespace(
            GetUnits=lambda: (0, 0, 0, 1.0), GetFPathName=lambda: "/tmp/test.vwx")
        try:
            baseline = self.commands.get_revision_snapshot({
                "domains": ["geometry"], "layer": "WORK",
                "name_prefix": "FRAME-", "quantum": 0.001})
            current = self.commands.get_revision_snapshot({
                "domains": ["geometry"], "layer": "WORK",
                "name_prefix": "FRAME-", "quantum": 0.001})
        finally:
            self.commands.get_assembly_manifest = original_manifest
            self.commands.vs = original_vs
        self.assertEqual(baseline["digest"], current["digest"])
        clean = self.commands.get_revision_diff({"baseline": baseline, "current": current})
        self.assertTrue(clean["summary"]["clean"])
        current = json.loads(json.dumps(current))
        current["rows"]["geometry"][0]["dimensions"]["x"] = 12
        current["digest"] = self.commands._revision_digest(
            self.commands._revision_snapshot_payload(current))
        diff = self.commands.get_revision_diff({"baseline": baseline, "current": current})
        self.assertEqual(diff["summary"]["changed"], 1)
        self.assertIn("dimensions", diff["changes"][0]["fields"])

    def test_revision_baselines_are_create_only_and_report_is_token_gated(self):
        snapshot_payload = {
            "schema": 1, "document": "/tmp/test.vwx", "units_per_inch": 1.0,
            "quantum": 0.001, "domains": ["geometry"],
            "scope": {"layer": "WORK"}, "rows": {"geometry": []},
            "partial": False, "limitations": [],
        }
        snapshot = {**snapshot_payload, "status": "ok", "read_only": True,
                    "taken_at": 1,
                    "digest": self.commands._revision_digest(snapshot_payload)}
        original_vs = self.commands.vs
        original_root = self.commands._REVISION_BASELINE_ROOT_OVERRIDE
        self.commands.vs = types.SimpleNamespace(GetFPathName=lambda: "/tmp/test.vwx")
        with tempfile.TemporaryDirectory() as root:
            self.commands._REVISION_BASELINE_ROOT_OVERRIDE = root
            self.commands._REVISION_BASELINES.pop("TEST", None)
            stored = self.commands.manage_revision_baseline({
                "action": "store", "name": "TEST", "snapshot": snapshot})
            self.commands._REVISION_BASELINES.clear()
            loaded = self.commands.manage_revision_baseline({"action": "get", "name": "TEST"})
            duplicate = self.commands.manage_revision_baseline({
                "action": "store", "name": "TEST", "snapshot": snapshot})
            self.assertEqual(stored["status"], "ok")
            self.assertEqual(loaded["snapshot"]["digest"], snapshot["digest"])
            self.assertIn("never overwritten", duplicate["error"])
            self.commands.manage_revision_baseline({"action": "forget", "name": "TEST"})
        self.commands._REVISION_BASELINE_ROOT_OVERRIDE = original_root
        self.commands.vs = original_vs
        partial = dict(snapshot); partial["partial"] = True
        partial["digest"] = self.commands._revision_digest(
            self.commands._revision_snapshot_payload(partial))
        refused = self.commands.manage_revision_baseline({
            "action": "store", "name": "PARTIAL", "snapshot": partial})
        self.assertIn("partial", refused["error"])
        self.assertTrue(self.commands._revision_static_text("=RUNSCRIPT('x')").startswith("'="))
        self.assertTrue(self.commands._revision_static_text(" \t=RUNSCRIPT('x')").startswith("'="))

        self.commands.vs = types.SimpleNamespace(
            GetObject=lambda _name: None, GetFPathName=lambda: "/tmp/test.vwx")
        diff = {"status": "ok", "read_only": True,
                "baseline_digest": "b", "current_digest": "c",
                "summary": {"added": 0, "removed": 0, "changed": 0,
                            "total": 0, "clean": True},
                "changes": [], "human_summary": []}
        diff["diff_token"] = self.commands._revision_digest(diff)
        try:
            dry = self.commands.ensure_revision_report({
                "name": "REV-1", "diff": diff})
            blocked = self.commands.ensure_revision_report({
                "name": "REV-1", "diff": diff,
                "confirm": True, "plan_token": "stale"})
        finally:
            self.commands.vs = original_vs
        self.assertEqual(dry["state"], "create")
        self.assertEqual(blocked["status"], "blocked")

    def test_revision_snapshot_supports_multiple_geometry_scopes(self):
        original_manifest = self.commands.get_assembly_manifest
        original_vs = self.commands.vs
        self.commands.get_assembly_manifest = lambda p: {
            "status": "ok", "truncated": False, "truncated_by_guard": False,
            "objects": [{"name": "PART-1", "type": 24, "type_name": "extrude",
                         "class": "C", "layer": p["layer"], "role": "panel",
                         "axis": "x", "center": {"x": 0, "y": 0, "z": 0},
                         "dimensions": {"x": 1, "y": 2, "z": 3}, "length": 3,
                         "material": None, "texture": None}],
        }
        self.commands.vs = types.SimpleNamespace(
            GetUnits=lambda: (0, 0, 0, 1.0), GetFPathName=lambda: "/tmp/test.vwx")
        try:
            snapshot = self.commands.get_revision_snapshot({
                "domains": ["geometry"],
                "geometry_scopes": [
                    {"layer": "A", "name_prefix": "ONE-"},
                    {"layer": "B", "name_prefix": "TWO-"}],
            })
        finally:
            self.commands.get_assembly_manifest = original_manifest
            self.commands.vs = original_vs
        self.assertEqual(len(snapshot["rows"]["geometry"]), 2)
        self.assertNotEqual(snapshot["rows"]["geometry"][0]["key"],
                            snapshot["rows"]["geometry"][1]["key"])

    def test_document_search_includes_layer_class_and_symbol_catalog(self):
        original_index = self.commands.index_document
        self.commands.index_document = lambda _p: {
            "status": "ok", "truncated": False, "index_token": "idx", "count": 0,
            "coverage": {}, "rows": [],
            "catalog": [
                {"entity_kind": "layer", "entity_id": "layer:a", "name": "A-Plan",
                 "layer": "A-Plan", "type_name": "design"},
                {"entity_kind": "class", "entity_id": "class:c", "name": "Casework",
                 "class": "Casework", "type_name": "class"},
                {"entity_kind": "symbol_definition", "object_id": "sym:1",
                 "name": "Dining Chair"}],
        }
        try:
            result = self.commands.search_document({"query": "chair"})
        finally:
            self.commands.index_document = original_index
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["entity_kind"], "symbol_definition")

    def test_new_mutating_workflows_block_stale_tokens(self):
        original_change = self.commands._plan_document_changes
        original_sync = self.commands._plan_drawing_sync
        original_qa = self.commands._plan_qa_autopilot
        self.commands._plan_document_changes = lambda _p: ({
            "status": "dry_run", "plan_token": "fresh"}, None)
        self.commands._plan_drawing_sync = lambda _p: ({
            "status": "dry_run", "plan_token": "fresh"}, None)
        self.commands._plan_qa_autopilot = lambda _p: ({
            "status": "dry_run", "plan_token": "fresh"}, None)
        try:
            results = [
                self.commands.compile_document_changes({"confirm": True, "plan_token": "stale"}),
                self.commands.sync_drawing_package({"confirm": True, "plan_token": "stale"}),
                self.commands.qa_autopilot({"confirm": True, "plan_token": "stale"}),
            ]
        finally:
            self.commands._plan_document_changes = original_change
            self.commands._plan_drawing_sync = original_sync
            self.commands._plan_qa_autopilot = original_qa
        self.assertTrue(all(result["status"] == "blocked" for result in results))

    def test_component_editor_is_token_gated_and_verifies_semantic_fields(self):
        original_vs = self.commands.vs
        original_h = self.commands._h
        original_classes = self.commands._class_state_map
        original_selected = self.commands._selected_object_ids
        original_restore = self.commands._restore_selection_ids
        state = {"name": "Core", "width": 4.0, "class": "Wall-Core",
                 "function": 1, "material": "Gypsum"}
        material_handle = object()
        self.commands._h = lambda object_id: "wall" if object_id == "wall-id" else None
        self.commands._class_state_map = lambda: {"Wall-Core": {}, "Wall-Finish": {}}
        self.commands._selected_object_ids = lambda: (["selected"], None)
        self.commands._restore_selection_ids = lambda ids: {"restored": ids == ["selected"]}

        def set_value(field):
            def setter(_h, _index, value):
                state[field] = ("Gypsum" if field == "material" and value is material_handle
                                else "" if field == "material" and not value else value)
                return True
            return setter

        self.commands.vs = types.SimpleNamespace(
            GetNumberOfComponents=lambda _h: (True, 1),
            GetComponentName=lambda _h, _i: state["name"],
            GetComponentWidth=lambda _h, _i: (True, state["width"]),
            GetComponentClass=lambda _h, _i: (True, state["class"]),
            GetComponentFunction=lambda _h, _i: (True, state["function"]),
            GetComponentMaterial=lambda _h, _i: (True, material_handle if state["material"] else None),
            GetName=lambda h: "Gypsum" if h is material_handle else "",
            GetObject=lambda name: material_handle if name == "Gypsum" else None,
            GetFPathName=lambda: "/tmp/test.vwx", GetTypeN=lambda _h: 68,
            SetComponentName=set_value("name"), SetComponentWidth=set_value("width"),
            SetComponentClass=set_value("class"), SetComponentFunction=set_value("function"),
            SetComponentMaterial=set_value("material"), ResetObject=lambda _h: None,
            NameUndoEvent=lambda _name: None,
        )
        params = {"object_id": "wall-id", "updates": [{"index": 1, "set": {
            "name": "Finish", "width": 0.5, "class": "Wall-Finish",
            "function": 2, "material": "Gypsum"}}]}
        try:
            plan = self.commands.edit_components(params)
            blocked = self.commands.edit_components({**params, "confirm": True,
                                                      "plan_token": "stale"})
            applied = self.commands.edit_components({**params, "confirm": True,
                                                      "plan_token": plan["plan_token"]})
        finally:
            self.commands.vs = original_vs
            self.commands._h = original_h
            self.commands._class_state_map = original_classes
            self.commands._selected_object_ids = original_selected
            self.commands._restore_selection_ids = original_restore
        self.assertEqual(plan["status"], "dry_run")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(applied["status"], "ok")
        self.assertEqual(state["name"], "Finish")
        self.assertEqual(state["width"], 0.5)
        self.assertEqual(state["class"], "Wall-Finish")

    def test_delete_layer_refuses_active_layer_and_verifies_readback(self):
        original_vs = self.commands.vs
        layer = object()
        self.commands.vs = types.SimpleNamespace(
            GetLayerByName=lambda _name: layer,
            ActLayer=lambda: layer,
            DelObject=lambda _h: None,
        )
        try:
            blocked = self.commands.delete_layer({"name": "Active"})
        finally:
            self.commands.vs = original_vs
        self.assertIn("active layer", blocked["error"])

    def test_semantic_search_ranks_terms_and_structured_ranges(self):
        original_manifest = self.commands.get_assembly_manifest
        original_vs = self.commands.vs
        objects = [
            {"object_id": "post", "name": "FRAME-POST-01", "type": 24,
             "type_name": "extrude", "class": "LUMBER", "layer": "WORK",
             "role": "tube", "axis": "z", "center": {"x": 0, "y": 0, "z": 48},
             "dimensions": {"x": 1.5, "y": 3.5, "z": 96}, "material": "pine"},
            {"object_id": "rail", "name": "FRAME-RAIL-01", "type": 24,
             "type_name": "extrude", "class": "LUMBER", "layer": "WORK",
             "role": "tube", "axis": "x", "center": {"x": 36, "y": 0, "z": 1.75},
             "dimensions": {"x": 72, "y": 1.5, "z": 3.5}, "material": "pine"},
            {"object_id": "shelf", "name": "CABINET-SHELF-01", "type": 24,
             "type_name": "extrude", "class": "CASEWORK", "layer": "WORK",
             "role": "panel", "axis": "x", "center": {"x": 15, "y": 12, "z": 18},
             "dimensions": {"x": 28.5, "y": 23.75, "z": 0.75}},
        ]
        self.commands.get_assembly_manifest = lambda _p: {
            "status": "ok", "truncated": False, "objects": objects}
        self.commands.vs = types.SimpleNamespace(GetFPathName=lambda: "/tmp/test.vwx")
        try:
            result = self.commands.semantic_object_search({
                "layer": "WORK", "query": "lumber vertical",
                "filters": {"dimensions": {"z": {"min": 80}}}})
            cabinet = self.commands.semantic_object_search({
                "layer": "WORK", "query": "cabinet", "mode": "any"})
            truncated = self.commands.semantic_object_search({
                "layer": "WORK", "query": "lumber", "limit": 1})
        finally:
            self.commands.get_assembly_manifest = original_manifest
            self.commands.vs = original_vs
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["object_id"], "post")
        self.assertTrue(any("vertical" in reason for reason in result["matches"][0]["reasons"]))
        self.assertEqual(cabinet["matches"][0]["object_id"], "shelf")
        self.assertTrue(truncated["truncated"])
        self.assertEqual(truncated["total_match_count"], 2)

    def test_semantic_selection_requires_fresh_state_token(self):
        original_search = self.commands._semantic_search_plan
        original_collect = self.commands._collect
        original_vs = self.commands.vs
        self.commands._semantic_search_plan = lambda _p: ({
            "status": "ok", "read_only": True, "search_token": "search",
            "matches": [{"object_id": "one"}], "match_count": 1}, None)
        self.commands._collect = lambda *_args: []
        self.commands.vs = types.SimpleNamespace(GetFPathName=lambda: "/tmp/test.vwx")
        try:
            dry = self.commands.select_semantic_objects({"layer": "WORK", "query": "stud"})
            stale = self.commands.select_semantic_objects({
                "layer": "WORK", "query": "stud", "confirm": True,
                "plan_token": "stale"})
        finally:
            self.commands._semantic_search_plan = original_search
            self.commands._collect = original_collect
            self.commands.vs = original_vs
        self.assertEqual(dry["status"], "dry_run")
        self.assertEqual(stale["status"], "blocked")

    def test_semantic_selection_clears_cross_layer_selection_and_reads_back(self):
        original_search = self.commands._semantic_search_plan
        original_collect = self.commands._collect
        original_vs = self.commands.vs
        state = {"old"}
        self.commands._semantic_search_plan = lambda _p: ({
            "status": "ok", "read_only": True, "search_token": "search",
            "matches": [{"object_id": "nested", "selection_id": "target-group"}],
            "match_count": 1}, None)
        self.commands._collect = lambda *_args: sorted(state)
        self.commands.vs = types.SimpleNamespace(
            GetFPathName=lambda: "/tmp/test.vwx",
            GetObjectUuid=lambda h: h,
            GetObjectByUuid=lambda oid: oid,
            SetDSelect=lambda h: state.discard(h),
            SetSelect=lambda h: state.add(h),
        )
        try:
            dry = self.commands.select_semantic_objects({"layer": "WORK", "query": "stud"})
            applied = self.commands.select_semantic_objects({
                "layer": "WORK", "query": "stud", "confirm": True,
                "plan_token": dry["plan_token"]})
        finally:
            self.commands._semantic_search_plan = original_search
            self.commands._collect = original_collect
            self.commands.vs = original_vs
        self.assertEqual(applied["status"], "ok")
        self.assertEqual(state, {"target-group"})

    def test_drawing_package_normalizes_useful_manifest_and_rejects_scripts(self):
        package, error = self.commands._normalize_drawing_package({
            "package": {
                "sheets": [{"name": "A-101", "title": "Booth", "dpi": 300}],
                "viewports": [{"name": "VP-PLAN", "sheet": "A-101",
                               "view": "plan", "scale": 48, "x": 2, "y": 3,
                               "layers": {"WORK-Frame": "visible"}}],
                "dimensions": [{"name": "DIM-W", "viewport": "VP-PLAN",
                                "mode": "horizontal", "x1": 0, "y1": 0,
                                "x2": 72, "y2": 0, "offset": 6}],
                "reports": [{"name": "CUT-LIST", "criteria": "(L='WORK-Frame')",
                             "columns": [{"header": "Name", "formula": "=N"}]}],
                "saved_views": [{"name": "QA-A-101"}],
            },
        })
        self.assertIsNone(error)
        self.assertEqual(package["viewports"][0]["view"], "plan")
        self.assertEqual(package["viewports"][0]["layers"]["WORK-Frame"], 0)
        self.assertEqual(package["dimensions"][0]["mode"], "horizontal")

        package, error = self.commands._normalize_drawing_package({
            "package": {"reports": [{"name": "BAD", "criteria": "(T=RECT)",
                         "columns": [{"header": "Bad",
                                      "formula": "=RUNSCRIPT('payload')"}]}]},
        })
        self.assertIsNone(package)
        self.assertIn("record-field lookup", error)

        package, error = self.commands._normalize_drawing_package({
            "package": {"viewports": [{"name": "BLANK", "sheet": "A-101"}]},
        })
        self.assertIsNone(package)
        self.assertIn("at least one visible/gray layer", error)

    def test_drawing_package_plan_classifies_create_only_artifacts(self):
        package, error = self.commands._normalize_drawing_package({
            "package": {
                "sheets": [{"name": "A-101"}],
                "viewports": [{"name": "VP-PLAN", "sheet": "A-101",
                               "layers": {"WORK": "visible"}}],
                "dimensions": [{"name": "DIM-1", "viewport": "VP-PLAN",
                                "x1": 0, "y1": 0, "x2": 10, "y2": 0}],
                "reports": [{"name": "REPORT", "criteria": "(T=RECT)",
                             "columns": [{"header": "Name", "formula": "=N"}]}],
                "saved_views": [{"name": "QA-A-101"}],
            },
        })
        self.assertIsNone(error)
        originals = (self.commands._drawing_sheet_map, self.commands.get_layers,
                     self.commands._class_names_all,
                     self.commands._saved_view_names_strict, self.commands.vs)
        self.commands._drawing_sheet_map = lambda: {}
        self.commands.get_layers = lambda _p: {
            "layers": [{"name": "WORK", "type": "design"}]}
        self.commands._class_names_all = lambda: []
        self.commands._saved_view_names_strict = lambda: []
        self.commands.vs = types.SimpleNamespace(GetObject=lambda _name: None)
        try:
            plan = self.commands._plan_drawing_package(package)
        finally:
            (self.commands._drawing_sheet_map, self.commands.get_layers,
             self.commands._class_names_all,
             self.commands._saved_view_names_strict, self.commands.vs) = originals
        self.assertEqual({row["state"] for rows in plan["sections"].values()
                          for row in rows}, {"create"})
        self.assertFalse(plan["summary"]["converged"])

    def test_compact_drawing_set_expands_standard_views_and_dimension_scopes(self):
        normalized, error = self.commands._normalize_drawing_set({
            "drawing_set": {
                "sheet": {"name": "A-101", "title": "Booth", "dpi": 300},
                "model_layer": "WORK-Frame", "name_prefix": "BOOTH-",
                "views": ["plan", "front", "right", "right_iso"], "scale": 48,
                "layout": {"x": 4, "y": 8, "columns": 2,
                           "x_spacing": 8, "y_spacing": 6},
                "visible_layers": ["WORK-Frame", "30-WORK-SKIN"],
            },
        })
        self.assertIsNone(error)
        self.assertEqual([row["name"] for row in normalized["package"]["viewports"]],
                         ["BOOTH-PLAN", "BOOTH-FRONT", "BOOTH-RIGHT", "BOOTH-RIGHT-ISO"])
        self.assertEqual([(row["x"], row["y"]) for row in normalized["package"]["viewports"]],
                         [(4, 8), (12, 8), (4, 2), (12, 2)])
        self.assertEqual([row["plane"] for row in normalized["dimension_scopes"]],
                         ["plan", "front", "side"])

    def test_compact_drawing_set_blocks_stale_token(self):
        original = self.commands._plan_drawing_set
        self.commands._plan_drawing_set = lambda _p: ({
            "status": "dry_run", "plan_token": "fresh"}, None)
        try:
            result = self.commands.compile_drawing_set({
                "confirm": True, "plan_token": "stale"})
        finally:
            self.commands._plan_drawing_set = original
        self.assertEqual(result["status"], "blocked")

    def test_compact_drawing_set_refuses_unconverged_final_verification(self):
        originals = {
            "plan": self.commands._plan_drawing_set,
            "selected": self.commands._selected_object_ids,
            "restore": self.commands._restore_selection_ids,
            "ensure": self.commands.ensure_drawing_package,
            "sync": self.commands.sync_drawing_package,
            "vs": self.commands.vs,
        }
        self.commands._plan_drawing_set = lambda _p: ({
            "status": "dry_run", "plan_token": "fresh",
            "drawing_set": {"package": {}, "dimension_scopes": []},
        }, None)
        self.commands._selected_object_ids = lambda: ([], None)
        self.commands._restore_selection_ids = lambda _ids: {"restored": True}
        self.commands.ensure_drawing_package = lambda p: ({
            "status": "ok", "plan_token": "package", "changed": False,
        } if p.get("confirm") else {"status": "dry_run", "plan_token": "package"})
        self.commands.sync_drawing_package = lambda p: ({
            "status": "ok", "plan_token": "sync", "changed": True,
            "converged": False, "verification": {"summary": {"package": {"converged": False}}},
        } if p.get("confirm") else {"status": "dry_run", "plan_token": "sync"})
        self.commands.vs = types.SimpleNamespace(NameUndoEvent=lambda _name: None)
        try:
            result = self.commands.compile_drawing_set({
                "confirm": True, "plan_token": "fresh"})
        finally:
            self.commands._plan_drawing_set = originals["plan"]
            self.commands._selected_object_ids = originals["selected"]
            self.commands._restore_selection_ids = originals["restore"]
            self.commands.ensure_drawing_package = originals["ensure"]
            self.commands.sync_drawing_package = originals["sync"]
            self.commands.vs = originals["vs"]
        self.assertEqual(result["status"], "partial")
        self.assertIn("did not converge", result["error"])

    def test_create_viewport_uses_one_arg_api_without_layer_activation(self):
        calls = []
        state = {"scale": None}
        fake = types.SimpleNamespace(
            GetLayerByName=lambda name: "sheet" if name == "A-101" else None,
            GetObjectVariableInt=lambda h, selector: 2 if (h, selector) == ("sheet", 154) else 0,
            CreateVP=lambda parent: calls.append(("CreateVP", parent)) or "vp",
            GetObject=lambda _name: None,
            SetName=lambda h, name: calls.append(("SetName", h, name)),
            SetObjectVariableReal=lambda h, selector, value: state.update(scale=value),
            UpdateVP=lambda h: calls.append(("UpdateVP", h)),
            GetBBox=lambda h: ((-1.0, 1.0), (1.0, -1.0)),
            HMove=lambda h, x, y: calls.append(("HMove", h, x, y)),
            GetTypeN=lambda h: 122,
            GetLayer=lambda h: "sheet",
            GetLName=lambda h: "A-101",
            GetObjectVariableReal=lambda h, selector: state["scale"],
            GetObjectUuid=lambda h: "vp-uuid",
        )
        original_vs = self.commands.vs
        self.commands.vs = fake
        try:
            result = self.commands.create_viewport({
                "sheet_layer": "A-101", "name": "VP-PLAN",
                "x": 4, "y": 5, "scale": 48,
            })
        finally:
            self.commands.vs = original_vs
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls[0], ("CreateVP", "sheet"))
        self.assertIn(("HMove", "vp", 4.0, 5.0), calls)

    def test_resource_library_paths_report_truncation(self):
        with tempfile.TemporaryDirectory() as root:
            pathlib.Path(root, "a.vwx").touch()
            pathlib.Path(root, "b.vwx").touch()
            paths, error, truncated = self.commands._resource_library_paths(
                library_roots=[root], max_files=1)
        self.assertIsNone(error)
        self.assertEqual(len(paths), 1)
        self.assertTrue(truncated)

    def test_resource_plan_rejects_wrong_type_collision_and_ambiguity(self):
        with tempfile.NamedTemporaryFile(suffix=".vwx") as source:
            names = {301: ["Duplicate", "Duplicate"]}
            fake = types.SimpleNamespace(
                GetObject=lambda name: "wrong-h" if name == "Collision" else None,
                GetTypeN=lambda _h: 97,
                GetObjectUuid=lambda _h: "wrong-id",
                BuildResourceListN=lambda _type, _path: (301, 2),
                ResourceListSize=lambda list_id: len(names[list_id]),
                GetActualNameFromResourceList=lambda list_id, index: names[list_id][index - 1],
            )
            original_vs = self.commands.vs
            original_cache = self.commands._RESOURCE_INDEX_CACHE
            self.commands.vs = fake
            self.commands._RESOURCE_INDEX_CACHE = {}
            try:
                rows, error = self.commands._normalize_resource_items({
                    "items": [
                        {"kind": "symbol", "name": "Collision", "source_path": source.name},
                        {"kind": "symbol", "name": "Duplicate", "source_path": source.name},
                    ],
                })
                self.assertIsNone(error)
                plan = self.commands._plan_ensure_resources(rows)
            finally:
                self.commands.vs = original_vs
                self.commands._RESOURCE_INDEX_CACHE = original_cache
        self.assertEqual([row["state"] for row in plan], ["collision", "ambiguous"])

    def test_ensure_resources_blocks_stale_token_before_import(self):
        rows = [{"kind": "symbol", "type": 16, "name": "Chair", "source_path": None}]
        plan = [{"kind": "symbol", "type": 16, "name": "Chair",
                 "source_path": None, "state": "existing"}]
        originals = {
            "normalize": self.commands._normalize_resource_items,
            "plan": self.commands._plan_ensure_resources,
            "token": self.commands._resource_plan_token,
        }
        self.commands._normalize_resource_items = lambda _p: (rows, None)
        self.commands._plan_ensure_resources = lambda *_a, **_k: plan
        self.commands._resource_plan_token = lambda _rows, _plan: "fresh-token"
        try:
            result = self.commands.ensure_resources({
                "items": [{"kind": "symbol", "name": "Chair"}],
                "confirm": True, "plan_token": "stale-token",
            })
        finally:
            self.commands._normalize_resource_items = originals["normalize"]
            self.commands._plan_ensure_resources = originals["plan"]
            self.commands._resource_plan_token = originals["token"]
        self.assertEqual(result["status"], "blocked")
        self.assertIn("stale", result["error"])

    def test_batch_refs_resolve_nested_result_values(self):
        results = [{"object_id": "abc", "items": [{"center": {"z": 12.5}}]}]
        value = self.commands._resolve_batch_refs({
            "target": {"$ref": "0.object_id"},
            "nested": [{"$ref": "0.items.0.center.z"}],
        }, results)
        self.assertEqual(value, {"target": "abc", "nested": [12.5]})

    def test_batch_refs_fail_before_unavailable_or_missing_paths(self):
        with self.assertRaises(ValueError):
            self.commands._resolve_batch_refs({"$ref": "1.object_id"}, [{}])
        with self.assertRaises(ValueError):
            self.commands._resolve_batch_refs({"$ref": "0.missing"}, [{}])

    def test_bulk_transform_specs_normalize_partial_axes(self):
        rows, error = self.commands._normalize_bulk_transform_specs({
            "expected_count": 2,
            "items": [
                {"object_id": "one", "target_center": {"z": 12}},
                {"object_id": "two", "delta": {"x": -2, "y": 3}},
            ],
        })
        self.assertIsNone(error)
        self.assertEqual(rows[0], {
            "object_id": "one", "mode": "target_center",
            "target_center": {"z": 12.0},
        })
        self.assertEqual(rows[1]["delta"], {"x": -2.0, "y": 3.0})

    def test_bulk_transform_specs_reject_poorly_shaped_requests(self):
        rows, error = self.commands._normalize_bulk_transform_specs({
            "items": [{"object_id": "one", "target_center": {"z": 1},
                       "delta": {"z": 2}}],
        })
        self.assertIsNone(rows)
        self.assertIn("cannot set both", error)

    def test_workspace_snapshot_bounds_selection_and_skips_layers_by_default(self):
        originals = {
            name: getattr(self.commands, name) for name in (
                "get_selected_objects", "get_document_info", "get_document_units",
                "get_active_layer", "get_layers")
        }
        self.commands.get_selected_objects = lambda _p: {
            "count": 3, "objects": [{"id": 1}, {"id": 2}, {"id": 3}]}
        self.commands.get_document_info = lambda _p: {"name": "doc"}
        self.commands.get_document_units = lambda _p: {"name": "inches"}
        self.commands.get_active_layer = lambda _p: {"name": "Layer-1"}
        self.commands.get_layers = lambda _p: {"count": 99}
        try:
            result = self.commands.workspace_snapshot({"selection_limit": 2})
        finally:
            for name, fn in originals.items():
                setattr(self.commands, name, fn)
        self.assertEqual(result["selection"]["objects"], [{"id": 1}, {"id": 2}])
        self.assertEqual(result["selection"]["objects_omitted"], 1)
        self.assertIsNone(result["layers"])

    def test_pio_integrity_rejects_vectorworks_sentinel_centers(self):
        self.assertTrue(self.commands._finite_center({"x": 1, "y": 2, "z": 3}))
        self.assertFalse(self.commands._finite_center({"x": 3.937e98, "y": 2, "z": 3}))
        self.assertFalse(self.commands._finite_center({"x": float("inf"), "y": 2, "z": 3}))

    def test_pio_child_signature_tracks_finite_center_by_immediate_child(self):
        next_map = {"child-1": "child-2", "child-2": None}
        centers = {
            "child-1": ((10.0, 20.0), 30.0),
            "child-2": ((3.937e98, 3.937e98), 0.0),
        }
        fake = types.SimpleNamespace(
            FInGroup=lambda _h: "child-1",
            NextObj=lambda h: next_map[h],
            GetTypeN=lambda h: 11 if h == "child-1" else 84,
            Get3DCntr=lambda h: centers[h],
        )
        original_vs = self.commands.vs
        self.commands.vs = fake
        try:
            signature = self.commands._pio_child_integrity_signature("pio")
        finally:
            self.commands.vs = original_vs
        self.assertEqual(signature, [
            {"type": 11, "finite_center": True},
            {"type": 84, "finite_center": False},
        ])


if __name__ == "__main__":
    unittest.main()
