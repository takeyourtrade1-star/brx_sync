"""Regression gate for DOM-XSS sinks in the local operator dashboard."""

from pathlib import Path
import unittest


_DASHBOARD = Path(__file__).resolve().parents[2] / "static" / "index.html"


class StaticDashboardDomXssTests(unittest.TestCase):
    def test_dashboard_does_not_use_html_execution_sinks(self) -> None:
        source = _DASHBOARD.read_text(encoding="utf-8")

        forbidden = (
            ".innerHTML",
            ".outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "document.writeln",
            "eval(",
            "new Function(",
        )
        for sink in forbidden:
            with self.subTest(sink=sink):
                self.assertNotIn(sink, source)

    def test_api_data_is_rendered_through_dom_text_nodes(self) -> None:
        source = _DASHBOARD.read_text(encoding="utf-8")

        self.assertIn("element.textContent = text(value);", source)
        self.assertIn("element.replaceChildren(summary, wrapper);", source)
        self.assertIn("content.replaceChildren(template.content.cloneNode(true));", source)
        self.assertIn("editButton.addEventListener('click'", source)
        self.assertNotIn("onclick=\"openEditModal(${", source)


if __name__ == "__main__":
    unittest.main()
