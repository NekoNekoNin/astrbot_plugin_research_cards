"""Check data fidelity, bounded layouts and event-scoped delivery offline."""

import importlib.util
import json
import logging
import sys
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "research_cards_test_package"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(
    PACKAGE + ".renderer", ROOT / "renderer.py"
)
renderer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = renderer
spec.loader.exec_module(renderer)


class FakeStar:
    def __init__(self, context):
        self.context = context
        self.logger = logging.getLogger("reference-card-test")


class FakePlain:
    def __init__(self, text):
        self.text = text


class FakeImage:
    @staticmethod
    def fromBytes(data):
        return {"png": data}


class Event:
    def __init__(self):
        self.extra = {}
        self.sent = []
        self.send_error = False
        self.message = "查阅武器参数"

    def get_message_str(self):
        return self.message

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value

    async def send(self, chain):
        if self.send_error:
            raise RuntimeError("Adapter rejected the send")
        self.sent.append(chain)


stubs = {}
for name, attrs in {
    "astrbot.api.event": {
        "AstrMessageEvent": Event,
        "MessageChain": lambda chain: chain,
        "filter": types.SimpleNamespace(
            llm_tool=lambda **_: lambda handler: handler,
            on_llm_request=lambda **_: lambda handler: handler,
            on_llm_tool_respond=lambda **_: lambda handler: handler,
            on_llm_response=lambda **_: lambda handler: handler,
        ),
    },
    "astrbot.api.message_components": {"Image": FakeImage, "Plain": FakePlain},
    "astrbot.api.provider": {"ProviderRequest": object},
    "astrbot.api.star": {"Context": object, "Star": FakeStar},
}.items():
    stubs[name] = types.ModuleType(name)
    stubs[name].__dict__.update(attrs)
with patch.dict(sys.modules, stubs):
    spec = importlib.util.spec_from_file_location(PACKAGE + ".main", ROOT / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)


def data(columns=None, rows=None):
    return json.dumps(
        [
            {
                "title": "测试参数",
                "columns": columns or ["参数", "数据"],
                "rows": rows or [["真实零值", 0], ["缺失", None]],
            }
        ],
        ensure_ascii=False,
    )


class LayoutTests(unittest.TestCase):
    def test_zero_null_and_unit_preservation(self):
        tables = renderer.validate_tables(
            data(rows=[["伤害", 0], ["射速", "750 发/分钟"], ["缺失", None]])
        )
        self.assertEqual(
            tables[0]["rows"],
            [["伤害", "0"], ["射速", "750 发/分钟"], ["缺失", "未核实"]],
        )

    def test_single_record_transposes_without_losing_values(self):
        tables = renderer.validate_tables(
            data(["武器", "伤害", "耐久伤害"], [["示例武器", 90, None]])
        )
        self.assertEqual(tables[0]["columns"], ["参数", "数据"])
        self.assertEqual(
            tables[0]["rows"],
            [["武器", "示例武器"], ["伤害", "90"], ["耐久伤害", "未核实"]],
        )

    def test_invalid_shapes_and_nonfinite_numbers_rejected(self):
        for value in [
            "bad JSON",
            "{}",
            "[]",
            data(rows=[[1, 2, 3]]),
            data(rows=[["x", float("nan")]]),
            data(rows=[["x", float("inf")]]),
            data(rows=[["x", {"hidden": "value"}]]),
        ]:
            with self.subTest(value=value), self.assertRaises(renderer.CardError):
                renderer.validate_tables(value)

    def test_total_row_limit_across_groups(self):
        group = {"columns": ["参数", "数据"], "rows": [["x", 1]] * 21}
        with self.assertRaises(renderer.CardError):
            renderer.validate_tables(json.dumps([group, group]))

    def test_chinese_wrapping_keeps_all_characters(self):
        text = "伤害90，耐久伤害未核实；射速750发/分钟，版本1.2.3。" * 3
        face = renderer.font(28)
        lines = renderer.wrap(text, face, 250)
        self.assertEqual("".join(lines), text)
        self.assertTrue(all(face.getlength(line) <= 250 for line in lines))

    def test_wide_table_splits_without_hiding_metrics(self):
        tables = renderer.validate_tables(
            data(
                ["对象", "A", "B", "C", "D", "E", "F", "G"],
                [["一", 1, 2, 3, 4, 5, 6, 7], ["二", 0, None, 3, 4, 5, 6, 7]],
            )
        )
        png, layout = renderer.render_card("宽表测试", tables)
        self.assertEqual(layout["rendered_sections"], 3)
        image = Image.open(BytesIO(png))
        self.assertEqual(image.size, (layout["width"], layout["height"]))
        self.assertLessEqual(image.height, 7200)

    def test_weapon_models_and_parentheses_stay_together_when_wrapped(self):
        text = "穿甲“解放者”（AR-23P）"
        lines = renderer.wrap(text, renderer.font(28), 220)
        self.assertEqual("".join(lines), text)
        self.assertTrue(any("（AR-23P）" in line for line in lines))
        self.assertFalse(any(line == "）" for line in lines))

    def test_light_dark_multigroup_images_are_valid(self):
        tables = renderer.validate_tables(
            json.dumps(
                [
                    {
                        "title": "第一组",
                        "columns": ["对象", "数值"],
                        "rows": [["甲", 0], ["乙", 123]],
                    },
                    {
                        "title": "第二组",
                        "columns": ["参数", "内容"],
                        "rows": [["说明", "中文换行测试" * 10]],
                    },
                ]
            )
        )
        for theme in ["light", "dark"]:
            png, layout = renderer.render_card(
                "分组资料",
                tables,
                "测试来源 https://example.org/source",
                "测试版本",
                "仅排版测试",
                theme,
            )
            self.assertEqual(layout["groups"], 2)
            Image.open(BytesIO(png)).verify()

    def test_oversized_layout_fails_before_allocating_image(self):
        tables = renderer.validate_tables(data(rows=[["参数" * 80, "资料" * 80]] * 40))
        with self.assertRaises(renderer.CardError):
            renderer.render_card("过长的资料", tables)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_markdown_table_becomes_image_even_without_model_tool_call(
        self,
    ):
        plugin = main.ResearchCardsPlugin(None, {})
        event = Event()
        event.set_extra(
            "_research_card_sources",
            [
                {
                    "title": "真实武器资料",
                    "url": "https://example.org/weapon",
                    "data_time": "页面修订：测试版本",
                }
            ],
        )
        response = types.SimpleNamespace(
            completion_text="开头结论\n\n|参数|数值|\n|---|---|\n|伤害|90/发|\n|未知|未核实|\n\n结尾说明和来源"
        )
        await plugin.render_final_tables(event, response)
        self.assertEqual(len(event.sent), 1)
        self.assertIn("开头结论", response.completion_text)
        self.assertIn("结尾说明和来源", response.completion_text)
        self.assertNotIn("|伤害|", response.completion_text)
        self.assertTrue(event.get_extra("_research_card_fallback"))
        await plugin.render_final_tables(event, response)
        self.assertEqual(len(event.sent), 1)

    async def test_fallback_requires_sources_and_preserves_original_on_failure(self):
        plugin = main.ResearchCardsPlugin(None, {})
        event = Event()
        text = "|参数|数值|\n|---|---|\n|伤害|90|\n"
        response = types.SimpleNamespace(completion_text=text)
        await plugin.render_final_tables(event, response)
        self.assertEqual(event.sent, [])
        event.set_extra(
            "_research_card_sources", [{"title": "来源", "url": "https://example.org/"}]
        )
        event.send_error = True
        with self.assertLogs("reference-card-test", level="ERROR"):
            await plugin.render_final_tables(event, response)
        self.assertEqual(response.completion_text, text)
        self.assertEqual(event.sent, [])

    async def test_explicit_text_request_disables_tool_and_fallback(self):
        plugin = main.ResearchCardsPlugin(None, {})
        event = Event()
        event.message = "请查一下武器数据，只要文字，不要发图"
        await plugin.add_presentation_guidance(
            event, types.SimpleNamespace(system_prompt="")
        )
        result = json.loads(await plugin.research_table_image(event, "资料", data()))
        self.assertEqual(result["sent"], 0)
        self.assertEqual(event.sent, [])

    async def test_request_not_to_use_only_text_does_not_disable_images(self):
        plugin = main.ResearchCardsPlugin(None, {})
        for text in [
            "不要只用文字，发一张资料图",
            "不用纯文字，给个图",
            "别纯文字回答",
        ]:
            event = Event()
            event.message = text
            await plugin.add_presentation_guidance(
                event, types.SimpleNamespace(system_prompt="")
            )
            self.assertFalse(event.get_extra("_research_card_text_only"), text)

    async def test_source_links_use_actual_article_urls_instead_of_model_typo(self):
        plugin = main.ResearchCardsPlugin(None, {})
        event = Event()
        source = {
            "ok": True,
            "title": "测试武器",
            "text": "真实来源内容",
            "url": "https://example.org/weapon",
            "revision_url": "https://example.org/weapon?oldid=123",
        }
        result = types.SimpleNamespace(
            content=[types.SimpleNamespace(text=json.dumps(source))]
        )
        await plugin.remember_source_links(
            event, types.SimpleNamespace(name="helldivers_read"), {}, result
        )
        await plugin.research_table_image(
            event, "参数", data(), "测试武器 https://example.org/typo"
        )
        self.assertIn("https://example.org/weapon?oldid=123", event.sent[0][0].text)
        self.assertNotIn("/typo", event.sent[0][0].text)

    async def test_guidance_is_proactive_and_added_once(self):
        plugin = main.ResearchCardsPlugin(None, {})
        request = types.SimpleNamespace(system_prompt="原有设定")
        await plugin.add_presentation_guidance(Event(), request)
        await plugin.add_presentation_guidance(Event(), request)
        self.assertEqual(request.system_prompt.count("[资料呈现指导]"), 1)
        self.assertIn("不需要用户要求", request.system_prompt)
        self.assertTrue(request.system_prompt.startswith("原有设定"))

    async def test_automatic_off_requires_explicit_request_in_policy(self):
        plugin = main.ResearchCardsPlugin(None, {"automatic_presentation": False})
        request = types.SimpleNamespace(system_prompt="")
        await plugin.add_presentation_guidance(Event(), request)
        self.assertIn("仅在用户明确要求", request.system_prompt)

    async def test_sends_only_to_supplied_event_and_blocks_duplicate(self):
        plugin = main.ResearchCardsPlugin(None, {})
        current, other = Event(), Event()
        result = json.loads(
            await plugin.research_table_image(current, "测试资料", data())
        )
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(current.sent), 1)
        self.assertEqual(other.sent, [])
        Image.open(BytesIO(current.sent[0][1]["png"])).verify()
        repeated = json.loads(
            await plugin.research_table_image(current, "测试资料", data())
        )
        self.assertTrue(repeated["already_sent"])
        self.assertEqual(len(current.sent), 1)

    async def test_failed_delivery_does_not_claim_success_or_block_retry(self):
        plugin = main.ResearchCardsPlugin(None, {})
        event = Event()
        event.send_error = True
        with self.assertLogs("reference-card-test", level="ERROR"):
            result = json.loads(
                await plugin.research_table_image(event, "测试资料", data())
            )
        self.assertEqual(result["sent"], 0)
        self.assertFalse(event.get_extra("_research_card_sent"))
        self.assertFalse(event.get_extra("_research_card_busy"))
        event.send_error = False
        result = json.loads(
            await plugin.research_table_image(event, "测试资料", data())
        )
        self.assertEqual(result["sent"], 1)

    async def test_disabled_and_bad_data_do_not_send(self):
        event = Event()
        for settings, payload in [({"enabled": False}, data()), ({}, "invalid")]:
            plugin = main.ResearchCardsPlugin(None, settings)
            result = json.loads(
                await plugin.research_table_image(event, "测试资料", payload)
            )
            self.assertEqual(result["sent"], 0)
        self.assertEqual(event.sent, [])


if __name__ == "__main__":
    unittest.main()
