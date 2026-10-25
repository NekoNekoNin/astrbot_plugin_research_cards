"""Let the model choose visual reference cards after researching structured data."""

import asyncio
import json
import re
from urllib.parse import urlsplit

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .markdown_tables import extract_tables
from .renderer import CardError, render_card, validate_tables

GUIDANCE = """[资料呈现指导]
查阅工具返回的资料后，自行选择最便于阅读的呈现方式，不需要用户要求做表或发图。
当资料适合参数表、装备数据、排行榜、多对象比较或多组分类时，主动调用 research_table_image，
单个对象用参数资料卡，多个对象用对比表，不同类别分组。尤其绝地潜兵武器查询应优先采用这种方式。
在生成最终回复前，如果你准备输出 Markdown 表格，必须先把同一组数据传给 research_table_image，
让表格以图片呈现，不要直接用 Markdown 表格替代。工具成功后只补充简短分析，不再逐项重列数值。
只排入本轮实际查到或上下文已核实的数据，保留准确名称、单位、变体、版本及来源；缺失值用 null，
不要为了凑表推算未给出的伤害、穿甲、射速等数值，不把生成时间当作数据更新时间。
图后用少量文字解释关键结论并保留来源链接，避免重复整张表。普通闲聊或少量解释用文字即可。
用户明确说纯文字、不发图时遵循用户要求。工具 sent=1 才代表当前聊天的平台发送接口接受，勿重复发图。
[/资料呈现指导]"""


class ResearchCardsPlugin(Star):
    """Render deterministic, local table images for the current event only."""

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.slots = asyncio.Semaphore(2)

    @filter.on_llm_request(priority=45)
    async def add_presentation_guidance(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """Attach a small policy for automatic presentation without another model call."""
        if not self.config.get("enabled", True):
            return
        message = event.get_message_str() or ""
        message = re.sub(
            r"(?:不要|别|不想|不是|不用)(?:只用|只要|纯)(?:文字|文本)", "", message
        )
        event.set_extra(
            "_research_card_text_only",
            bool(
                re.search(
                    r"(?:只(?:用|要)|仅(?:用|要)?|纯)(?:文字|文本)|(?:不要|别|不需要|无需|不)(?:发|用|生成|发送)?(?:图|图片)|(?:用|以)文字(?:回答|回复)",
                    message,
                )
            ),
        )
        if "[资料呈现指导]" in (req.system_prompt or ""):
            return
        if self.config.get("automatic_presentation", True):
            policy = GUIDANCE
        else:
            policy = "[资料呈现指导]仅在用户明确要求资料图或表格图时使用 research_table_image。[/资料呈现指导]"
        req.system_prompt = f"{req.system_prompt or ''}\n{policy}".strip()

    @filter.on_llm_tool_respond(priority=45)
    async def remember_source_links(self, event, tool, tool_args, tool_result):
        """Keep canonical article URLs from this event's actual research results.

        Args:
            event: Current message event; references expire with this event.
            tool: Tool that just completed.
            tool_args: Unused research arguments.
            tool_result: Native CallToolResult containing JSON text blocks.
        """
        if (
            not self.config.get("enabled", True)
            or getattr(tool, "name", "") == "research_table_image"
        ):
            return
        known = list(event.get_extra("_research_card_sources") or [])
        for block in getattr(tool_result, "content", []) or []:
            text = getattr(block, "text", None)
            if not isinstance(text, str) or len(text) > 1000000:
                continue
            try:
                result = json.loads(text)
            except ValueError:
                continue
            if not isinstance(result, dict) or result.get("ok") is False:
                continue
            candidates = [result, result.get("data"), result.get("item")]
            for item in candidates:
                if not isinstance(item, dict) or not any(
                    k in item for k in ("text", "tables", "content")
                ):
                    continue
                url = (
                    item.get("revision_url")
                    or item.get("url")
                    or item.get("source_url")
                )
                title = item.get("title") or item.get("name") or "资料来源"
                if (
                    not isinstance(url, str)
                    or not isinstance(title, str)
                    or len(url) > 800
                ):
                    continue
                try:
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme != "https"
                        or not parsed.hostname
                        or parsed.username
                        or parsed.password
                    ):
                        continue
                except ValueError:
                    continue
                if not any(record["url"] == url for record in known):
                    timestamp = item.get("modified_at") or item.get("retrieved_at")
                    label = "页面修订：" if item.get("modified_at") else "资料获取："
                    known.append(
                        {
                            "title": title[:100],
                            "url": url,
                            "data_time": label + str(timestamp) if timestamp else "",
                        }
                    )
        event.set_extra("_research_card_sources", known[-12:])

    @filter.on_llm_response(priority=250)
    async def render_final_tables(self, event, response):
        """Convert a researched final table if the model omitted its image tool call.

        Args:
            event: Current message, including real sources collected during research.
            response: Final provider response before normal result delivery.
        """
        known = event.get_extra("_research_card_sources") or []
        if (
            not self.config.get("enabled", True)
            or not self.config.get("automatic_presentation", True)
            or not known
            or event.get_extra("_research_card_sent")
            or event.get_extra("_research_card_text_only")
        ):
            return
        text = getattr(response, "completion_text", "")
        if not isinstance(text, str) or len(text) > 40000:
            return
        groups, spans = extract_tables(text)
        if not groups:
            return
        title = " / ".join(record["title"] for record in known[-2:])[:120] or "资料参数"
        sources = "\n".join(
            record["title"] + " " + record["url"] for record in known[-2:]
        )
        data_time = "；".join(
            dict.fromkeys(record.get("data_time", "") for record in known[-2:])
        )[:240]
        result = json.loads(
            await self.research_table_image(
                event,
                title,
                json.dumps(groups, ensure_ascii=False),
                sources,
                data_time,
                "根据本轮实际查阅资料整理，参数口径、适用条件与结论见随图文字。",
            )
        )
        if result.get("sent") == 1:
            for start, end in reversed(spans):
                text = text[:start] + "（参数见资料图）\n" + text[end:]
            response.completion_text = text
            event.set_extra("_research_card_fallback", True)

    @filter.llm_tool(name="research_table_image")
    async def research_table_image(
        self,
        event: AstrMessageEvent,
        title: str,
        tables_json: str,
        sources: str = "",
        data_time: str = "",
        note: str = "",
    ):
        """Proactively send a local reference-card image when researched data is clearer as parameters, rankings, grouped records or comparisons, even without an explicit image request. Use only retrieved, verified data. Single records become vertical cards; wide tables are grouped automatically. Sent=1 means accepted in THIS chat; never send again with another tool. If it fails, answer in text.

        Args:
            title(string): Concise researched subject, with exact weapon or object names.
            tables_json(string): JSON array of 1-4 groups, each with title, columns (2-8 strings including units) and rows (arrays matching columns). Example: [{"title":"武器参数","columns":["参数","数据"],"rows":[["普通伤害","来源数值"],["耐久伤害",null]]}]. Use null for unavailable values. No Markdown or HTML needed.
            sources(string): Actual source titles and HTTPS URLs from research, not fabricated links.
            data_time(string): Source data timestamp, game patch or wiki revision; leave empty if unknown. Never use image creation time as data freshness.
            note(string): Short caveats, unit definitions or distinctions between variants, armor levels and body parts.
        """
        if not self.config.get("enabled", True):
            return json.dumps(
                {"ok": False, "sent": 0, "message": "资料图功能已关闭"},
                ensure_ascii=False,
            )
        if event.get_extra("_research_card_text_only"):
            return json.dumps(
                {
                    "ok": False,
                    "sent": 0,
                    "message": "用户要求纯文字，本条问题不发送资料图",
                },
                ensure_ascii=False,
            )
        if event.get_extra("_research_card_sent"):
            return json.dumps(
                {
                    "ok": False,
                    "sent": 0,
                    "already_sent": True,
                    "message": "本条问题已发送资料图，请直接补充文字结论，不要重复发图",
                },
                ensure_ascii=False,
            )
        if event.get_extra("_research_card_busy"):
            return json.dumps(
                {"ok": False, "sent": 0, "message": "当前问题的资料图正在生成"},
                ensure_ascii=False,
            )
        event.set_extra("_research_card_busy", True)
        try:
            max_rows = self.config.get("max_rows", 40)
            max_rows = min(60, max(1, max_rows)) if type(max_rows) is int else 40
            tables = validate_tables(tables_json, max_rows)
            known = event.get_extra("_research_card_sources") or []
            if known and isinstance(sources, str):
                matched = [
                    record
                    for record in known
                    if record["title"] in sources or record["url"] in sources
                ]
                references = matched or known
                canonical = []
                for record in references:
                    line = record["title"] + " " + record["url"]
                    if len("\n".join([*canonical, line])) > 2200:
                        break
                    canonical.append(line)
                sources = "\n".join(canonical)
            async with self.slots:
                payload, layout = await asyncio.to_thread(
                    render_card,
                    title,
                    tables,
                    sources,
                    data_time,
                    note,
                    self.config.get("theme", "light"),
                )
            caption = title
            if self.config.get("keep_source_links", True) and sources:
                caption += "\n资料来源：" + sources
            if data_time:
                caption += "\n数据时效 / 版本：" + data_time
            await event.send(MessageChain([Plain(caption), Image.fromBytes(payload)]))
            event.set_extra("_research_card_sent", True)
            return json.dumps(
                {
                    "ok": True,
                    "sent": 1,
                    **layout,
                    "message": "资料图已被当前聊天的平台接口接受，请用文字补充关键结论，无需再次发送",
                },
                ensure_ascii=False,
            )
        except CardError as error:
            return json.dumps(
                {"ok": False, "sent": 0, "message": str(error)}, ensure_ascii=False
            )
        except Exception:
            self.logger.exception("Reference-card rendering or delivery failed")
            return json.dumps(
                {
                    "ok": False,
                    "sent": 0,
                    "message": "资料图生成或发送失败，请保留文字回答，不要声称发图成功",
                },
                ensure_ascii=False,
            )
        finally:
            event.set_extra("_research_card_busy", False)
