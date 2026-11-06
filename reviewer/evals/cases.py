"""Eval cases: a clean repository plus a list of bugs to seed into it.

Ground truth is exact because we plant the bugs ourselves: each bug replaces a unique piece
of text, and its line range and enclosing function are computed from the mutated file. The
same case also gives a clean variant (no bugs) for measuring false positives.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from reviewer.index.parser import parse_python
from reviewer.llm.schemas import Category, Severity

Variant = Literal["buggy", "clean"]

# Present in a source checkout; installed copies must pass --cases.
DEFAULT_CASES_DIR = Path(__file__).resolve().parents[2] / "evals" / "cases"


class CaseError(ValueError):
    """A case definition is invalid."""


class BugSpec(BaseModel):
    id: str
    file: str
    old: str
    new: str
    category: Category
    severity: Severity
    title: str
    tests_detect: bool = False  # the case's own tests fail when this bug is present
    relevant: list[str] = Field(default_factory=list)  # "path::qualname" needed to see the bug


class AcceptableSpec(BaseModel):
    """A pattern that looks suspicious but is deliberate: findings on it are not errors."""

    file: str
    text: str
    note: str = ""


class CaseSpec(BaseModel):
    id: str
    description: str
    bugs: list[BugSpec] = Field(default_factory=list)
    acceptable: list[AcceptableSpec] = Field(default_factory=list)


class ResolvedBug(BaseModel):
    id: str
    case_id: str
    file: str
    category: Category
    severity: Severity
    title: str
    tests_detect: bool
    relevant: list[str]
    line_start: int
    line_end: int
    symbol: str  # qualname of the enclosing function


class AcceptableRange(BaseModel):
    file: str
    line_start: int
    line_end: int


class Materialized(BaseModel):
    case_id: str
    variant: Variant
    root: Path
    clean_dir: Path
    seeded: list[ResolvedBug]  # bugs present in this variant (empty for "clean")
    acceptable: list[AcceptableRange]
    tests_dir_present: bool


def load_case(case_dir: Path) -> CaseSpec:
    try:
        spec = CaseSpec.model_validate(yaml.safe_load((case_dir / "case.yaml").read_text()))
    except (OSError, ValueError) as err:
        raise CaseError(f"{case_dir.name}: {err}") from err
    if not (case_dir / "repo").is_dir():
        raise CaseError(f"{spec.id}: missing repo/ directory")
    return spec


def load_cases(cases_dir: Path, only: list[str] | None = None) -> list[tuple[Path, CaseSpec]]:
    if not cases_dir.is_dir():
        raise CaseError(f"cases directory not found: {cases_dir}")
    cases = [(d, load_case(d)) for d in sorted(cases_dir.iterdir()) if (d / "case.yaml").is_file()]
    if only:
        unknown = set(only) - {spec.id for _, spec in cases}
        if unknown:
            raise CaseError(f"unknown case id(s): {', '.join(sorted(unknown))}")
        cases = [(d, s) for d, s in cases if s.id in only]
    if not cases:
        raise CaseError(f"no cases found in {cases_dir}")
    return cases


def _enclosing_function(path: str, source: str, line: int) -> str:
    best: tuple[int, str] | None = None
    for sym in parse_python(path, source.encode()).symbols:
        if sym.kind in ("function", "method") and sym.start_line <= line <= sym.end_line:
            span = sym.end_line - sym.start_line
            if best is None or span < best[0]:
                best = (span, sym.qualname)
    return best[1] if best else "<module>"


def materialize(case_dir: Path, spec: CaseSpec, dest: Path, variant: Variant) -> Materialized:
    """Copy the clean repo to `dest` and, for the buggy variant, plant the bugs."""
    clean_dir = case_dir / "repo"
    if dest.exists():
        shutil.rmtree(dest)  # `dest` is a scratch directory this tool owns
    shutil.copytree(clean_dir, dest)

    texts: dict[str, str] = {}
    if variant == "buggy":
        for bug in spec.bugs:
            path = dest / bug.file
            if not path.is_file():
                raise CaseError(f"{spec.id}/{bug.id}: no such file {bug.file}")
            text = texts.get(bug.file) or path.read_text()
            if text.count(bug.old) != 1:
                raise CaseError(
                    f"{spec.id}/{bug.id}: 'old' must occur exactly once in {bug.file} "
                    f"(found {text.count(bug.old)})"
                )
            texts[bug.file] = text.replace(bug.old, bug.new, 1)
        for file, text in texts.items():
            (dest / file).write_text(text)

    seeded: list[ResolvedBug] = []
    if variant == "buggy":
        for bug in spec.bugs:
            text = texts[bug.file]
            if text.count(bug.new) != 1:
                raise CaseError(f"{spec.id}/{bug.id}: 'new' must occur exactly once after seeding")
            start = text[: text.index(bug.new)].count("\n") + 1
            end = start + bug.new.rstrip("\n").count("\n")
            seeded.append(
                ResolvedBug(
                    id=bug.id,
                    case_id=spec.id,
                    file=bug.file,
                    category=bug.category,
                    severity=bug.severity,
                    title=bug.title,
                    tests_detect=bug.tests_detect,
                    relevant=bug.relevant,
                    line_start=start,
                    line_end=end,
                    symbol=_enclosing_function(bug.file, text, start),
                )  # fmt: skip
            )

    acceptable = []
    for item in spec.acceptable:
        text = (dest / item.file).read_text()
        if item.text not in text:
            raise CaseError(f"{spec.id}: acceptable text not found in {item.file}: {item.text!r}")
        line = text[: text.index(item.text)].count("\n") + 1
        acceptable.append(
            AcceptableRange(file=item.file, line_start=max(1, line - 1), line_end=line + 1)
        )

    return Materialized(
        case_id=spec.id, variant=variant, root=dest, clean_dir=clean_dir, seeded=seeded,
        acceptable=acceptable, tests_dir_present=(dest / "tests").is_dir(),
    )  # fmt: skip
