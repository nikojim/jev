from dataclasses import dataclass, field
from pathlib import Path

import yaml


class TeamsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Team:
    name: str
    description: str = ""
    examples: list[str] = field(default_factory=list)


def load_teams(path: Path) -> list[Team]:
    if not path.exists():
        raise TeamsError(f"Teams file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise TeamsError(f"Invalid YAML in {path}: {e}")

    entries = data.get("teams") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise TeamsError(f"{path} must contain a top-level 'teams:' list")

    teams: list[Team] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
            raise TeamsError(f"{path}: team #{i} needs a 'name'")
        name = str(entry["name"]).strip()
        if name.lower() in seen:
            raise TeamsError(f"{path}: duplicate team name {name!r}")
        seen.add(name.lower())
        examples = entry.get("examples") or []
        if not isinstance(examples, list):
            raise TeamsError(f"{path}: 'examples' of team {name!r} must be a list")
        teams.append(Team(name, str(entry.get("description", "")).strip(), [str(x) for x in examples]))

    if len(teams) < 2:
        raise TeamsError(f"{path}: define at least 2 teams")
    return teams
