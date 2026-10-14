"""Formatting: ChatGPT markdown -> Telegram HTML (streaming/fallback)
and -> Bot API 10.1+/10.3 rich-message blocks (native headings, tables,
quotes, dividers, buttons).

All converters are total: on any unexpected input they degrade to plain
text rather than raising.
"""
import html
import re

# ---------------------------------------------------------------- HTML ---
_CODE = re.compile(r"```(\w*)\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_STRIKE = re.compile(r"~~(.+?)~~")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _inline_html(s: str) -> str:
    s = html.escape(s, quote=False)
    s = _INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", s)
    s = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", s)
    s = _ITALIC.sub(lambda m: f"<i>{m.group(1)}</i>", s)
    s = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", s)
    s = _LINK.sub(lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', s)
    return s


def md_to_html(md: str) -> str:
    """ChatGPT markdown -> Telegram HTML. Never raises."""
    try:
        parts: list = []
        pos = 0
        for m in _CODE.finditer(md):
            parts.append(_inline_html(md[pos:m.start()]))
            code = html.escape(m.group(2).rstrip("\n"), quote=False)
            parts.append(f"<pre>{code}</pre>")
            pos = m.end()
        parts.append(_inline_html(md[pos:]))

        out: list = []
        for chunk in parts:
            for line in chunk.split("\n"):
                s = line.strip()
                if s.startswith("### "):
                    out.append(f"<b>{s[4:].strip()}</b>")
                elif s.startswith("## "):
                    out.append(f"<b>{s[3:].strip()}</b>")
                elif s.startswith("# "):
                    out.append(f"<b>{s[2:].strip()}</b>")
                elif re.match(r"^---+$", s):
                    out.append("——————")
                elif s.startswith("&gt; ") or s.startswith("> "):
                    q = s[2:].strip() if s.startswith("> ") else s[5:].strip()
                    out.append(f"<i>❝ {q}</i>")
                else:
                    out.append(line)
        text = "\n".join(out)
        # tables -> monospace block for readability
        text = re.sub(r"(?m)^\|.*\|\s*$",
                      lambda m: f"<pre>{html.escape(m.group(0), quote=False)}</pre>"
                      if "---" not in m.group(0) else "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() or "…"
    except Exception:
        return html.escape(md[:3900], quote=False)


def chunk_text(s: str, n: int = 4000) -> list:
    if len(s) <= n:
        return [s]
    out, cur = [], ""
    for para in s.split("\n\n"):
        if len(cur) + len(para) + 2 > n and cur:
            out.append(cur)
            cur = ""
        cur = (cur + "\n\n" + para) if cur else para
    if cur:
        out.append(cur)
    # hard split any monster chunk
    final = []
    for c in out:
        while len(c) > n:
            final.append(c[:n])
            c = c[n:]
        final.append(c)
    return final


# ------------------------------------------------------- rich blocks ---
# Bot API 10.1+ sendRichMessage / editMessageText(rich_message).
# Shapes follow the 10.2/10.3 changelog (blocks, is_compact tables,
# expandable_blockquote, buttons). If Telegram rejects them we fall back
# to HTML, so being conservative is fine.

def _rt_inline(s: str):
    """Inline markdown -> RichText (str | dict | list)."""
    # tokenise **bold**, *italic*, `code`, [t](u), ~~strike~~
    pat = re.compile(r"(\*\*.+?\*\*|\*[^*\n]+?\*|`[^`\n]+?`|~~.+?~~|\[[^\]]+\]\([^)\s]+\))")
    out: list = []
    pos = 0
    for m in pat.finditer(s):
        if m.start() > pos:
            out.append(s[pos:m.start()])
        t = m.group(0)
        if t.startswith("**"):
            out.append({"type": "bold", "text": _rt_inline(t[2:-2])})
        elif t.startswith("*"):
            out.append({"type": "italic", "text": _rt_inline(t[1:-1])})
        elif t.startswith("`"):
            out.append({"type": "code", "text": t[1:-1]})
        elif t.startswith("~~"):
            out.append({"type": "strikethrough", "text": _rt_inline(t[2:-2])})
        else:
            lm = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", t)
            if lm:
                out.append({"type": "url", "text": lm.group(1), "url": lm.group(2)})
            else:
                out.append(t)
        pos = m.end()
    if pos < len(s):
        out.append(s[pos:])
    if not out:
        return ""
    if len(out) == 1:
        return out[0]
    return out


def _is_table_sep(line: str) -> bool:
    return bool(re.match(r"^\|?[\s:|-]+\|?[\s:|-]*$", line)) and "-" in line


def md_to_rich_blocks(md: str) -> list:
    """ChatGPT markdown -> list of InputRichBlock dicts. Never raises."""
    try:
        return _blocks(md)
    except Exception:
        return [{"type": "paragraph", "text": md[:3900]}]


def _blocks(md: str) -> list:
    blocks: list = []
    lines = md.split("\n")
    i, n = 0, len(lines)
    para: list = []

    def flush_para():
        if para:
            t = "\n".join(para).strip()
            if t:
                blocks.append({"type": "paragraph", "text": _rt_inline(t)})
            para.clear()

    while i < n:
        line = lines[i]
        s = line.strip()
        if s.startswith("```"):
            flush_para()
            lang = s[3:].strip()
            code = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            b: dict = {"type": "preformatted", "text": "\n".join(code)}
            if lang:
                b["language"] = lang
            blocks.append(b)
        elif s.startswith("# "):
            flush_para()
            blocks.append({"type": "section_heading", "text": _rt_inline(s[2:].strip())})
        elif s.startswith("## ") or s.startswith("### "):
            flush_para()
            t = s.lstrip("#").strip()
            blocks.append({"type": "section_heading", "text": _rt_inline(t)})
        elif re.match(r"^---+$", s):
            flush_para()
            blocks.append({"type": "divider"})
        elif s.startswith("> "):
            flush_para()
            q = []
            while i < n and lines[i].strip().startswith("> "):
                q.append(lines[i].strip()[2:])
                i += 1
            qt = "\n".join(q)
            if len(qt) > 280:
                blocks.append({"type": "expandable_blockquote",
                               "text": _rt_inline(qt[:1500])})
            else:
                blocks.append({"type": "blockquote", "text": _rt_inline(qt)})
            continue
        elif re.match(r"^(\d+[.)]|[-*])\s+", s):
            flush_para()
            items = []
            while i < n and re.match(r"^(\d+[.)]|[-*])\s+", lines[i].strip()):
                items.append(re.sub(r"^(\d+[.)]|[-*])\s+", "", lines[i].strip()))
                i += 1
            blocks.append({"type": "list",
                           "items": [{"text": _rt_inline(t)} for t in items]})
            continue
        elif s.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1].strip()):
            flush_para()
            header = [c.strip() for c in s.strip("|").split("|")]
            i += 2
            rows = [header]
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            rich_rows = []
            for r_i, r in enumerate(rows):
                rich_rows.append({
                    "cells": [{"text": _rt_inline(c), "align": "left",
                               "valign": "top", "is_header": r_i == 0} for c in r],
                })
            blocks.append({"type": "table", "is_compact": True, "rows": rich_rows})
            continue
        elif not s:
            flush_para()
        else:
            para.append(line)
        i += 1
    flush_para()
    return blocks or [{"type": "paragraph", "text": "…"}]


def rich_message_payload(blocks: list) -> dict:
    return {"blocks": blocks}
