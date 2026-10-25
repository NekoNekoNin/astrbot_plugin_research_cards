"""Extract complete GFM tables while retaining surrounding explanation text."""

import re


def cells(line: str) -> list[str]:
    """Split a Markdown row without treating escaped or code-span pipes as columns.

    Args:
        line: One Markdown table row.

    Returns:
        Plain-text cells preserving numeric values and units.
    """
    values, current = [], ""
    code = False
    escaped = False
    for char in line.strip():
        if escaped:
            current += char if char == "|" else "\\" + char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            code = not code
        elif char == "|" and not code:
            values.append(current.strip())
            current = ""
        else:
            current += char
    values.append((current + ("\\" if escaped else "")).strip())
    if line.strip().startswith("|"):
        values = values[1:]
    if line.strip().endswith("|") and not line.strip().endswith("\\|"):
        values = values[:-1]
    return [re.sub(r"\*\*(.*?)\*\*", r"\1", value) for value in values]


def extract_tables(text: str) -> tuple[list[dict], list[tuple[int, int]]]:
    """Locate complete table groups outside fenced code blocks.

    Args:
        text: Final researched answer.

    Returns:
        Groups and exact character spans, leaving headings, sources and prose intact.
    """
    lines = text.splitlines(keepends=True)
    offsets, position = [], 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    groups, spans = [], []
    fence = None
    index = 0
    while index < len(lines) - 1:
        stripped = lines[index].strip()
        match = re.match(r"^(`{3,}|~{3,})", stripped)
        if match:
            token = match.group(1)
            if fence and token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            elif not fence:
                fence = token
            index += 1
            continue
        if fence or "|" not in stripped:
            index += 1
            continue
        headers, divider = cells(stripped), cells(lines[index + 1])
        if (
            not 2 <= len(headers) <= 8
            or len(divider) != len(headers)
            or not all(re.fullmatch(r":?-{3,}:?", value) for value in divider)
        ):
            index += 1
            continue
        end, rows = index + 2, []
        while end < len(lines) and "|" in lines[end]:
            row = cells(lines[end])
            if len(row) != len(headers):
                break
            rows.append(row)
            end += 1
        if not rows:
            index += 1
            continue
        heading = f"资料表格 {len(groups) + 1}"
        for line in reversed(lines[:index]):
            if line.strip().startswith("#"):
                heading = line.strip().lstrip("# ").replace("**", "")[:100]
                break
        groups.append({"title": heading, "columns": headers, "rows": rows})
        spans.append(
            (offsets[index], offsets[end] if end < len(offsets) else len(text))
        )
        index = end
    return groups, spans
