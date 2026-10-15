"""Decisions API (POST /api/alpha/decisions) for decision models such as TypeSafe's Jev.

A decision model does not generate text: it receives a `state` and typed `questions`
(choice / score / noul) and returns answers with probabilities. Triage fields that need
free text (summary, suggested reply, next steps) are therefore left empty for these models.
"""

from app.schemas import Ticket
from app.severity import SeverityRank, position_to_score, ranks
from app.teams import Team
from app.tiers import Tier, describe
from app.urgency import URGENCY_DEFINITIONS

URGENCY_CRITERIA = URGENCY_DEFINITIONS


def severity_criteria(rank_list: list[SeverityRank] | None = None) -> list[str]:
    """jev's ordered scale, least severe first (the score answer 0..n-1 is mapped back to 0-100)."""
    ordered = list(reversed(rank_list or ranks()))
    return [f"{r.name}{f' ({r.label})' if r.label else ''}: {r.description}" for r in ordered]

SENTIMENT_CRITERIA = {
    "positive": "Friendly or satisfied tone.",
    "neutral": "Matter-of-fact tone.",
    "negative": "Unhappy or frustrated.",
    "angry": "Angry, threatening to leave, or very upset.",
}


def is_decisions_model(model: str) -> bool:
    return model.lstrip("~").startswith("typesafe/") or model.endswith("-decisions")


def build_decisions_payload(model: str, ticket: Ticket, teams: list[Team], guidelines: str,
                            tiers: list[Tier] | None = None) -> dict:
    state: dict = {"subject": ticket.subject, "body": ticket.body}
    if ticket.customer_tier:
        state["customer_tier"] = ticket.customer_tier
        desc = describe(ticket.customer_tier, tiers)
        if desc:
            state["customer_tier_description"] = desc
    if guidelines:
        state["guidelines"] = guidelines
    follow = " Apply the guidelines in the state when they are relevant." if guidelines else ""

    team_criteria = {}
    for t in teams:
        text = t.description
        if t.examples:
            text += " Examples: " + "; ".join(t.examples)
        team_criteria[t.name] = text

    return {
        "model": model,
        "state": state,
        "questions": {
            "team": {"type": "choice", "instructions": "Which team should handle this support ticket?" + follow,
                     "criteria": team_criteria},
            "urgency": {"type": "choice", "instructions": "How fast must we respond to this ticket? Consider the customer tier." + follow,
                        "criteria": URGENCY_CRITERIA},
            "severity": {"type": "score",
                         "instructions": "How severe is the impact of this issue, independently of how fast we must respond?" + follow,
                         "criteria": severity_criteria()},
            "escalate": {"type": "noul", "instructions": "Should this ticket be escalated beyond the normal support queue?" + follow,
                         "criteria": {"true": "Production impact, security risk, legal deadline, or a key account at risk.",
                                      "false": "Can be handled in the normal queue."}},
            "sentiment": {"type": "choice", "instructions": "What is the customer's sentiment?", "criteria": SENTIMENT_CRITERIA},
        },
    }


def _top(probs: dict, n: int = 2) -> str:
    ranked = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return ", ".join(f"{k} {v:.0%}" for k, v in ranked if v > 0)


def parse_decisions(response: dict) -> dict:
    """Map a decisions response to the TriageResult fields (validated later by Triager.parse_result)."""
    answers = response.get("answers") or {}
    team = answers.get("team") or {}
    urgency = answers.get("urgency") or {}
    severity = answers.get("severity") or {}
    escalate = answers.get("escalate") or {}
    sentiment = answers.get("sentiment") or {}
    if "choice" not in team or "choice" not in urgency:
        raise ValueError("Decisions response is missing the team or urgency answer")

    severity_score = None
    severity_reason = ""
    if severity.get("score") is not None:
        ordered = list(reversed(ranks()))  # index 0 = least severe, as sent in the criteria
        severity_score = position_to_score(severity["score"])
        probs = {}
        for k, v in (severity.get("probabilities") or {}).items():
            try:
                probs[ordered[int(k)].name] = v
            except (ValueError, IndexError):
                probs[str(k)] = v
        severity_reason = (f"Jev rank probabilities: {_top(probs)} "
                           f"(scale position {severity['score']:.2f} of {len(ordered) - 1})")

    confidences = [a["confidence"] for a in (team, urgency) if a.get("confidence") is not None]
    p_escalate = escalate.get("noul")
    return {
        "team": team["choice"],
        "team_probabilities": team.get("probabilities") or {},
        "urgency": urgency["choice"],
        # jev doesn't generate text: the UI shows the level's meaning (urgency_meaning) plus these probabilities
        "urgency_reason": f"Jev probabilities: {_top(urgency.get('probabilities') or {}, 4)}",
        "urgency_probabilities": urgency.get("probabilities") or {},
        "category": "",
        "sentiment": sentiment.get("choice", ""),
        "summary": "",
        "suggested_reply": "",
        "next_steps": [],
        "escalate": bool(p_escalate is not None and p_escalate >= 0.5),
        # overall triage confidence = the weaker of the team and urgency confidences
        "confidence": min(confidences) if confidences else 0.0,
        "severity_score": severity_score,
        "severity_reason": severity_reason,
        "escalate_probability": p_escalate,
    }
