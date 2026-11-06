"""Result records for an eval run. JSON-serialisable so runs can be compared later."""

from __future__ import annotations

from pydantic import BaseModel, Field

from reviewer.evals.cases import Variant


class FixEval(BaseModel):
    attempted: bool = False
    exact: bool | None = None  # patched function's AST equals the clean function's AST
    status: str = "no_fix"  # the pipeline's verification status
    resolves: bool | None = None  # case tests: failing before, passing after (None = not measured)


class BugOutcome(BaseModel):
    case_id: str
    bug_id: str
    category: str
    severity: str
    function_reviewed: bool  # triage let the buggy function reach the deep review
    found: bool
    finding_id: str | None = None
    finding_confidence: float | None = None
    category_correct: bool | None = None
    fix: FixEval = Field(default_factory=FixEval)


class FalsePositive(BaseModel):
    finding_id: str
    file: str
    line_start: int
    title: str
    severity: str
    confidence: float


class CaseResult(BaseModel):
    case_id: str
    variant: Variant
    run_id: str
    run_status: str
    functions: int
    deep_reviewed: int
    stage_failures: int
    cost_usd: float
    bugs: list[BugOutcome] = Field(default_factory=list)
    false_positives: list[FalsePositive] = Field(default_factory=list)
    ignored_acceptable: int = 0
    architecture_findings: int = 0


class Rate(BaseModel):
    hit: int = 0
    total: int = 0

    @property
    def value(self) -> float | None:
        return self.hit / self.total if self.total else None


class Totals(BaseModel):
    bugs: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    # where recall is lost: triage never forwarded the function, or deep review missed it
    triage_selected: Rate = Field(default_factory=Rate)
    deep_found_given_selected: Rate = Field(default_factory=Rate)
    category_accuracy: Rate = Field(default_factory=Rate)
    recall_by_category: dict[str, Rate] = Field(default_factory=dict)
    recall_by_severity: dict[str, Rate] = Field(default_factory=dict)
    mean_confidence_tp: float | None = None
    mean_confidence_fp: float | None = None
    # false positives on code with no seeded bugs
    clean_functions: int = 0
    clean_false_positives: int = 0
    fp_per_100_clean_functions: float | None = None
    # fixes, over the bugs that were found
    fixes_attempted: Rate = Field(default_factory=Rate)  # attempted / found
    fixes_exact: Rate = Field(default_factory=Rate)  # exact / attempted
    fixes_verified: Rate = Field(default_factory=Rate)  # verified_fix / attempted
    fixes_resolve_tests: Rate = Field(default_factory=Rate)  # resolves / measured
    cost_usd: float = 0.0


class EvalResult(BaseModel):
    eval_id: str
    started_at: str
    config_version: str
    models: dict[str, str]
    prompt_versions: dict[str, str]
    status: str  # completed | stopped_budget
    fix_enabled: bool = True
    cases: list[CaseResult]
    totals: Totals
    notes: list[str] = Field(default_factory=list)
