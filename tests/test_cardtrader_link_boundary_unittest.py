import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYNC_ROUTES = ROOT / "app" / "api" / "v1" / "routes" / "sync.py"


class CardTraderLinkBoundaryTest(unittest.TestCase):
    def test_link_only_writes_sync_owned_settings(self) -> None:
        """Linking must work with the restricted production Sync DB role."""

        source = SYNC_ROUTES.read_text(encoding="utf-8")
        tree = ast.parse(source)
        setup_function = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "setup_test_user"
        )
        function_source = ast.get_source_segment(source, setup_function)

        self.assertIsNotNone(function_source)
        self.assertIn("user_sync_settings", function_source)
        self.assertNotIn("mkt_sync_config", function_source)


if __name__ == "__main__":
    unittest.main()
