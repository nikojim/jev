import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    jev_model: str
    compare_models: list[str]
    teams_file: Path
    skills_dir: Path
    severity_file: Path
    tiers_file: Path
    max_skills: int
    auto_min_prob: float
    auto_min_confidence: float
    human_review_mode: str
    max_concurrency: int
    request_timeout: float

    @property
    def models(self) -> list[str]:
        return [self.jev_model] + [m for m in self.compare_models if m != self.jev_model]


def _float(name: str, default: str) -> float:
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}")


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    return p if p.is_absolute() else ROOT / p


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    jev_model = os.getenv("JEV_MODEL", "").strip()
    if not api_key:
        raise ConfigError("OPENROUTER_API_KEY is not set (copy .env.example to .env)")
    if not jev_model:
        raise ConfigError("JEV_MODEL is not set")

    review_mode = os.getenv("HUMAN_REVIEW_MODE", "default").strip().lower() or "default"
    if review_mode not in ("default", "urgency", "team"):
        raise ConfigError(f"HUMAN_REVIEW_MODE must be default, urgency or team, got {review_mode!r}")

    teams_file = _path("TEAMS_FILE", "teams.yaml")
    skills_dir = _path("SKILLS_DIR", "skills")
    severity_file = _path("SEVERITY_FILE", "severity.yaml")
    tiers_file = _path("TIERS_FILE", "tiers.yaml")

    return Settings(
        api_key=api_key,
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        jev_model=jev_model,
        compare_models=[m.strip() for m in os.getenv("COMPARE_MODELS", "").split(",") if m.strip()],
        teams_file=teams_file,
        skills_dir=skills_dir,
        severity_file=severity_file,
        tiers_file=tiers_file,
        max_skills=max(0, int(_float("MAX_SKILLS_PER_TICKET", "3"))),
        auto_min_prob=_float("AUTO_ASSIGN_MIN_PROB", "0.80"),
        auto_min_confidence=_float("AUTO_ASSIGN_MIN_CONFIDENCE", "0.80"),
        human_review_mode=review_mode,
        max_concurrency=max(1, int(_float("MAX_CONCURRENCY", "4"))),
        request_timeout=_float("REQUEST_TIMEOUT", "60"),
    )
