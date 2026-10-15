from app.schemas import Ticket
from app.severity import ranks
from app.teams import Team
from app.tiers import Tier, describe
from app.urgency import URGENCY_DEFINITIONS


def system_prompt(teams: list[Team], tiers: list[Tier] | None = None) -> str:
    lines = []
    for t in teams:
        line = f"- {t.name}: {t.description}"
        if t.examples:
            line += " Examples: " + "; ".join(f'"{e}"' for e in t.examples)
        lines.append(line)
    team_block = "\n".join(lines)
    urgency_block = "\n".join(f"- {k}: {v}" for k, v in URGENCY_DEFINITIONS.items())
    tier_block = ""
    if tiers:
        tier_block = "\nCustomer tiers:\n" + "\n".join(f"- {t.name}: {t.description}" if t.description else f"- {t.name}"
                                                    for t in tiers) + "\n"
    severity_block = "\n".join(
        f"- {r.name}{f' ({r.label})' if r.label else ''}, score {r.min_score}-{r.max_score}: {r.description}" for r in ranks())
    return f"""You are a support ticket triage assistant. Route each ticket to exactly one team, assess urgency, and suggest how to resolve it.

Teams:
{team_block}
{tier_block}
Urgency levels (how fast we must respond):
{urgency_block}

Severity (0-100) measures how serious the impact is, independently of urgency (urgency = how fast we must respond).
Consider how many users are affected, revenue or data loss, security risk, and whether a workaround exists.
Severity ranks:
{severity_block}
Pick the rank that fits best and give a "severity_score" inside its range.
Example: a GDPR request can be medium urgency but low severity.

Rules:
- "team" must be one of the team names above, written exactly.
- "team_probabilities" must contain every team name with your probability (0-1) that it is the right team; they should sum to 1.
- "urgency_reason" explains in one or two sentences why the ticket has that urgency, citing facts from the ticket.
- "confidence" (0-1) is how sure you are about the overall triage (team + urgency). Use low values when the ticket is vague or spans several teams.
- Consider the customer tier when judging urgency and escalation.
- Write "suggested_reply" in the same language as the ticket.
- Respond with a single JSON object only, no markdown."""


def user_prompt(ticket: Ticket, tiers: list[Tier] | None = None) -> str:
    parts = [f"Subject: {ticket.subject or '(none)'}"]
    if ticket.customer_tier:
        desc = describe(ticket.customer_tier, tiers)
        parts.append(f"Customer tier: {ticket.customer_tier}" + (f" ({desc})" if desc else ""))
    parts.append(f"Body:\n{ticket.body}")
    return "\n".join(parts)


def triage_json_schema(teams: list[Team]) -> dict:
    names = [t.name for t in teams]
    return {
        "name": "ticket_triage",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "team", "team_probabilities", "urgency", "urgency_reason", "category", "sentiment",
                "summary", "suggested_reply", "next_steps", "escalate", "confidence",
                "severity_score", "severity_reason",
            ],
            "properties": {
                "team": {"type": "string", "enum": names},
                "team_probabilities": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": names,
                    "properties": {n: {"type": "number"} for n in names},
                },
                "urgency": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                "urgency_reason": {"type": "string"},
                "category": {"type": "string"},
                "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative", "angry"]},
                "summary": {"type": "string"},
                "suggested_reply": {"type": "string"},
                "next_steps": {"type": "array", "items": {"type": "string"}},
                "escalate": {"type": "boolean"},
                "confidence": {"type": "number"},
                "severity_score": {"type": "integer"},
                "severity_reason": {"type": "string"},
            },
        },
    }
