"""Run tickets through every configured model, decide auto-assign vs human review, and aggregate metrics."""

import asyncio
import math
from statistics import mean

from pydantic import ValidationError

from app.config import Settings
from app.decisions import build_decisions_payload, is_decisions_model, parse_decisions
from app.llm import LLMError, OpenRouterClient, extract_json, is_decisions_model_error, team_logprob
from app.prompts import system_prompt, triage_json_schema, user_prompt
from app.schemas import URGENCY_LEVELS, Decision, ModelRun, Ticket, TriageResult, severity_level
from app.skills import Skill, select_skills, skills_block
from app.teams import Team
from app.tiers import DEFAULT_TIERS, Tier

TIE_MARGIN = 0.15


class Triager:
    def __init__(self, client: OpenRouterClient, settings: Settings, teams: list[Team], skills: list[Skill] | None = None,
                 tiers: list[Tier] | None = None):
        self.client = client
        self.settings = settings
        self.teams = teams
        self.skills = skills or []
        self.tiers = list(DEFAULT_TIERS) if tiers is None else tiers
        self.team_names = [t.name for t in teams]
        self.system = system_prompt(teams, self.tiers)
        self.schema = triage_json_schema(teams)
        self.semaphore = asyncio.Semaphore(settings.max_concurrency)
        # models that only work with the Decisions API (detected by id, or learned from a chat 400)
        self.decision_models = {m for m in settings.models if is_decisions_model(m)}

    async def run_ticket(self, ticket: Ticket, on_model_done=None) -> tuple[list[ModelRun], list[str]]:
        """Returns the runs (jev first) and the names of the skills applied. Every model gets the same skills.

        on_model_done(index, run) is awaited as soon as each model finishes (used to stream progress).
        """
        user, skills = self.build_user_prompt(ticket)
        guidelines = self.guidelines(ticket)

        async def one(i: int, model: str) -> ModelRun:
            run = await self._run_model(model, ticket, user, guidelines)
            if on_model_done:
                await on_model_done(i, run)
            return run

        runs = await asyncio.gather(*(one(i, m) for i, m in enumerate(self.settings.models)))
        jev = runs[0]
        for run in runs:
            if run.result and jev.result:
                run.agrees_team = run.result.team == jev.result.team
                run.agrees_urgency = run.result.urgency == jev.result.urgency
        return list(runs), skills

    def build_user_prompt(self, ticket: Ticket) -> tuple[str, list[str]]:
        selected = select_skills(self.skills, ticket, self.settings.max_skills)
        return user_prompt(ticket, self.tiers) + skills_block(selected), [s.name for s in selected]

    def messages(self, user: str) -> list[dict]:
        return [{"role": "system", "content": self.system}, {"role": "user", "content": user}]

    def guidelines(self, ticket: Ticket) -> str:
        """Skill instructions, sent to decision models inside the state."""
        return skills_block(select_skills(self.skills, ticket, self.settings.max_skills)).strip()

    def decisions_payload(self, model: str, ticket: Ticket) -> dict:
        return build_decisions_payload(model, ticket, self.teams, self.guidelines(ticket), self.tiers)

    async def _run_model(self, model: str, ticket: Ticket, user: str, guidelines: str) -> ModelRun:
        if model in self.decision_models:
            return await self._run_decisions(model, ticket, guidelines)
        run = ModelRun(model=model, is_jev=model == self.settings.jev_model)
        messages = self.messages(user)
        async with self.semaphore:
            try:
                completion = await self.client.chat(model, messages, self.schema)
            except LLMError as e:
                if is_decisions_model_error(e):
                    self.decision_models.add(model)
                    return await self._run_decisions(model, ticket, guidelines)
                run.error = str(e)
                run.request = e.request
                run.decision = Decision(automatic=False, reasons=["Model call failed"])
                return run
        run.metrics = completion.metrics
        run.request = completion.request
        try:
            run.result = self.parse_result(extract_json(completion.text))
        except (ValueError, ValidationError) as e:
            run.error = f"Invalid response: {str(e).splitlines()[0]}"
            run.decision = Decision(automatic=False, reasons=["Invalid response from model"])
            return run
        lp = team_logprob(completion.text, completion.logprobs, run.result.team)
        run.decision = decide(run.result, self.settings.auto_min_prob, self.settings.auto_min_confidence, lp)
        return run

    async def _run_decisions(self, model: str, ticket: Ticket, guidelines: str) -> ModelRun:
        run = ModelRun(model=model, is_jev=model == self.settings.jev_model, api="decisions")
        run.request = build_decisions_payload(model, ticket, self.teams, guidelines, self.tiers)
        async with self.semaphore:
            try:
                response, run.metrics = await self.client.decisions(model, run.request)
            except LLMError as e:
                run.error = str(e)
                run.decision = Decision(automatic=False, reasons=["Model call failed"])
                return run
        try:
            run.result = self.parse_result(parse_decisions(response))
        except (ValueError, ValidationError) as e:
            run.error = f"Invalid response: {str(e).splitlines()[0]}"
            run.decision = Decision(automatic=False, reasons=["Invalid response from model"])
            return run
        run.decision = decide(run.result, self.settings.auto_min_prob, self.settings.auto_min_confidence,
                              source="decision model")
        return run

    def parse_result(self, data: dict) -> TriageResult:
        by_lower = {n.lower(): n for n in self.team_names}
        team = by_lower.get(str(data.get("team", "")).strip().lower())
        if team is None:
            raise ValueError(f"Unknown team {data.get('team')!r}")
        data["team"] = team
        data["urgency"] = str(data.get("urgency", "")).strip().lower()
        data["team_probabilities"] = normalize_probs(data.get("team_probabilities"), self.team_names)
        try:
            data["confidence"] = min(1.0, max(0.0, float(data.get("confidence", 0))))
        except (TypeError, ValueError):
            data["confidence"] = 0.0
        data["severity_score"] = parse_severity(data.get("severity_score"))
        if isinstance(data.get("next_steps"), str):
            data["next_steps"] = [data["next_steps"]]
        return TriageResult.model_validate(data)


def parse_severity(raw) -> int | None:
    """Clamp to 0-100; a missing or invalid score is tolerated (severity is informational)."""
    try:
        return min(100, max(0, round(float(raw))))
    except (TypeError, ValueError):
        return None


def normalize_probs(raw, team_names: list[str]) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    by_lower = {str(k).strip().lower(): v for k, v in raw.items()}
    probs = {}
    for name in team_names:
        try:
            probs[name] = max(0.0, float(by_lower.get(name.lower(), 0)))
        except (TypeError, ValueError):
            probs[name] = 0.0
    total = sum(probs.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in probs.items()}


def decide(result: TriageResult, min_prob: float, min_conf: float, logprob: float | None = None,
           source: str = "self-reported") -> Decision:
    reasons: list[str] = []
    probs = result.team_probabilities
    top_prob = probs.get(result.team) if probs else None
    if logprob is not None and top_prob is not None:
        top_prob, source = min(top_prob, logprob), "logprobs+self-reported"

    if top_prob is None:
        reasons.append("Model returned no team probabilities")
    else:
        if top_prob < min_prob:
            reasons.append(f"Team probability {top_prob:.2f} is below {min_prob:.2f}")
        ranked = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
        if ranked[0][0] != result.team:
            reasons.append(f"Chosen team differs from the most probable team ({ranked[0][0]})")
        elif len(ranked) > 1 and ranked[0][1] - ranked[1][1] < TIE_MARGIN:
            reasons.append(f"Near tie between {ranked[0][0]} and {ranked[1][0]}")
    if result.confidence < min_conf:
        reasons.append(f"Confidence {result.confidence:.2f} is below {min_conf:.2f}")
    if result.urgency == "critical":
        reasons.append("Critical urgency always requires human review")
    return Decision(automatic=not reasons, top_prob=top_prob, prob_source=source, reasons=reasons)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)]


def _pct(num: int, den: int) -> float | None:
    return num / den if den else None


def aggregate(tickets: list[Ticket], all_runs: list[list[ModelRun]], models: list[str], jev_model: str,
              team_names: list[str]) -> dict:
    """Per-model summary over a batch, plus jev's confusion matrix when labels are present."""
    labeled_team = any(t.expected_team for t in tickets)
    labeled_urgency = any(t.expected_urgency for t in tickets)
    labeled_severity = any(t.expected_severity is not None for t in tickets)
    per_model = []
    for mi, model in enumerate(models):
        runs = [r[mi] for r in all_runs]
        ok = [r for r in runs if r.result]
        lat = [r.metrics.latency_s for r in runs if r.metrics.latency_s is not None]
        ttft = [r.metrics.ttft_s for r in runs if r.metrics.ttft_s is not None]
        costs = [r.metrics.cost_usd for r in runs if r.metrics.cost_usd is not None]
        auto = [r for r in ok if r.decision and r.decision.automatic]
        compared = [r for r in runs if r.agrees_team is not None]
        sev = [r.result.severity_score for r in ok if r.result.severity_score is not None]
        jev_runs = [t[0] for t in all_runs]
        sev_diff = [abs(r.result.severity_score - j.result.severity_score) for r, j in zip(runs, jev_runs)
                    if r.result and j.result and r.result.severity_score is not None and j.result.severity_score is not None]
        row = {
            "model": model,
            "is_jev": model == jev_model,
            "tickets": len(runs),
            "errors": len(runs) - len(ok),
            "avg_latency_s": mean(lat) if lat else None,
            "p95_latency_s": _p95(lat),
            "avg_ttft_s": mean(ttft) if ttft else None,
            "p95_ttft_s": _p95(ttft),
            "prompt_tokens": sum(r.metrics.prompt_tokens for r in runs),
            "completion_tokens": sum(r.metrics.completion_tokens for r in runs),
            "total_tokens": sum(r.metrics.total_tokens for r in runs),
            "total_cost_usd": sum(costs) if costs else None,
            "cost_per_1k_usd": mean(costs) * 1000 if costs else None,
            "auto_rate": _pct(len(auto), len(runs)),
            "agree_team_with_jev": _pct(sum(r.agrees_team for r in compared), len(compared)),
            "agree_urgency_with_jev": _pct(sum(bool(r.agrees_urgency) for r in compared), len(compared)),
            "avg_severity": mean(sev) if sev else None,
            "severity_diff_vs_jev": mean(sev_diff) if sev_diff and model != jev_model else None,
        }
        if labeled_team:
            pairs = [(t, r) for t, r in zip(tickets, runs) if t.expected_team]
            correct = sum(1 for t, r in pairs if r.result and r.result.team.lower() == t.expected_team.lower())
            auto_pairs = [(t, r) for t, r in pairs if r.result and r.decision and r.decision.automatic]
            auto_correct = sum(1 for t, r in auto_pairs if r.result.team.lower() == t.expected_team.lower())
            row["team_accuracy"] = _pct(correct, len(pairs))
            row["auto_accuracy"] = _pct(auto_correct, len(auto_pairs))
            row["auto_count"] = len(auto_pairs)
            row["cost_per_correct_usd"] = (sum(costs) / correct) if costs and correct else None
        if labeled_urgency:
            pairs = [(t, r) for t, r in zip(tickets, runs) if t.expected_urgency]
            exact = within = 0
            for t, r in pairs:
                if not r.result or t.expected_urgency.lower() not in URGENCY_LEVELS:
                    continue
                d = abs(URGENCY_LEVELS.index(r.result.urgency) - URGENCY_LEVELS.index(t.expected_urgency.lower()))
                exact += d == 0
                within += d <= 1
            row["urgency_accuracy"] = _pct(exact, len(pairs))
            row["urgency_within_one"] = _pct(within, len(pairs))
        if labeled_severity:
            pairs = [(t, r) for t, r in zip(tickets, runs)
                     if t.expected_severity is not None and r.result and r.result.severity_score is not None]
            row["severity_mae"] = mean(abs(r.result.severity_score - t.expected_severity) for t, r in pairs) if pairs else None
            row["severity_level_accuracy"] = _pct(
                sum(r.result.severity_level == severity_level(t.expected_severity) for t, r in pairs), len(pairs))
        per_model.append(row)

    confusion = None
    if labeled_team:
        labels = team_names + ["(error)"]
        matrix = {exp: {pred: 0 for pred in labels} for exp in team_names}
        for t, runs in zip(tickets, all_runs):
            if not t.expected_team:
                continue
            exp = next((n for n in team_names if n.lower() == t.expected_team.lower()), None)
            if exp is None:
                continue
            jev = runs[0]
            matrix[exp][jev.result.team if jev.result else "(error)"] += 1
        confusion = {"labels": labels, "matrix": matrix}

    return {"models": per_model, "labeled_team": labeled_team, "labeled_urgency": labeled_urgency,
            "labeled_severity": labeled_severity, "jev_confusion": confusion}
