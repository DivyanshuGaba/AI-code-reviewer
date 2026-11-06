"""Versioned prompt files. Prompts live in config/prompts/<id>.<version>.md, never inline.

A file has a system section and a user section separated by a line `=== USER ===`.
Placeholders use `{name}`; only the template is formatted, so braces inside substituted
values (code, JSON) are safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache

from reviewer.settings import CONFIG_DIR

LANGUAGE_NAMES = {
    "python": "Python",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "tsx": "TypeScript (TSX)",
}
_SEPARATOR = "\n=== USER ===\n"
_FIELD_RE = re.compile(r"\{([a-z_]+)\}")


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    system_template: str
    user_template: str

    def render(self, *, language: str = "python", **values: str) -> tuple[str, str]:
        def fill(template: str) -> str:
            return _FIELD_RE.sub(lambda m: values[m.group(1)], template)

        system = fill(self.system_template).strip()
        if language != "python":
            name = LANGUAGE_NAMES.get(language, language)
            system += (
                f"\n\nLanguage note: the code you are given is {name}, not Python. Apply the "
                f"instructions above to {name}; for example, 'function' includes methods and "
                "arrow functions."
            )
        return system, fill(self.user_template).strip()


@cache
def load_prompt(prompt_id: str, version: str) -> Prompt:
    path = CONFIG_DIR / "prompts" / f"{prompt_id}.{version}.md"
    text = path.read_text()
    if _SEPARATOR not in text:
        raise ValueError(f"{path.name}: missing '=== USER ===' separator")
    system, user = text.split(_SEPARATOR, 1)
    return Prompt(prompt_id, version, system, user)


def number_lines(text: str, first_line: int) -> str:
    """Prefix each line with its absolute line number so the model can cite lines."""
    return "\n".join(f"{n:>5}| {line}" for n, line in enumerate(text.splitlines(), first_line))


_TAG_RE = re.compile(
    r"</?(target_code|context_chunk|code_under_review|triage_hints|static_signals|finding|module_head|repo_facts|module_summaries|reviewed_findings)",
    re.I,
)


def neutralise_tags(text: str) -> str:
    """Stop repository text from closing or opening the delimiters we wrap it in."""
    return _TAG_RE.sub(lambda m: m.group(0).replace("<", "<​"), text)
