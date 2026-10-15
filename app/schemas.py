from typing import Literal

from pydantic import BaseModel, Field, computed_field

from app.severity import rank_for
from app.urgency import urgency_meaning

Urgency = Literal["critical", "high", "medium", "low"]
URGENCY_LEVELS: list[str] = ["low", "medium", "high", "critical"]


def severity_level(score: int | None) -> str | None:
    """Name of the severity rank (from severity.yaml) that contains the score, e.g. "SEV-1"."""
    rank = rank_for(score)
    return rank.name if rank else None


class Ticket(BaseModel):
    subject: str = ""
    body: str = Field(min_length=1)
    customer_tier: str | None = None
    expected_team: str | None = None
    expected_urgency: str | None = None
    expected_severity: int | None = Field(default=None, ge=0, le=100)


class TriageResult(BaseModel):
    team: str
    team_probabilities: dict[str, float] = {}
    urgency: Urgency
    urgency_reason: str = ""
    urgency_probabilities: dict[str, float] = {}  # only from decision models
    category: str = ""
    sentiment: str = ""
    summary: str = ""
    suggested_reply: str = ""
    next_steps: list[str] = []
    escalate: bool = False
    confidence: float = Field(default=0.0, ge=0, le=1)
    severity_score: int | None = Field(default=None, ge=0, le=100)
    severity_reason: str = ""
    escalate_probability: float | None = None  # only from decision models (noul answer)
    # per-question confidence returned by decision models (choice/score answers; noul has none)
    team_confidence: float | None = None
    urgency_confidence: float | None = None
    severity_confidence: float | None = None

    @computed_field
    @property
    def severity_level(self) -> str | None:
        return severity_level(self.severity_score)

    @computed_field
    @property
    def urgency_meaning(self) -> str:
        return urgency_meaning(self.urgency)

    @computed_field
    @property
    def severity_label(self) -> str | None:
        rank = rank_for(self.severity_score)
        return rank.label if rank else None


class RunMetrics(BaseModel):
    latency_s: float | None = None
    ttft_s: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    cost_source: Literal["openrouter", "pricing", "unknown"] = "unknown"


REVIEW_MODES = ("default", "urgency", "team")
ReviewMode = Literal["default", "urgency", "team"]


class Decision(BaseModel):
    automatic: bool
    top_prob: float | None = None
    prob_source: Literal["self-reported", "logprobs+self-reported", "decision model"] = "self-reported"
    reasons: list[str] = []
    mode: ReviewMode = "default"  # which rule decided human review (HUMAN_REVIEW_MODE)


class ModelRun(BaseModel):
    model: str
    is_jev: bool = False
    api: Literal["chat", "decisions"] = "chat"
    result: TriageResult | None = None
    metrics: RunMetrics = RunMetrics()
    decision: Decision | None = None
    error: str | None = None
    agrees_team: bool | None = None
    agrees_urgency: bool | None = None
    request: dict | None = None  # JSON body sent to OpenRouter (last attempt)
    team_logprob: float | None = None  # probability of the team tokens, when the provider returns logprobs


class ComparisonResponse(BaseModel):
    ticket: Ticket
    skills: list[str] = []
    runs: list[ModelRun]
