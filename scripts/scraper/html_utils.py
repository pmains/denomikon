from __future__ import annotations

import html
import re
from typing import Optional

from scraper.models import _HtmlNode, _TreeBuilder

__all__ = [
    "_parse_html",
    "_node_text",
    "_clean_html_text",
    "_closest_parent",
    "_find_all",
    "_has_class",
    "_search_results_table_present",
]

def _parse_html(html: str) -> _HtmlNode:
    parser = _TreeBuilder()
    parser.feed(html or "")
    parser.close()
    return parser.root


def _node_text(node: _HtmlNode | str) -> str:
    if isinstance(node, str):
        return node
    return " ".join(_node_text(child) for child in node.children)


def _clean_html_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _closest_parent(node: _HtmlNode, tag: str) -> Optional[_HtmlNode]:
    """Walk up the parent chain to find the nearest ancestor with the given tag."""
    current = node.parent
    while current:
        if current.tag == tag:
            return current
        current = current.parent
    return None


def _find_all(node: _HtmlNode, tag: Optional[str] = None) -> list[_HtmlNode]:
    found: list[_HtmlNode] = []
    wanted = tag.lower() if tag else None
    for child in node.children:
        if not isinstance(child, _HtmlNode):
            continue
        if wanted is None or child.tag == wanted:
            found.append(child)
        found.extend(_find_all(child, wanted))
    return found


def _find_one(node: _HtmlNode, tag: str) -> Optional[_HtmlNode]:
    """Return the first direct or descendant element matching *tag*."""
    wanted = tag.lower()
    if node.tag == wanted:
        return node
    for child in node.children:
        if not isinstance(child, _HtmlNode):
            continue
        result = _find_one(child, wanted)
        if result is not None:
            return result
    return None


def _has_class(node: _HtmlNode, class_name: str) -> bool:
    return class_name in (node.attrs.get("class") or "").split()


def _search_results_table_present(html: str) -> bool:
    root = _parse_html(html)
    for table in _find_all(root, "table"):
        table_text = _clean_html_text(_node_text(table)).lower()
        if all(token in table_text for token in ["meeting name", "meeting type", "meeting date", "links"]):
            return True
    return False

