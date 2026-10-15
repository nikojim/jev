"""Customer tiers: loaded from tiers.yaml at startup (like teams.yaml).

The tier tells the models how important the customer is, so they can weigh urgency and escalation.
Each tier's description is sent to the models along with its name.
"""

from dataclasses import asdict, dataclass
from pathlib import Path

import yaml


class TiersError(RuntimeError):
    pass


@dataclass(frozen=True)
class Tier:
    name: str
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_TIERS = [
    Tier("free", "Free plan. No SLA; best-effort support."),
    Tier("pro", "Paying customer on a self-serve plan. Standard support SLA."),
    Tier("enterprise", "Large contract with a dedicated SLA, many users and high revenue at stake. "
                       "Issues blocking an enterprise account deserve higher urgency and escalation."),
]


def load_tiers(path: Path) -> list[Tier]:
    """Missing file = default tiers. A malformed file stops startup with a clear error."""
    if not path.exists():
        return list(DEFAULT_TIERS)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise TiersError(f"Invalid YAML in {path}: {e}")
    entries = data.get("tiers") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise TiersError(f"{path} must contain a non-empty top-level 'tiers:' list")

    tiers: list[Tier] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
            raise TiersError(f"{path}: tier #{i} needs a 'name'")
        name = str(entry["name"]).strip()
        if name.lower() in seen:
            raise TiersError(f"{path}: duplicate tier name {name!r}")
        seen.add(name.lower())
        tiers.append(Tier(name, str(entry.get("description", "")).strip()))
    return tiers


def describe(tier_name: str | None, tiers: list[Tier] | None) -> str:
    """Description of a tier by name (case-insensitive); empty for unknown tiers (e.g. free text in a CSV)."""
    if not tier_name or not tiers:
        return ""
    return next((t.description for t in tiers if t.name.lower() == tier_name.strip().lower()), "")
