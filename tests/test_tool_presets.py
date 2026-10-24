import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from tool_tags import PRESET_TOOLS, TOOL_TAGS, preset_names  # noqa: E402


class ToolPresetTests(unittest.TestCase):
    def test_explicit_tool_count_matches_tags_and_docs(self):
        source = (ROOT / "mcp-server" / "vwx_mcp_server.py").read_text()
        self.assertEqual(sum(line.strip() == "@vtool" for line in source.splitlines()), 334)
        self.assertEqual(len(TOOL_TAGS), 334)

    def test_mechanical_preset_is_focused_and_complete(self):
        tools = PRESET_TOOLS["mechanical"]
        self.assertEqual(len(tools), 79)
        self.assertLessEqual(tools, TOOL_TAGS.keys())

    def test_mechanical_preset_keeps_guards_and_escape_hatches(self):
        tools = PRESET_TOOLS["mechanical"]
        self.assertLessEqual({
            "workspace_snapshot",
            "assembly_qa_bundle",
            "bulk_transform_objects",
            "create_verified_native_boxes",
            "reconcile_verified_native_boxes",
            "find_library_resources",
            "ensure_resources",
            "ensure_project_standards",
            "ensure_drawing_package",
            "compile_vw_project",
            "list_assembly_templates",
            "expand_assembly_template",
            "semantic_object_search",
            "select_semantic_objects",
            "index_document",
            "search_document",
            "compile_document_changes",
            "plan_auto_dimensions",
            "sync_drawing_package",
            "compile_drawing_set",
            "get_revision_snapshot",
            "manage_revision_baseline",
            "get_revision_diff",
            "ensure_revision_report",
            "qa_autopilot",
            "edit_components",
            "place_member_between_faces_guarded",
            "vwx_batch",
            "execute_script",
            "save_document",
        }, tools)

    def test_interior_preset_exists_and_is_valid(self):
        interior = PRESET_TOOLS["interior"]
        self.assertEqual(len(interior), 142)
        self.assertLessEqual(interior, TOOL_TAGS.keys())

    def test_interior_preset_covers_casework_verbs(self):
        interior = PRESET_TOOLS["interior"]
        self.assertLessEqual({
            "get_spaces", "get_walls", "create_wall", "create_space",
            "create_slab", "join_walls", "add_symbol_to_wall",
            "list_components", "insert_component",
            "get_record_formats", "create_record_format",
            "create_report_worksheet", "export_pdf",
            "get_viewports", "create_viewport",
            "execute_script", "vwx", "vwx_batch",
            "create_verified_native_boxes", "reconcile_verified_native_boxes",
            "create_verified_loft", "qa_autopilot", "get_revision_snapshot",
        }, interior)

    def test_interior_preset_excludes_site_and_landscape(self):
        interior = PRESET_TOOLS["interior"]
        # Site and landscape verbs have no place in a casework workflow
        for excluded in ("get_site_model_info", "update_site_model",
                         "get_terrain_elevation", "send_to_surface",
                         "get_plants", "create_plant", "update_plant"):
            self.assertNotIn(excluded, interior,
                             msg=f"{excluded!r} should not appear in interior preset")

    def test_preset_names_are_exact_tool_names(self):
        names = preset_names("mechanical")
        self.assertEqual(len(names), 79)
        self.assertTrue(all(name in TOOL_TAGS for name in names))
        self.assertIsNone(preset_names("full"))
        # interior is also an exact-name preset
        interior_names = preset_names("interior")
        self.assertEqual(len(interior_names), 142)
        self.assertTrue(all(name in TOOL_TAGS for name in interior_names))
