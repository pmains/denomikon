from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

@dataclass
class Meeting:
    meeting_date: str
    meeting_time: str
    meeting_title: str
    meeting_type: str
    body: str
    row_text: str
    detail_url: str
    agenda_url: str
    summary_url: str = ""
    minutes_url: str = ""
    video_url: str = ""

    @property
    def meeting_id(self) -> str:
        for url in (self.detail_url, self.agenda_url):
            # BOS format: /ViewMeeting?id=1234&doctype=1
            m = re.search(r"[?&]ID=(\d+)", url or "", re.I)
            if m:
                return m.group(1)
            # PZ format: /Agenda/_04232026-3722?html=true  or  /Agenda/3734
            m = re.search(r"/Agenda/[^/]*-(\d{3,})", url or "")
            if m:
                return m.group(1)
            m = re.search(r"/Agenda/(\d{3,})", url or "")
            if m:
                return m.group(1)
        return "meeting"


class _HtmlNode:
    def __init__(self, tag: str = "", attrs: Optional[dict[str, str]] = None, parent: Optional['_HtmlNode'] = None) -> None:
        self.tag = tag.lower()
        self.attrs = attrs or {}
        self.parent = parent
        self.children: list[_HtmlNode | str] = []


class _TreeBuilder(HTMLParser):
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("document")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        parent_node = self._stack[-1] if self._stack else None
        node = _HtmlNode(tag, {k.lower(): v or "" for k, v in attrs}, parent=parent_node)
        if parent_node:
            parent_node.children.append(node)
        if tag.lower() not in self._VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx].tag == tag:
                del self._stack[idx:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)

