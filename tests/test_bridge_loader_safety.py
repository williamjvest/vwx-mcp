import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class BridgeLoaderSafetyTests(unittest.TestCase):
    def test_menu_command_loader_is_ascii_only(self):
        # VW's embedded Python can open the external menu-command script with
        # the ASCII locale before Python sees an encoding declaration.
        source = ROOT / "vwx-plugin" / "BridgeStart_MenuCommand.py"
        source.read_bytes().decode("ascii")

    def test_dispatchers_reject_private_helpers(self):
        for filename in ("vwx_pump.py", "vwx_mcp_bridge.py"):
            source = (ROOT / "vwx-plugin" / filename).read_text()
            self.assertIn("startswith('_')", source)
            self.assertIn("cmd == '_batch'", source)


if __name__ == "__main__":
    unittest.main()
