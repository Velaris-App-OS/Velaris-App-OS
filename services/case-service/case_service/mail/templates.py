"""Template rendering — Jinja2 sandboxed + f-string-style {var}."""
from __future__ import annotations
import re
from typing import Any


class TemplateError(Exception):
    pass


_FSTRING_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


def _render_fstring(template: str, ctx: dict[str, Any]) -> str:
    def lookup(match):
        path = match.group(1).split(".")
        v: Any = ctx
        for part in path:
            if isinstance(v, dict):
                v = v.get(part, "")
            else:
                v = getattr(v, part, "")
            if v == "":
                return ""
        return str(v)
    return _FSTRING_RE.sub(lookup, template)


def _render_jinja2(template: str, ctx: dict[str, Any]) -> str:
    try:
        from jinja2.sandbox import SandboxedEnvironment
        from jinja2 import StrictUndefined, exceptions as j2exc
    except ImportError as e:
        raise TemplateError(f"Jinja2 not available: {e}")
    env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    try:
        tmpl = env.from_string(template)
        return tmpl.render(**ctx)
    except j2exc.TemplateError as e:
        raise TemplateError(f"Jinja2 render failed: {e}")
    except Exception as e:
        raise TemplateError(f"Render failed: {e}")


def render_template(
    template: str, ctx: dict[str, Any] | None = None, engine: str = "jinja2",
) -> str:
    ctx = ctx or {}
    engine = (engine or "jinja2").lower()
    if engine == "fstring":
        return _render_fstring(template, ctx)
    if engine == "jinja2":
        return _render_jinja2(template, ctx)
    raise TemplateError(f"Unknown template engine: {engine}")
