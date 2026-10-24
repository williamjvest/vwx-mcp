"""Tests for the four fix areas added in the 2026-08-09 batch:
  1. create_verified_loft — type-84 validation, bbox/center/volume checks, cleanup
  2. get_assembly_manifest — native texture readback for solid_loft / extrude
  3. qa_autopilot — named viewport update + worksheet recalculate compound path
  4. export_image fail-closed; modal exports return MODAL_DIALOG_REQUIRED
"""
import importlib.util
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMANDS_PATH = ROOT / "vwx-plugin" / "commands.py"
sys.path.insert(0, str(COMMANDS_PATH.parent))


def load_commands():
    sys.modules.setdefault("vs", types.SimpleNamespace())
    spec = importlib.util.spec_from_file_location("vwx_commands_fix_test", COMMANDS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifiedLoftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = load_commands()

    def _run_loft(self, result_handle=True, result_type=84,
                  bbox=((-1.0, -1.0), (1.0, 1.0)),
                  get3dinfo=(10.0, 5.0, 3.0),
                  get3dcntr=((0.0, 0.0), 5.0),
                  volume_result=(True, 150.0),
                  extra_params=None):
        """Run create_verified_loft with a faked vs module and stubbed _h."""
        deleted = []
        g_h = object()
        loft_h = object() if result_handle else None
        fake = types.SimpleNamespace(
            CreateLoftSurfaces=lambda g, ruled, closed, solid: loft_h,
            GetTypeN=lambda h: 11 if h is g_h else result_type,
            GetBBox=lambda _h: bbox,
            Get3DInfo=lambda _h: get3dinfo,
            Get3DCntr=lambda _h: get3dcntr,
            ObjVolume=lambda _h: (volume_result[1] if isinstance(volume_result, tuple)
                                  and volume_result[0] else volume_result),
            GetObjectUuid=lambda _h: "loft-uuid",
            GetName=lambda _h: "",
            SetName=lambda _h, n: None,
            DelObject=lambda _h: deleted.append(_h),
        )
        original_vs = self.commands.vs
        original_h = self.commands._h
        self.commands.vs = fake
        # stub _h so "grp" resolves to g_h without needing GetObjectByUuid
        self.commands._h = lambda oid: g_h if oid == "grp" else None
        params = {"group_id": "grp", **(extra_params or {})}
        try:
            result = self.commands.create_verified_loft(params)
        finally:
            self.commands.vs = original_vs
            self.commands._h = original_h
        return result, loft_h, g_h, deleted, fake

    def test_verified_loft_succeeds_on_type84(self):
        result, loft_h, _, _, _ = self._run_loft()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["type"], 84)
        self.assertEqual(result["type_name"], "solid_loft")
        self.assertIn("bbox", result)
        self.assertIn("center", result)
        self.assertEqual(result["volume"], 150.0)

    def test_verified_loft_rejects_wrong_type_and_deletes(self):
        result, loft_h, _, deleted, _ = self._run_loft(result_type=24)
        self.assertIn("error", result)
        self.assertIn("bad output deleted", result["error"])
        self.assertEqual(result["actual_type"], 24)
        self.assertEqual(result["expected_type"], 84)
        self.assertIn(loft_h, deleted)

    def test_verified_loft_rejects_degenerate_bbox(self):
        result, loft_h, _, deleted, _ = self._run_loft(bbox=((0.0, 0.0), (0.0, 0.0)))
        self.assertIn("error", result)
        self.assertIn("degenerate", result["error"])
        self.assertIn(loft_h, deleted)

    def test_verified_loft_rejects_zero_volume_and_deletes(self):
        result, loft_h, _, deleted, _ = self._run_loft(volume_result=(True, 0.0))
        self.assertIn("error", result)
        self.assertIn("non-positive", result["error"])
        self.assertIn(loft_h, deleted)

    def test_verified_loft_skips_volume_check_when_api_absent(self):
        result, _, _, deleted, fake = self._run_loft()
        del fake.ObjVolume
        # re-run without ObjVolume
        result, _, _, deleted, _ = self._run_loft()
        # volume check skipped: no deletion, result ok
        # (the first run above already passed; just confirm the impl handles absence)
        # We check by monkey-patching the module directly:
        original_vs = self.commands.vs
        original_h = self.commands._h
        g_h = object()
        loft_h = object()
        fake2 = types.SimpleNamespace(
            CreateLoftSurfaces=lambda *a: loft_h,
            GetTypeN=lambda h: 11 if h is g_h else 84,
            GetBBox=lambda _h: ((-1.0, -1.0), (1.0, 1.0)),
            Get3DInfo=lambda _h: (10.0, 5.0, 3.0),
            Get3DCntr=lambda _h: ((0.0, 0.0), 5.0),
            GetObjectUuid=lambda _h: "loft-uuid",
            GetName=lambda _h: "",
            SetName=lambda _h, n: None,
            DelObject=lambda _h: None,
            # No ObjVolume attribute
        )
        self.commands.vs = fake2
        self.commands._h = lambda oid: g_h if oid == "grp" else None
        try:
            result = self.commands.create_verified_loft({"group_id": "grp"})
        finally:
            self.commands.vs = original_vs
            self.commands._h = original_h
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["volume"])

    def test_verified_loft_deletes_source_when_requested(self):
        g_deleted = []
        original_vs = self.commands.vs
        original_h = self.commands._h
        g_h = object()
        loft_h = object()
        fake = types.SimpleNamespace(
            CreateLoftSurfaces=lambda *a: loft_h,
            GetTypeN=lambda h: 11 if h is g_h else 84,
            GetBBox=lambda _h: ((-1.0, -1.0), (1.0, 1.0)),
            Get3DInfo=lambda _h: (10.0, 5.0, 3.0),
            Get3DCntr=lambda _h: ((0.0, 0.0), 5.0),
            ObjVolume=lambda _h: 150.0,
            GetObjectUuid=lambda _h: "loft-uuid",
            GetName=lambda _h: "",
            SetName=lambda _h, n: None,
            DelObject=lambda _h: g_deleted.append(_h),
        )
        self.commands.vs = fake
        self.commands._h = lambda oid: (g_h if oid == "grp" and g_h not in g_deleted else None)
        try:
            result = self.commands.create_verified_loft({"group_id": "grp", "delete_source": True})
        finally:
            self.commands.vs = original_vs
            self.commands._h = original_h
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["source_group_deleted"])
        self.assertIn(g_h, g_deleted)

    def test_verified_loft_fails_without_group_id(self):
        result = self.commands.create_verified_loft({})
        self.assertIn("error", result)
        self.assertIn("group_id", result["error"])

    def test_verified_loft_fails_when_no_handle_returned(self):
        original_vs = self.commands.vs
        original_h = self.commands._h
        g_h = object()
        fake = types.SimpleNamespace(
            CreateLoftSurfaces=lambda *a: None,  # returns None -> no handle
            GetTypeN=lambda h: 11 if h is g_h else 0,
        )
        self.commands.vs = fake
        self.commands._h = lambda oid: g_h if oid == "grp" else None
        try:
            result = self.commands.create_verified_loft({"group_id": "grp"})
        finally:
            self.commands.vs = original_vs
            self.commands._h = original_h
        self.assertIn("error", result)
        self.assertIn("no handle", result["error"])


class ManifestTextureTests(unittest.TestCase):
    """get_assembly_manifest reports native texture for solid_loft (type 84)."""

    @classmethod
    def setUpClass(cls):
        cls.commands = load_commands()

    def _run_manifest_single(self, obj_type, rec_name, texture_ref, texture_name):
        """Run get_assembly_manifest with a single faked object of the given type."""
        sentinel = object()
        calls = {}
        fake = types.SimpleNamespace(
            GetLayerByName=lambda _name: sentinel,
            GetUnits=lambda: (0, 0, 0, 1.0),
            FInLayer=lambda _lh: sentinel,
            NextObj=lambda _h: None,
            GetTypeN=lambda _h: obj_type,
            GetName=lambda _h: "PART-01",
            GetClass=lambda _h: "UPHOLSTERY",
            GetSymName=lambda _h: "",
            GetObjectUuid=lambda _h: "uuid-part",
            GetBBox=lambda _h: ((-5.0, -10.0), (5.0, 10.0)),
            Get3DInfo=lambda _h: (20.0, 10.0, 3.0),
            Get3DCntr=lambda _h: ((0.0, 0.0), 5.0),
            GetParametricRecord=lambda _h: None,
            GetRField=lambda _h, rec, field: None,
            GetLName=lambda _lh: "WORK",
            GetLayer=lambda _h: sentinel,
            GetTextureRefN=lambda _h, part, layer, inherit: texture_ref,
            Index2Name=lambda ref: texture_name if ref == texture_ref else "",
        )
        original_vs = self.commands.vs
        self.commands.vs = fake
        try:
            result = self.commands.get_assembly_manifest({"layer": "WORK"})
        finally:
            self.commands.vs = original_vs
        return result

    def test_solid_loft_reports_native_texture(self):
        result = self._run_manifest_single(
            obj_type=84, rec_name=None, texture_ref=7, texture_name="Foam-Gray")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["objects"]), 1)
        self.assertEqual(result["objects"][0]["texture"], "Foam-Gray")

    def test_extrude_reports_native_texture(self):
        result = self._run_manifest_single(
            obj_type=24, rec_name=None, texture_ref=3, texture_name="Oak-Stain")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["objects"][0]["texture"], "Oak-Stain")

    def test_no_texture_ref_returns_none(self):
        result = self._run_manifest_single(
            obj_type=84, rec_name=None, texture_ref=0, texture_name="")
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["objects"][0]["texture"])


class QAAutopilotViewportWorksheetTests(unittest.TestCase):
    """qa_autopilot compound path: viewport update + worksheet recalculate."""

    @classmethod
    def setUpClass(cls):
        cls.commands = load_commands()

    def test_update_named_viewports_updates_existing_and_reports_missing(self):
        vp_h = object()
        updated = []
        fake = types.SimpleNamespace(
            GetObject=lambda name: vp_h if name == "VP-PLAN" else None,
            GetTypeN=lambda h: 122,  # viewport
            UpdateVP=lambda h: updated.append(h),
        )
        original_vs = self.commands.vs
        self.commands.vs = fake
        try:
            result = self.commands._qa_update_named_viewports(["VP-PLAN", "VP-MISSING"])
        finally:
            self.commands.vs = original_vs
        self.assertEqual(result["updated"], ["VP-PLAN"])
        self.assertEqual(result["missing"], ["VP-MISSING"])
        self.assertEqual(result["errors"], [])
        self.assertIn(vp_h, updated)

    def test_update_named_viewports_errors_on_wrong_type(self):
        fake = types.SimpleNamespace(
            GetObject=lambda name: object(),
            GetTypeN=lambda h: 11,  # group — not a viewport
            UpdateVP=lambda h: None,
        )
        original_vs = self.commands.vs
        self.commands.vs = fake
        try:
            result = self.commands._qa_update_named_viewports(["NOT-VP"])
        finally:
            self.commands.vs = original_vs
        self.assertEqual(result["errors"][0]["name"], "NOT-VP")
        self.assertIn("not a viewport", result["errors"][0]["error"])

    def test_recalculate_named_worksheets_recalcs_and_reads_back(self):
        ws_h = object()
        recalced = []
        size_val = [3, 2]
        fake = types.SimpleNamespace(
            GetObject=lambda name: ws_h if name == "CUT-LIST" else None,
            GetTypeN=lambda h: 94,  # worksheet
            RecalculateWS=lambda h: recalced.append(h),
            GetWSRowColumnCount=lambda h: size_val,
        )
        original_vs = self.commands.vs
        self.commands.vs = fake
        try:
            result = self.commands._qa_recalculate_named_worksheets(["CUT-LIST", "MISSING"])
        finally:
            self.commands.vs = original_vs
        self.assertEqual(len(result["recalculated"]), 1)
        self.assertEqual(result["recalculated"][0]["name"], "CUT-LIST")
        self.assertEqual(result["recalculated"][0]["rows"], 3)
        self.assertEqual(result["missing"], ["MISSING"])

    def test_qa_autopilot_blocks_on_viewport_update_failure(self):
        """When update_viewports has a missing viewport, qa_autopilot returns error."""
        original_plan = self.commands._plan_qa_autopilot
        original_update = self.commands._qa_update_named_viewports
        self.commands._plan_qa_autopilot = lambda _p: ({
            "status": "dry_run", "plan_token": "tok",
            "blocking": [], "fixable": [], "passed_state_checks": True,
            "selected_ids": [], "temporary_ids": [],
        }, None)
        self.commands._qa_update_named_viewports = lambda names: {
            "updated": [], "missing": ["VP-GONE"], "errors": []}
        try:
            result = self.commands.qa_autopilot({
                "confirm": True, "plan_token": "tok",
                "update_viewports": ["VP-GONE"],
            })
        finally:
            self.commands._plan_qa_autopilot = original_plan
            self.commands._qa_update_named_viewports = original_update
        self.assertEqual(result["status"], "error")
        self.assertIn("viewport", result["error"])

    def test_qa_autopilot_blocks_on_worksheet_recalc_failure(self):
        original_plan = self.commands._plan_qa_autopilot
        original_update = self.commands._qa_update_named_viewports
        original_recalc = self.commands._qa_recalculate_named_worksheets
        self.commands._plan_qa_autopilot = lambda _p: ({
            "status": "dry_run", "plan_token": "tok2",
            "blocking": [], "fixable": [], "passed_state_checks": True,
            "selected_ids": [], "temporary_ids": [],
        }, None)
        self.commands._qa_update_named_viewports = lambda names: {
            "updated": list(names), "missing": [], "errors": []}
        self.commands._qa_recalculate_named_worksheets = lambda names: {
            "recalculated": [], "missing": [],
            "errors": [{"name": "WS", "error": "RecalculateWS raised: boom"}]}
        try:
            result = self.commands.qa_autopilot({
                "confirm": True, "plan_token": "tok2",
                "recalculate_worksheets": ["WS"],
            })
        finally:
            self.commands._plan_qa_autopilot = original_plan
            self.commands._qa_update_named_viewports = original_update
            self.commands._qa_recalculate_named_worksheets = original_recalc
        self.assertEqual(result["status"], "error")
        self.assertIn("worksheet", result["error"])


class ExportFailClosedTests(unittest.TestCase):
    """export_image fails closed; modal exports return MODAL_DIALOG_REQUIRED."""

    @classmethod
    def setUpClass(cls):
        cls.commands = load_commands()

    def test_export_image_returns_error_without_object_id(self):
        # VW2027: ExportImageFile(hImage, filePath) exports Image OBJECTS, not
        # the drawing. Without an object_id the command fails honestly.
        result = self.commands.export_image({"path": "/tmp/out.png"})
        self.assertIn("error", result)
        self.assertIn("ExportImageFile", result["error"])
        self.assertEqual(result["code"], "MODAL_DIALOG_REQUIRED")

    def test_export_image_exports_image_object_when_object_id_supplied(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.png")
            fake_h = object()
            calls = []
            original_vs = self.commands.vs
            original_h = self.commands._h
            self.commands.vs = types.SimpleNamespace(
                ExportImageFile=lambda h, p: calls.append((h, p)))
            self.commands._h = lambda oid: fake_h
            try:
                result = self.commands.export_image(
                    {"path": path, "object_id": "some-uuid"})
            finally:
                self.commands.vs = original_vs
                self.commands._h = original_h
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, [(fake_h, path)])

    def test_export_pdf_returns_modal_required(self):
        result = self.commands.export_pdf({})
        self.assertEqual(result["code"], "MODAL_DIALOG_REQUIRED")
        self.assertIn("modal", result["detail"].lower())

    def test_export_shp_returns_modal_required(self):
        # vs.ExportSHP does not exist; the legacy exporter is modal.
        result = self.commands.export_shp({})
        self.assertEqual(result["code"], "MODAL_DIALOG_REQUIRED")

    def test_import_dwg_returns_modal_required_without_calling_vectorworks(self):
        # vs.ImportDXFDWG takes NO arguments and opens the import UI. Upstream
        # vw2027 calls it as (path, False): an arity error, which VW shows in
        # a modal Script Errors window that blocks the bridge. Never call it.
        calls = []
        original_vs = self.commands.vs
        self.commands.vs = types.SimpleNamespace(
            ImportDXFDWG=lambda *args: calls.append(args))
        try:
            result = self.commands.import_dwg({"path": "/tmp/test.dwg"})
        finally:
            self.commands.vs = original_vs
        self.assertEqual(result["code"], "MODAL_DIALOG_REQUIRED")
        self.assertEqual(calls, [])

    def test_export_dxf_returns_modal_required(self):
        # ExportDXFDWG_Batch does not exist; ExportDXFDWG is modal.
        self.assertEqual(self.commands.export_dxf({})["code"],
                         "MODAL_DIALOG_REQUIRED")
        self.assertEqual(self.commands.export_dxf({"path": "/tmp/out.dxf"})["code"],
                         "MODAL_DIALOG_REQUIRED")


if __name__ == "__main__":
    unittest.main()
