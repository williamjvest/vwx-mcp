"""Invariants of Will's macOS fork that an upstream merge must not break.

Added 2026-10-24 after the vw2027 merge silently dropped export_stl, left two
NameErrors (_class_names_used), swapped in a wrong-arity modal import_dwg, lost
document units, and made every macOS file-transport call fail VW_BRIDGE_DOWN.
All of it compiled and the old suite passed; these tests make each one fail.
"""
import ast
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMANDS = ROOT / "vwx-plugin" / "commands.py"
SERVER = ROOT / "mcp-server" / "vwx_mcp_server.py"
PUMP_BUILTINS = {"_batch", "_hold"}   # dispatched by vwx_pump itself


def load_commands():
    sys.modules.setdefault("vs", types.SimpleNamespace())
    sys.path.insert(0, str(COMMANDS.parent))
    spec = importlib.util.spec_from_file_location("vwx_commands_invariants", COMMANDS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vtool_cmd_targets(tree):
    targets = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(isinstance(d, ast.Name) and d.id == "vtool" for d in node.decorator_list):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "cmd" and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)):
                targets.setdefault(call.args[0].value, node.name)
    return targets


class SurfaceConsistencyTests(unittest.TestCase):
    def test_every_server_tool_has_a_bridge_command(self):
        server = ast.parse(SERVER.read_text())
        commands = ast.parse(COMMANDS.read_text())
        defined = {n.name for n in commands.body if isinstance(n, ast.FunctionDef)}
        missing = {t: tool for t, tool in _vtool_cmd_targets(server).items()
                   if t not in defined and t not in PUMP_BUILTINS}
        self.assertEqual(missing, {}, "tools whose bridge command is gone")

    def test_private_helpers_called_in_commands_exist(self):
        tree = ast.parse(COMMANDS.read_text())
        defined = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                defined.update((a.asname or a.name).split(".")[0] for a in node.names)
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id.startswith("_")}
        self.assertEqual(sorted(called - defined), [])

    def test_every_vs_call_exists_in_the_index(self):
        # A call to a vs function that does not exist raises at runtime. Nothing
        # flags it until that path runs: compile_document_changes called
        # vs.HMove3D for months, so every center update rolled back.
        index = json.loads((ROOT / "vwx-plugin" / "vs_index.json").read_text())
        tree = ast.parse(COMMANDS.read_text())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and isinstance(n.func.value, ast.Name) and n.func.value.id == "vs"}
        known_upstream = {"GetPolyClosed"}   # upstream terrain code, hasattr-guarded
        self.assertEqual(sorted(called - set(index) - known_upstream), [])

    def test_loader_is_ascii(self):
        (ROOT / "vwx-plugin" / "BridgeStart_MenuCommand.py").read_bytes().decode("ascii")


class CommandBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = load_commands()

    def _with_vs(self, **fns):
        original = self.c.vs
        self.c.vs = types.SimpleNamespace(**fns)
        self.addCleanup(setattr, self.c, "vs", original)

    def test_units_mm_uses_standard_preset(self):
        calls = []
        self._with_vs(Units=lambda i: calls.append(i))
        result = self.c.set_document_preferences({"units": "mm"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, [7])

    def test_unknown_units_rejected_without_calling_vectorworks(self):
        calls = []
        self._with_vs(Units=lambda i: calls.append(i))
        self.assertIn("error", self.c.set_document_preferences({"units": "furlongs"}))
        self.assertEqual(calls, [])

    def test_export_stl_rejects_relative_path_without_calling_vectorworks(self):
        calls = []
        self._with_vs(ExportSTL=lambda *a: calls.append(a))
        self.assertIn("error", self.c.export_stl({"path": "part.stl"}))
        self.assertEqual(calls, [])

    def test_export_stl_verifies_the_written_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "part.stl")

            def fake_export(p, binary, tess, scope):
                with open(p, "wb") as fh:
                    fh.write(b"\0" * 84)
                return True

            self._with_vs(ExportSTL=fake_export)
            result = self.c.export_stl({"path": path, "scope": "all"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["size"], 84)


class PumpHostYearTests(unittest.TestCase):
    """A 2027 Vectorworks must drain the 2027 queue, a 2026 one the 2026 queue."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        for year in ("2026", "2027"):
            os.makedirs(os.path.join(self.home, "Library", "Application Support",
                                     "Vectorworks", year, "Plug-ins", "VWX-MCP"))
        self.saved = {k: os.environ.get(k) for k in ("HOME", "APPDATA", "VWX_VW_VERSION")}
        os.environ["HOME"] = self.home
        os.environ.pop("APPDATA", None)
        os.environ.pop("VWX_VW_VERSION", None)
        self.saved_exe, self.saved_prefix = sys.executable, sys.prefix
        sys.modules["vs"] = types.SimpleNamespace()
        sys.path.insert(0, str(ROOT / "vwx-plugin"))
        spec = importlib.util.spec_from_file_location(
            "vwx_pump_invariants", ROOT / "vwx-plugin" / "vwx_pump.py")
        self.pump = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.pump)

    def tearDown(self):
        sys.executable, sys.prefix = self.saved_exe, self.saved_prefix
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _first_root(self, app_year):
        sys.executable = "/Applications/Vectorworks %s/Vectorworks %s.app/Contents/MacOS/Vectorworks" % (app_year, app_year)
        sys.prefix = "/Applications/Vectorworks %s/Vectorworks %s.app/Contents/Frameworks/Python.framework" % (app_year, app_year)
        return self.pump._vw_roots()[0]

    def test_vw2026_prefers_its_own_year(self):
        self.assertIn(os.path.join("Vectorworks", "2026", "Plug-ins"), self._first_root("2026"))

    def test_vw2027_prefers_its_own_year(self):
        self.assertIn(os.path.join("Vectorworks", "2027", "Plug-ins"), self._first_root("2027"))


class MacFileTransportTests(unittest.TestCase):
    """macOS has no native-palette heartbeat; missing heartbeat != bridge down."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SERVER.parent))
        import vwx_mcp_server as S
        cls.S = S

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = {k: getattr(self.S, k) for k in (
            "VWX_NATIVE_HEARTBEAT", "VWX_ALIVE_GRACE", "VWX_SOCKET_TIMEOUT")}
        self.saved_env = os.environ.get("VWX_PLUGIN_DIR")
        os.environ["VWX_PLUGIN_DIR"] = self.dir
        self.S.VWX_ALIVE_GRACE = 0.3
        self.S.VWX_SOCKET_TIMEOUT = 5.0

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.S, k, v)
        if self.saved_env is None:
            os.environ.pop("VWX_PLUGIN_DIR", None)
        else:
            os.environ["VWX_PLUGIN_DIR"] = self.saved_env

    def test_no_heartbeat_source_is_unknown_not_down(self):
        self.S.VWX_NATIVE_HEARTBEAT = False
        self.assertIsNone(self.S.VwxFileTransport().bridge_state()[0])

    def test_unclaimed_job_fails_fast_with_mac_code(self):
        self.S.VWX_NATIVE_HEARTBEAT = False
        t = self.S.VwxFileTransport()
        start = time.monotonic()
        result = t.send_command("ping")
        self.assertEqual(result["code"], "VW_JOB_UNCLAIMED")
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertEqual([n for n in os.listdir(t.jobs) if n.endswith(".json")], [])

    def test_claimed_job_outlives_the_grace_and_returns_its_result(self):
        self.S.VWX_NATIVE_HEARTBEAT = False
        t = self.S.VwxFileTransport()

        def fake_pump():
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                jobs = [n for n in os.listdir(t.jobs) if n.endswith(".json")]
                if jobs:
                    src = os.path.join(t.jobs, jobs[0])
                    with open(src) as fh:
                        cid = json.load(fh)["_cid"]
                    os.rename(src, src + ".working")
                    time.sleep(0.8)               # longer than the 0.3 s grace
                    with open(os.path.join(t.results, cid + ".json"), "w") as fh:
                        json.dump({"status": "ok", "pong": True}, fh)
                    os.remove(src + ".working")
                    return
                time.sleep(0.01)

        worker = threading.Thread(target=fake_pump)
        worker.start()
        result = t.send_command("ping")
        worker.join()
        self.assertEqual(result.get("pong"), True)

    def test_windows_still_fails_fast_when_palette_never_ran(self):
        self.S.VWX_NATIVE_HEARTBEAT = True
        self.assertEqual(self.S.VwxFileTransport().send_command("ping")["code"],
                         "VW_BRIDGE_DOWN")


if __name__ == "__main__":
    unittest.main()
