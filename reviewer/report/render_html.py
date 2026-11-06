"""Self-contained static HTML view of a ScanReport.

No scripts, no external resources and a restrictive Content-Security-Policy: the report
embeds text derived from untrusted repositories, so every value is HTML-escaped and the page
cannot load or run anything even if an escape were missed.
"""

from __future__ import annotations

from html import escape

from reviewer.report.build import ScanReport
from reviewer.report.rank import RankedFinding

CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1b1f24;--muted:#59636e;--line:#d1d9e0;
--card:#f6f8fa;--add:#dafbe1;--del:#ffebe9;--crit:#82071e;--high:#cf222e;--med:#9a6700;
--low:#0969da;--info:#59636e;--ok:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;
--line:#30363d;--card:#161b22;--add:#12261e;--del:#2d1517;--crit:#ff7b72;--high:#ff7b72;
--med:#d29922;--low:#58a6ff;--info:#8d96a0;--ok:#3fb950}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif}
main{max-width:60rem;margin:0 auto;padding:1.5rem 1rem 4rem}
h1,h2,h3{line-height:1.25} h2{margin-top:2.5rem;border-bottom:1px solid var(--line);
padding-bottom:.3rem}
.meta,.muted{color:var(--muted)} code{font:.9em ui-monospace,monospace}
.finding{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:1rem 1.25rem;margin:1rem 0}
.finding h3{margin:0 0 .5rem}
.sev{display:inline-block;font-size:.75rem;font-weight:700;letter-spacing:.04em;
text-transform:uppercase;padding:.1rem .5rem;border-radius:999px;border:1px solid currentColor}
.sev-critical{color:var(--crit)}.sev-high{color:var(--high)}.sev-medium{color:var(--med)}
.sev-low{color:var(--low)}.sev-info{color:var(--info)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem;margin:.5rem 0}
dt{color:var(--muted)} dd{margin:0}
table{border-collapse:collapse;width:100%;margin:.5rem 0}
th,td{border:1px solid var(--line);padding:.35rem .6rem;text-align:left;vertical-align:top}
pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:.75rem;
overflow-x:auto;margin:.5rem 0}
pre span{display:block;white-space:pre}.add{background:var(--add)}.del{background:var(--del)}
.pass{color:var(--ok)}.fail{color:var(--high)}
summary{cursor:pointer;margin:.5rem 0}
a{color:var(--low)}
@media (max-width:40rem){dl{grid-template-columns:1fr}}
"""


def e(text: object) -> str:
    return escape(str(text), quote=True)


def _diff(diff: str) -> str:
    rows = []
    for line in diff.splitlines():
        cls = ""
        if line.startswith("+") and not line.startswith("+++"):
            cls = ' class="add"'
        elif line.startswith("-") and not line.startswith("---"):
            cls = ' class="del"'
        rows.append(f"<span{cls}>{e(line) or ' '}</span>")
    return f"<pre><code>{''.join(rows)}</code></pre>"


def _finding(r: RankedFinding) -> str:
    f = r.finding
    repo_level = f.symbol == "<repo>"
    where = f.file if repo_level else f"{f.file}:{f.line_start}-{f.line_end}"
    rows = [
        (
            "Location",
            f"<code>{e(where)}</code>" + ("" if repo_level else f" in <code>{e(f.symbol)}</code>"),
        ),
        ("Category", e(f.category)),
        ("Confidence", f"{f.confidence:.0%}"),
        ("Blast radius", str(r.blast_radius)),
        ("Score", f"{r.score:.2f}"),
    ]
    if f.related_files:
        rows.append(("Also involves", ", ".join(f"<code>{e(p)}</code>" for p in f.related_files)))
    if f.related_signals:
        rows.append(
            ("Static signals", ", ".join(f"<code>{e(s)}</code>" for s in f.related_signals))
        )
    if r.duplicates:
        rows.append(("Merged duplicates", str(len(r.duplicates))))
    meta = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)

    fix = ""
    if f.verification_status != "no_fix" and f.suggested_diff:
        checks = ""
        if r.verification is not None:
            items = "".join(
                f'<li><span class="{"pass" if c.status == "passed" else "fail" if c.status == "failed" else "muted"}">'
                f"{e(c.name)}: {e(c.status)}</span>"
                + (f" — {e(c.detail)}" if c.detail else "")
                + "</li>"
                for c in r.verification.checks
            )
            checks = f"<ul>{items}</ul>"
        fix = (
            f"<details><summary>Suggested fix — <code>{e(f.verification_status)}</code></summary>"
            f"{checks}{_diff(f.suggested_diff)}</details>"
        )
    return (
        f'<article class="finding" id="f{r.rank}">'
        f'<h3>{r.rank}. <span class="sev sev-{e(f.severity)}">{e(f.severity)}</span> {e(f.title)}</h3>'
        f"<dl>{meta}</dl><p>{e(f.explanation)}</p>"
        f"<p><strong>Evidence:</strong> <code>{e(f.evidence)}</code></p>{fix}</article>"
    )


def render_html(scan: ScanReport) -> str:
    run, s = scan.run, scan.summary
    sha = run.commit_sha[:12] + (" (uncommitted changes)" if run.dirty else "")
    sev_rows = "".join(f"<tr><td>{e(k)}</td><td>{v}</td></tr>" for k, v in s.by_severity.items())
    fixes = ""
    if run.fixes_attempted:
        fixes = (
            f"<p>Fixes: {run.fixes_verified} verified, {run.fixes_unverified} unverified, "
            f"{run.fixes_failed} failed, {run.fixes_not_generated} not generated. "
            "<em>Verified</em> means the fix applies and breaks nothing the sandbox could check; "
            "it does not prove the problem is gone.</p>"
        )
    body = "".join(_finding(r) for r in scan.findings) or (
        "<p>No findings. Review was precision-oriented: nothing met the confidence bar.</p>"
    )

    arch = ""
    if scan.architecture is not None:
        a = scan.architecture
        cycles = (
            "<ul>"
            + "".join(f"<li>{' ↔ '.join(f'<code>{e(p)}</code>' for p in c)}</li>" for c in a.cycles)
            + "</ul>"
            if a.cycles
            else "<p>No import cycles.</p>"
        )
        hubs = (
            "<p><strong>Most depended-on modules:</strong> "
            + ", ".join(f"<code>{e(p)}</code>" for p in a.hubs)
            + "</p>"
            if a.hubs
            else ""
        )
        rows = "".join(
            f"<tr><td><code>{e(m.path)}</code></td><td>{m.lines}</td><td>{m.imported_by}</td>"
            f"<td>{e(m.summary)}</td></tr>"
            for m in a.modules
        )
        arch = (
            f'<h2 id="architecture">Architecture</h2>{cycles}{hubs}'
            f"<table><tr><th>Module</th><th>Lines</th><th>Imported by</th><th>Summary</th></tr>{rows}</table>"
        )

    models = ", ".join(f"{e(k)}=<code>{e(v)}</code>" for k, v in run.models.items())
    trace = (
        f"<li>Full model trace: <code>{e(scan.trace_file)}</code></li>" if scan.trace_file else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        f"<title>Code review report</title><style>{CSS}</style></head><body><main>"
        "<h1>Code review report</h1>"
        f'<p class="meta"><code>{e(run.repo_root)}</code> · commit <code>{e(sha)}</code> · '
        f"run <code>{e(run.id)}</code> ({e(run.status)}) · generated {e(scan.generated_at)}</p>"
        f"<h2>Summary</h2><p>{s.total} finding(s) from {run.chunks_deep_reviewed} of "
        f"{run.chunks_total} functions deep-reviewed, ranked by severity × confidence × blast radius.</p>"
        f"<table><tr><th>Severity</th><th>Count</th></tr>{sev_rows}</table>{fixes}"
        f"<h2>Findings</h2>{body}{arch}"
        "<h2>Run details</h2><ul>"
        f"<li>Config version <code>{e(run.config_version)}</code>; models: {models}</li>"
        f"<li>Cost: ${run.cost_usd:.4f}</li>"
        f"<li>Dropped before ranking: {run.findings_dropped_low_confidence} below the confidence bar, "
        f"{run.findings_dropped_out_of_range} outside the reviewed function</li>{trace}</ul>"
        "</main></body></html>\n"
    )
