"""Severity ranks: loaded from severity.yaml at startup (like teams.yaml).

The ranks drive everything severity-related: the rubric in the chat prompt, the ordered scale
of jev's `score` question, and the level (e.g. SEV-1) derived from a 0-100 score.
"""

from dataclasses import asdict, dataclass
from pathlib import Path

import yaml


class SeverityError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeverityRank:
    name: str          # e.g. "SEV-1"
    label: str         # short title, e.g. "Critical impact"
    min_score: int     # inclusive lower bound of the 0-100 score
    max_score: int     # inclusive upper bound (derived from the next rank)
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


# Used until load_severity() runs (and by tests); severity.yaml ships with the same values.
DEFAULT_RANKS_YAML = [
    {"name": "SEV-1", "label": "Critical impact", "min_score": 80,
     "description": "Confirmed security breach or data loss, full production outage, or core functionality "
                    "broken for many users or a whole enterprise account with revenue impact."},
    {"name": "SEV-2", "label": "Major impact", "min_score": 60,
     "description": "An important feature broken or a key customer blocked, with no reasonable workaround."},
    {"name": "SEV-3", "label": "Moderate impact", "min_score": 30,
     "description": "A single user blocked, or degraded service with a workaround."},
    {"name": "SEV-4", "label": "Low impact", "min_score": 0,
     "description": "Minor or cosmetic issue, feedback, how-to or billing question, time-bound request."},
]


def build_ranks(entries: list, source: str = "severity ranks") -> list[SeverityRank]:
    """Validate and sort the ranks from most to least severe, deriving each max_score."""
    if not isinstance(entries, list) or len(entries) < 2:
        raise SeverityError(f"{source}: define at least 2 ranks")
    parsed = []
    for i, e in enumerate(entries, 1):
        if not isinstance(e, dict) or not str(e.get("name", "")).strip():
            raise SeverityError(f"{source}: rank #{i} needs a 'name'")
        try:
            min_score = int(e.get("min_score"))
        except (TypeError, ValueError):
            raise SeverityError(f"{source}: rank {e['name']!r} needs an integer 'min_score'")
        if not 0 <= min_score <= 100:
            raise SeverityError(f"{source}: 'min_score' of {e['name']!r} must be between 0 and 100")
        description = str(e.get("description", "")).strip()
        if not description:
            raise SeverityError(f"{source}: rank {e['name']!r} needs a 'description'")
        parsed.append((str(e["name"]).strip(), str(e.get("label", "")).strip(), min_score, description))

    names = [p[0].lower() for p in parsed]
    if len(set(names)) != len(names):
        raise SeverityError(f"{source}: rank names must be unique")
    mins = [p[2] for p in parsed]
    if len(set(mins)) != len(mins):
        raise SeverityError(f"{source}: each rank needs a different 'min_score'")
    if 0 not in mins:
        raise SeverityError(f"{source}: the least severe rank must have min_score: 0")

    parsed.sort(key=lambda p: -p[2])
    ranks, upper = [], 100
    for name, label, lo, desc in parsed:
        ranks.append(SeverityRank(name, label, lo, upper, desc))
        upper = lo - 1
    return ranks


def load_severity(path: Path) -> list[SeverityRank]:
    """Missing file = default ranks. A malformed file stops startup with a clear error."""
    if not path.exists():
        return build_ranks(DEFAULT_RANKS_YAML)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SeverityError(f"Invalid YAML in {path}: {e}")
    entries = data.get("ranks") if isinstance(data, dict) else None
    return build_ranks(entries, str(path))


_ranks: list[SeverityRank] = build_ranks(DEFAULT_RANKS_YAML)


def configure(ranks: list[SeverityRank]) -> None:
    global _ranks
    _ranks = ranks


def ranks() -> list[SeverityRank]:
    """Current ranks, most severe first."""
    return _ranks


def rank_for(score: float | None) -> SeverityRank | None:
    if score is None:
        return None
    return next((r for r in _ranks if score >= r.min_score), _ranks[-1])


def position_to_score(position: float, rank_list: list[SeverityRank] | None = None) -> float:
    """Map a decision model's scale position (0 = least severe rank .. n-1 = most severe) to 0-100.

    Interpolates between anchor points: 0 for the least severe rank, the midpoint of each middle
    rank, and 100 for the most severe one, so a whole-number position always lands inside its rank.
    """
    ordered = list(reversed(rank_list or _ranks))  # least severe first
    n = len(ordered)
    anchors = [0.0] + [(r.min_score + r.max_score) / 2 for r in ordered[1:-1]] + [100.0]
    p = min(max(position, 0.0), n - 1)
    i = min(int(p), n - 2)
    return anchors[i] + (anchors[i + 1] - anchors[i]) * (p - i)
