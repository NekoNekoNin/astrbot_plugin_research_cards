"""Render bounded source data as local, deterministic PNG reference cards."""

import json
import math
import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


class CardError(ValueError):
    """Represent a displayable validation or layout failure."""


def validate_tables(tables_json: str, max_rows: int = 40) -> list[dict]:
    """Validate model-supplied tables without coercing or inventing numeric values.

    Args:
        tables_json: JSON array containing title, columns and rows per group.
        max_rows: Bound on original rows across every group.

    Returns:
        Normalized groups. Null cells are explicitly marked as unavailable.

    Raises:
        CardError: If the schema, dimensions or text limits are invalid.
    """
    if not isinstance(tables_json, str) or len(tables_json) > 40000:
        raise CardError("表格数据过长，请精简资料后重试")
    try:
        tables = json.loads(tables_json)
    except (ValueError, TypeError):
        raise CardError("tables_json 必须是合法 JSON 数组") from None
    if not isinstance(tables, list) or not 1 <= len(tables) <= 4:
        raise CardError("一次支持 1–4 组资料")
    total = 0
    result = []
    for table in tables:
        if not isinstance(table, dict):
            raise CardError("每组资料必须是包含 columns 和 rows 的对象")
        columns, rows = table.get("columns"), table.get("rows")
        if not isinstance(columns, list) or not 2 <= len(columns) <= 8:
            raise CardError("每组需要 2–8 列，请将指标单位写在表头或数值中")
        if not all(isinstance(c, str) and 0 < len(c.strip()) <= 60 for c in columns):
            raise CardError("表头必须为 1–60 字的文字")
        if not isinstance(rows, list) or not rows:
            raise CardError("每组至少需要一行实际资料")
        total += len(rows)
        if total > max_rows:
            raise CardError(f"资料超过 {max_rows} 行，请选择重点或减少对象数量")
        normalized = []
        for row in rows:
            if not isinstance(row, list) or len(row) != len(columns):
                raise CardError("每行的数据数量必须与表头一致，缺失数据使用 null")
            values = []
            for cell in row:
                if cell is None:
                    values.append("未核实")
                elif isinstance(cell, (str, int, float)) and not isinstance(cell, bool):
                    if isinstance(cell, float) and not math.isfinite(cell):
                        raise CardError("不支持 NaN 或无穷数值")
                    value = str(cell)
                    if len(value) > 240:
                        raise CardError("单元格超过 240 字，请把长说明放入图注")
                    values.append(value if value.strip() else "未提供")
                else:
                    raise CardError("单元格只接受文字、数字或 null")
            normalized.append(values)
        title = table.get("title", "资料参数")
        if not isinstance(title, str) or len(title) > 100:
            raise CardError("分组标题最多 100 字")
        # A single record is more readable as a vertical parameter card.
        if len(normalized) == 1 and len(columns) > 2:
            normalized = [[key, value] for key, value in zip(columns, normalized[0])]
            columns = ["参数", "数据"]
        result.append({"title": title, "columns": columns, "rows": normalized})
    return result


@lru_cache(maxsize=8)
def font(size: int, bold: bool = False):
    """Load a CJK font for reusable headings, cells and footnotes.

    Args:
        size: Font size in pixels.
        bold: Prefer a bold face.

    Returns:
        Pillow font with Chinese glyph support.

    Raises:
        CardError: If no supported CJK font is installed.
    """
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
        if bold
        else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    raise CardError("缺少中文字体，请安装微软雅黑或 Noto Sans CJK")


def wrap(text: str, face, width: int) -> list[str]:
    """Wrap cells and notes by measured glyph width, preserving all values.

    Args:
        text: Original display text.
        face: Pillow font.
        width: Available pixel width.

    Returns:
        Wrapped lines with original non-newline characters preserved.
    """
    lines = []
    for paragraph in text.replace("\r", "").split("\n"):
        current = ""
        tokens = re.findall(
            r"(?:[（(][A-Za-z0-9][A-Za-z0-9 ._+:/%?=&-]*[)）]|[A-Za-z0-9][A-Za-z0-9._+:/%?=&-]*|.)[)）\]】、，。！？；：,;!?]*",
            paragraph,
        )
        for token in tokens:
            units = [token] if face.getlength(token) <= width else list(token)
            for unit in units:
                if current and face.getlength(current + unit) > width:
                    lines.append(current)
                    current = ""
                current += unit
        lines.append(current)
    return lines


def render_card(
    title: str,
    tables: list[dict],
    sources: str = "",
    data_time: str = "",
    note: str = "",
    theme: str = "light",
) -> tuple[bytes, dict]:
    """Measure and render a reference card without fetching remote content.

    Args:
        title: Card title from researched material.
        tables: Validated groups.
        sources: Source labels and links as plain text.
        data_time: Actual data timestamp or version, never inferred from render time.
        note: Definitions and caveats.
        theme: Light or dark palette.

    Returns:
        PNG bytes and non-sensitive layout statistics.

    Raises:
        CardError: If text exceeds the bounded layout.
    """
    for value, limit in [(title, 120), (sources, 2200), (data_time, 240), (note, 1200)]:
        if not isinstance(value, str) or len(value) > limit:
            raise CardError("标题或说明超出图片容量，请保留重点并用文字补充来源")
    if not title.strip():
        raise CardError("资料图需要一个明确标题")
    dark = theme == "dark"
    bg, panel, ink, muted, line, alternate = (
        ("#111b29", "#192638", "#f1f5fa", "#b1bdcd", "#354459", "#202f43")
        if dark
        else ("#edf2f7", "#ffffff", "#142a42", "#4f647a", "#d9e2ec", "#f3f7fb")
    )
    width, left, content = 1200, 48, 1104
    heading, section, body, small = font(44, True), font(30, True), font(28), font(23)
    title_lines = wrap(title, heading, content)
    time_lines = wrap(
        "数据时效 / 版本：" + (data_time or "未提供，以来源标注为准"), small, content
    )
    y = 92 + 58 * len(title_lines) + 24 + 34 * len(time_lines) + 40
    groups = []
    for table in tables:
        columns = table["columns"]
        chunks = (
            [list(range(len(columns)))]
            if len(columns) <= 4
            else [
                [0, *range(start, min(start + 3, len(columns)))]
                for start in range(1, len(columns), 3)
            ]
        )
        for index, selected in enumerate(chunks):
            label = table["title"] or "资料参数"
            if len(chunks) > 1:
                label += f" · 指标组 {index + 1}/{len(chunks)}"
            label_lines = wrap(label, section, content)
            y += 44 * len(label_lines) + 16
            if len(selected) == 2:
                widths = [360, content - 360]
            else:
                first = min(
                    440,
                    max(
                        content // len(selected),
                        int(
                            max(
                                body.getlength(value)
                                for value in [
                                    columns[0],
                                    *[row[0] for row in table["rows"]],
                                ]
                            )
                        )
                        + 32,
                    ),
                )
                widths = [
                    first,
                    *[(content - first) // (len(selected) - 1)] * (len(selected) - 1),
                ]
                widths[-1] += content - sum(widths)
            prepared = []
            for raw in [
                [columns[i] for i in selected],
                *[[row[i] for i in selected] for row in table["rows"]],
            ]:
                cells = [
                    wrap(value, body, size - 32) for value, size in zip(raw, widths)
                ]
                height = max(len(cell) for cell in cells) * 40 + 28
                prepared.append((cells, height))
                y += height
            groups.append((label_lines, widths, prepared))
            y += 34
    note_lines = wrap("说明：" + note, small, content) if note else []
    display_sources = re.sub(
        r"https?://[^\s<>]+",
        lambda match: "〔" + match.group(0).split("/", 3)[2].split("@")[-1] + "〕",
        sources,
    )
    source_lines = wrap(
        "资料来源：" + (display_sources or "未提供，请以已核实资料为准"), small, content
    )
    height = y + 30 + 34 * (len(note_lines) + len(source_lines)) + 100
    if height > 7200:
        raise CardError("资料过长，无法清晰排在一张图中，请减少行或将说明放在文字回复")
    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, width - 24, height - 24), 24, fill=panel)
    draw.rounded_rectangle((left, 48, left + 8, 78), 4, fill="#16818c")
    draw.text((left + 24, 48), "RESEARCH BRIEF  /  资料速览", font=small, fill=muted)
    y = 92
    for text in title_lines:
        draw.text((left, y), text, font=heading, fill=ink)
        y += 58
    y += 24
    for text in time_lines:
        draw.text((left, y), text, font=small, fill=muted)
        y += 34
    y += 40
    for labels, widths, rows in groups:
        for text in labels:
            draw.text((left, y), text, font=section, fill=ink)
            y += 44
        y += 16
        for index, (cells, row_height) in enumerate(rows):
            fill = "#203c55" if index == 0 else (alternate if index % 2 else panel)
            draw.rectangle((left, y, left + content, y + row_height), fill=fill)
            x = left
            for cell, size in zip(cells, widths):
                for number, text in enumerate(cell):
                    draw.text(
                        (x + 16, y + 12 + number * 40),
                        text,
                        font=body,
                        fill="#ffffff" if index == 0 else ink,
                    )
                if x > left:
                    draw.line((x, y, x, y + row_height), fill=line)
                x += size
            y += row_height
            draw.line((left, y, left + content, y), fill=line)
        y += 34
    y += 30
    for text in [*note_lines, *source_lines]:
        draw.text((left, y), text, font=small, fill=muted)
        y += 34
    draw.text(
        (left, height - 76),
        "NekoNekoNin  ·  数值以资料来源为准",
        font=small,
        fill=muted,
    )
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue(), {
        "width": width,
        "height": height,
        "groups": len(tables),
        "rendered_sections": len(groups),
    }
