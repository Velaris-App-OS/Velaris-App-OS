#!/usr/bin/env python3
"""Build the Velaris Operator Guide PDF from the Markdown sources (R7).

    ./scripts/build-operator-guide.sh       → docs/operator/Velaris-Operator-Guide.pdf

The Markdown files stay the source: they are what is edited, reviewed and
versioned. This joins them in reading order, adds a cover and a contents page,
and prints the result with headless Chrome. Run it whenever one of them changes
(the release checklist does).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/operator/Velaris-Operator-Guide.pdf"

CHAPTERS = [
    ("Runbook", "docs/operator/runbook.md"),
    ("Monitoring", "docs/operator/monitoring.md"),
    ("Recovery", "docs/recovery-runbook.md"),
    ("Getting help", "docs/operator/support.md"),
    ("Incidents", "docs/operator/incidents/README.md"),
    ("Security policy", "SECURITY.md"),
    ("Changelog", "CHANGELOG.md"),
]

CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
body { font-family: "Inter", "Segoe UI", Helvetica, Arial, sans-serif; font-size: 10pt;
       line-height: 1.5; color: #1d2733; }
h1 { font-size: 20pt; color: #0f5e57; margin: 0 0 10pt; page-break-before: always; }
h2 { font-size: 13pt; color: #0f5e57; margin: 16pt 0 6pt; border-bottom: 1px solid #d5e3e1;
     padding-bottom: 2pt; }
h3 { font-size: 11pt; margin: 12pt 0 4pt; }
code { font-family: "JetBrains Mono", Consolas, monospace; font-size: 8.6pt;
       background: #f1f5f6; padding: 0 2pt; border-radius: 2pt; }
pre { background: #f1f5f6; padding: 6pt 8pt; border-radius: 3pt; white-space: pre-wrap;
      word-break: break-word; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 8.8pt;
        page-break-inside: auto; }
th, td { border: 1px solid #d5e3e1; padding: 3pt 5pt; vertical-align: top; text-align: left; }
th { background: #eaf3f2; }
tr { page-break-inside: avoid; }
blockquote { border-left: 3px solid #0f5e57; margin: 6pt 0; padding: 2pt 8pt; color: #3b4a58; }
a { color: #0f5e57; text-decoration: none; }
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
.cover h1 { page-break-before: avoid; font-size: 30pt; margin-bottom: 4pt; }
.cover p { font-size: 12pt; color: #3b4a58; margin: 2pt 0; }
.toc h1 { page-break-before: always; }
.toc ol { font-size: 12pt; line-height: 2; }
"""


def _version() -> str:
    for line in (ROOT / "velaris.yaml").read_text().splitlines():
        m = re.match(r"^\s*version:\s*[\"']?([^\"'#\s]+)", line)
        if m:
            return m.group(1)
    return "unknown"


def _chapter(title: str, rel: str, n: int) -> str:
    text = (ROOT / rel).read_text()
    # The file's own top heading becomes the chapter heading.
    text = re.sub(r"\A\s*# .*\n", "", text, count=1)
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    return (f'<section id="ch{n}"><h1>{n}. {html.escape(title)}</h1>'
            f'<p style="color:#6b7a88;font-size:8.5pt">Source: <code>{html.escape(rel)}</code></p>'
            f"{body}</section>")


def build_html() -> str:
    toc = "".join(f'<li><a href="#ch{i}">{html.escape(t)}</a></li>'
                  for i, (t, _r) in enumerate(CHAPTERS, 1))
    chapters = "".join(_chapter(t, r, i) for i, (t, r) in enumerate(CHAPTERS, 1))
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Velaris Operator Guide</title>"
            f"<style>{CSS}</style></head><body>"
            f"<div class='cover'><h1>Velaris Operator Guide</h1>"
            f"<p>Install, run, monitor, recover and support a Velaris server.</p>"
            f"<p>Version {html.escape(_version())} · built {date.today().isoformat()}</p></div>"
            f"<div class='toc'><h1>Contents</h1><ol>{toc}</ol></div>"
            f"{chapters}</body></html>")


def _chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser"):
        if p := shutil.which(name):
            return p
    found = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    if found:
        return str(found[-1])
    sys.exit("No Chrome or Chromium found — install one to build the PDF.")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "guide.html"
        page.write_text(build_html())
        OUT.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox",
                        "--no-pdf-header-footer", f"--print-to-pdf={OUT}", page.as_uri()],
                       check=True, capture_output=True, timeout=120)
    print(f"Operator Guide: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
