"""HxStorefront content helpers — theme CSS rendering + Page Builder sanitisation.

`render_theme_css` turns a theme config JSON into CSS custom properties (the doc's
"rendered into CSS custom properties at build time").

`sanitize_sections` enforces key invariant 5: Custom HTML sections are sanitised at
SAVE time so a published storefront can never execute arbitrary JS. This is a
**deny-by-default allowlist** sanitiser built on the stdlib HTML parser (not a regex
blocklist): the document is tokenised, and only an explicit set of safe tags +
attributes is re-emitted — every other tag (`<script>`, `<iframe>`, `<object>`,
`<embed>`, `<form>`, `<style>`, `<link>`, `<meta>`, `<base>`, …), every `on*` handler,
and every non-http(s)/mailto URL is dropped. No external dependency.
"""
from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

# A small, safe subset of theme config groups → CSS variable prefixes.
_GROUPS = {
    "colors": "color", "colours": "color", "typography": "font",
    "layout": "layout", "spacing": "space",
}


def render_theme_css(config: dict) -> str:
    """Render a theme config dict into a `:root { --… }` CSS block. Unknown groups are
    emitted under their own prefix; scalar top-level keys become --brand-<key>."""
    lines: list[str] = [":root {"]
    config = config or {}
    for group, value in config.items():
        if isinstance(value, dict):
            prefix = _GROUPS.get(group, re.sub(r"[^a-z0-9]+", "-", str(group).lower()))
            for k, v in value.items():
                key = re.sub(r"[^a-z0-9]+", "-", str(k).lower())
                lines.append(f"  --{prefix}-{key}: {_css_value(v)};")
        else:
            key = re.sub(r"[^a-z0-9]+", "-", str(group).lower())
            lines.append(f"  --brand-{key}: {_css_value(value)};")
    lines.append("}")
    return "\n".join(lines)


def _css_value(v) -> str:
    # Defensive: strip anything that could break out of a declaration.
    return re.sub(r"[;{}<>]", "", str(v)).strip()


# Deny-by-default allowlist. Only these tags survive; everything else is dropped.
_ALLOWED_TAGS = {
    "p", "br", "hr", "span", "div", "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "strong", "b", "em", "i", "u", "s", "small", "sub", "sup",
    "a", "img", "figure", "figcaption", "table", "thead", "tbody", "tr", "td", "th",
}
_VOID_TAGS = {"br", "hr", "img"}
# Per-tag allowed attributes (everything else, incl. all on* handlers + style, dropped).
_ALLOWED_ATTRS = {
    "a": {"href", "title", "target", "rel"},
    "img": {"src", "alt", "title", "width", "height"},
    "*": {"class"},
}
_URL_ATTRS = {"href", "src"}
_SAFE_URL_RE = re.compile(r"^(https?:|mailto:|/|#|\./|\.\./)", re.IGNORECASE)


def _safe_url(value: str) -> bool:
    return bool(_SAFE_URL_RE.match((value or "").strip()))


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def _emit_start(self, tag: str, attrs, self_closing: bool) -> None:
        if tag not in _ALLOWED_TAGS:
            return  # drop the tag entirely (script/iframe/style/form/… never re-emitted)
        allowed = _ALLOWED_ATTRS.get(tag, set()) | _ALLOWED_ATTRS["*"]
        kept = []
        for name, value in attrs:
            name = (name or "").lower()
            if name.startswith("on") or name not in allowed:
                continue                      # drop event handlers + non-allowlisted attrs
            if name in _URL_ATTRS and not _safe_url(value or ""):
                continue                      # drop javascript:/data:/unknown-scheme URLs
            if name == "target":
                value = "_blank"
            kept.append(f'{name}="{escape(value or "", quote=True)}"')
        # Force rel=noopener on links that open a new tab.
        if tag == "a" and any(k.startswith("target=") for k in kept):
            kept = [k for k in kept if not k.startswith("rel=")] + ['rel="noopener noreferrer"']
        attr_str = (" " + " ".join(kept)) if kept else ""
        self.out.append(f"<{tag}{attr_str}{' /' if self_closing else ''}>")

    def handle_starttag(self, tag, attrs):
        self._emit_start(tag.lower(), attrs, self_closing=False)

    def handle_startendtag(self, tag, attrs):
        self._emit_start(tag.lower(), attrs, self_closing=tag.lower() in _VOID_TAGS)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        self.out.append(escape(data))

    def result(self) -> str:
        return "".join(self.out)


def sanitize_html(html: str) -> str:
    """Allowlist-sanitise a raw HTML string (deny-by-default). Returns HTML that
    contains only safe tags/attributes and no executable content."""
    if not html:
        return ""
    p = _Sanitizer()
    p.feed(html)
    p.close()
    return p.result()


def sanitize_sections(sections: list) -> list:
    """Sanitise the html of any custom-HTML Page Builder section (invariant 5)."""
    if not sections:
        return []
    cleaned = []
    for s in sections:
        if isinstance(s, dict) and s.get("type") in ("custom_html", "Custom HTML") and "html" in s:
            s = {**s, "html": sanitize_html(str(s.get("html", "")))}
        cleaned.append(s)
    return cleaned
