"""Urgency levels: how fast the ticket needs a response (independent of severity = how serious the impact is).

Single source for the definitions used in the chat prompt, jev's urgency question and the UI legend.
"""

# most urgent first
URGENCY_DEFINITIONS: dict[str, str] = {
    "critical": "Outage, security incident, or many users blocked; revenue or data at risk right now. Respond immediately.",
    "high": "A user or customer is blocked or significantly impacted. Needs action today.",
    "medium": "Impaired but has a workaround, or a time-bound request. Handle within the normal SLA.",
    "low": "Questions, feedback, cosmetic issues; no time pressure.",
}


def urgency_meaning(level: str | None) -> str:
    return URGENCY_DEFINITIONS.get((level or "").lower(), "")


def urgency_list() -> list[dict]:
    return [{"name": k, "description": v} for k, v in URGENCY_DEFINITIONS.items()]
