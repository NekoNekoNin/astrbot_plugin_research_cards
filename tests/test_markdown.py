"""Ensure automatic conversion handles real tables rather than ordinary prose."""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "card_markdown", Path(__file__).resolve().parents[1] / "markdown_tables.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MarkdownTests(unittest.TestCase):
    def test_multiple_tables_keep_exact_spans_and_headings(self):
        text = "前言\n## 第一组\n|名称|数据|\n|:---|---:|\n|甲|0|\n\n中间解释\n## 第二组\n|指标|数值|\n|---|---|\n|乙|未核实|\n结论"
        tables, spans = module.extract_tables(text)
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0]["title"], "第一组")
        self.assertEqual(tables[0]["rows"], [["甲", "0"]])
        self.assertTrue(all(text[a:b].startswith("|") for a, b in spans))
        remaining = text
        for a, b in reversed(spans):
            remaining = remaining[:a] + remaining[b:]
        self.assertIn("中间解释", remaining)
        self.assertIn("结论", remaining)

    def test_code_examples_and_plain_prose_are_not_tables(self):
        text = (
            "```md\n|参数|数据|\n|---|---|\n|甲|10|\n```\n这是一句普通文字 | 不是表格"
        )
        self.assertEqual(module.extract_tables(text), ([], []))

    def test_escaped_and_code_span_pipes_preserve_cells(self):
        self.assertEqual(
            module.cells(r"| A\|B | `x|y` | **90/发** |"), ["A|B", "x|y", "90/发"]
        )

    def test_incomplete_table_is_not_removed(self):
        self.assertEqual(module.extract_tables("|甲|乙|\n|---|---|"), ([], []))


if __name__ == "__main__":
    unittest.main()
