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

_SEPARATOR = "\n=== USER ===\n"
_FIELD_RE = re.compile(r"\{([a-z_]+)\}")


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    system_template: str
    user_template: str

    def render(self, **values: str) -> tuple[str, str]:
        def fill(template: str) -> str:
            return _FIELD_RE.sub(lambda m: values[m.group(1)], template)

        return fill(self.system_template).strip(), fill(self.user_template).strip()


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


_TAG_RE = re.compile(r"</?(target_code|context_chunk|code_under_review|triage_hints)", re.I)


def neutralise_tags(text: str) -> str:
    """Stop repository text from closing or opening the delimiters we wrap it in."""
    return _TAG_RE.sub(lambda m: m.group(0).replace("<", "<​"), text)
