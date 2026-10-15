"""Agent Skills: folders with a SKILL.md (YAML frontmatter + instructions) that are added to the prompt when relevant.

skills/<skill-name>/SKILL.md
---
name: security-incident
description: How to triage suspected account compromise, phishing and vulnerability reports.
triggers: [hacked, phishing, suspicious login]   # case-insensitive phrases matched against subject + body
always: false                                    # true = include for every ticket
priority: 10                                     # higher = chosen first when MAX_SKILLS_PER_TICKET applies
---
<instructions in markdown>
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.schemas import Ticket


class SkillsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    triggers: list[str] = field(default_factory=list)
    always: bool = False
    priority: int = 0

    def matches(self, text: str) -> bool:
        if self.always:
            return True
        return any(re.search(rf"(?<!\w){re.escape(t.lower())}", text) for t in self.triggers)


def parse_skill(path: Path) -> Skill:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", raw, re.DOTALL)
    if not m:
        raise SkillsError(f"{path}: missing YAML frontmatter (--- ... ---)")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillsError(f"{path}: invalid frontmatter: {e}")
    name = str(meta.get("name") or path.parent.name).strip()
    description = str(meta.get("description", "")).strip()
    if not description:
        raise SkillsError(f"{path}: 'description' is required")
    triggers = meta.get("triggers") or []
    if not isinstance(triggers, list):
        raise SkillsError(f"{path}: 'triggers' must be a list")
    body = m.group(2).strip()
    if not body:
        raise SkillsError(f"{path}: skill has no instructions")
    try:
        priority = int(meta.get("priority", 0))
    except (TypeError, ValueError):
        raise SkillsError(f"{path}: 'priority' must be an integer")
    return Skill(name, description, body, [str(t) for t in triggers if str(t).strip()],
                 bool(meta.get("always", False)), priority)


def load_skills(directory: Path) -> list[Skill]:
    """Missing directory = no skills. Malformed SKILL.md files stop startup with a clear error."""
    if not directory.exists():
        return []
    skills = [parse_skill(p) for p in sorted(directory.glob("*/SKILL.md"))]
    names = [s.name for s in skills]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SkillsError(f"Duplicate skill names: {', '.join(sorted(dupes))}")
    return skills


def select_skills(skills: list[Skill], ticket: Ticket, limit: int) -> list[Skill]:
    text = f"{ticket.subject}\n{ticket.body}".lower()
    matched = [s for s in skills if s.matches(text)]
    return sorted(matched, key=lambda s: -s.priority)[:limit]


def skills_block(selected: list[Skill]) -> str:
    if not selected:
        return ""
    parts = [f"### Skill: {s.name}\n{s.description}\n\n{s.body}" for s in selected]
    return "\n\nRelevant skills (follow these instructions when they apply):\n\n" + "\n\n".join(parts)
