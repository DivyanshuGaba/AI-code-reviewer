"""Write the four report formats for a scan."""

from __future__ import annotations

import json
from pathlib import Path

from reviewer.report.build import ScanReport
from reviewer.report.render_html import render_html
from reviewer.report.render_md import render_markdown
from reviewer.report.render_sarif import render_sarif


def write_reports(scan: ScanReport, out_dir: Path) -> dict[str, Path]:
    """Write report.json (source of truth), report.md, report.sarif and report.html."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "json": ("report.json", scan.model_dump_json(indent=2) + "\n"),
        "markdown": ("report.md", render_markdown(scan)),
        "sarif": ("report.sarif", json.dumps(render_sarif(scan), indent=2) + "\n"),
        "html": ("report.html", render_html(scan)),
    }
    paths = {}
    for fmt, (name, content) in files.items():
        path = out_dir / name
        path.write_text(content, encoding="utf-8")
        paths[fmt] = path
    return paths
