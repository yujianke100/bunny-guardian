"""问答回答的 Markdown 渲染：转成 HTML 并做白名单清洗。

模型输出**不可信**（可能是被诱导的内容，也可能夹带 HTML），所以流程是
Markdown → 白名单清洗 → 直接插入页面。只允许排版类标签，脚本/样式/内嵌对象/
事件属性/非 http(s) 链接一律去掉；`<script>`/`<style>` 里的文本整段丢弃。

不引入任何第三方依赖，便于在只有 1GB 内存的小机器上自托管。
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

import markdown as md

EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]
ALLOWED = {
    "p", "br", "hr", "strong", "em", "del", "code", "pre", "ul", "ol", "li",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td", "a", "sup", "sub",
}
VOID = {"br", "hr"}
# 这些标签存在时，内容整段丢弃（不只是丢标签）
DROP_SUBTREE = {"script", "style", "iframe", "object", "embed", "svg", "math", "template"}


class _Cleaner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._drop = 0
        self._open: dict[str, int] = {}      # 实际输出过的标签，避免出现孤立的 </a> 之类

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in DROP_SUBTREE:
            self._drop += 1
            return
        if self._drop or tag not in ALLOWED:
            return
        if tag == "a":
            href = ""
            for k, v in attrs:
                if k.lower() == "href":
                    href = (v or "").strip()
            if not re.match(r"^https?://", href, re.I):
                return                      # 丢掉标签，文字保留
            self.out.append(f'<a href="{html.escape(href, quote=True)}" '
                            f'target="_blank" rel="noopener noreferrer">')
            self._open["a"] = self._open.get("a", 0) + 1
            return
        self.out.append(f"<{tag}>")
        self._open[tag] = self._open.get(tag, 0) + 1

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag in VOID and tag in ALLOWED:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_SUBTREE:
            if self._drop:
                self._drop -= 1
            return
        if self._drop or tag not in ALLOWED or tag in VOID:
            return
        if self._open.get(tag, 0) > 0:       # 只有真的开过才闭合
            self._open[tag] -= 1
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._drop and data:
            self.out.append(html.escape(data, quote=False))


def to_html(text: str) -> str:
    """Markdown 文本 → 可安全直接插入页面的 HTML。"""
    if not text:
        return ""
    try:
        raw = md.markdown(text, extensions=EXTENSIONS)
    except Exception:                                    # noqa: BLE001 - 渲染失败退回纯文本
        return "<p>" + html.escape(text).replace("\n", "<br>") + "</p>"
    c = _Cleaner()
    c.feed(raw)
    c.close()
    return "".join(c.out).strip()


def to_plain(text: str, limit: int = 400) -> str:
    """剥掉标记的纯文本版本（摘要/提示词里用）。"""
    s = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    s = re.sub(r"[*_`>#\-\[\]\(\)]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]
