import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from tool_tags import PRESET_TOOLS, TOOL_TAGS, preset_names  # noqa: E402


class ToolPresetTests(unittest.TestCase):
    def test_explicit_tool_count_matches_tags_and_docs(self):
        source = (ROOT / "mcp-server" / "vwx_mcp_server.py").read_text()
        self.assertEqual(sum(line.strip() == "@vtool" for line in source.splitlines()), 290)
        self.assertEqual(len(TOOL_TAGS), 290)

    def test_mechanical_preset_is_focused_and_complete(self):
        tools = PRESET_TOOLS["mechanical"]
        self.assertEqual(len(tools), 78)
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

    def test_preset_names_are_exact_tool_names(self):
        names = preset_names("mechanical")
        self.assertEqual(len(names), 78)
        self.assertTrue(all(name in TOOL_TAGS for name in names))
        self.assertIsNone(preset_names("full"))
