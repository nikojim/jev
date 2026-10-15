import json
import math
from pathlib import Path

import httpx
import pytest

from app.config import ROOT, Settings
from app.llm import OpenRouterClient, extract_json, team_logprob
from app.main import parse_csv
from app.schemas import ModelRun, RunMetrics, Ticket, TriageResult, severity_level
from app.skills import SkillsError, load_skills, parse_skill, select_skills, skills_block
from app.teams import TeamsError, load_teams
from app.triage import Triager, aggregate, decide, normalize_probs, parse_severity

TEAMS = ["Billing & Payments", "Technical Support", "Account & Access"]


def result(team="Billing & Payments", probs=None, conf=0.9, urgency="high", severity=None) -> TriageResult:
    return TriageResult(team=team, team_probabilities=probs or {team: 0.9, "Technical Support": 0.1},
                        urgency=urgency, confidence=conf, severity_score=severity)


# ---------- JSON parsing ----------

def test_extract_json_plain_fenced_and_prose():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_normalize_probs_fills_missing_and_renormalizes():
    p = normalize_probs({"billing & payments": 3, "Technical Support": 1}, TEAMS)
    assert p == {"Billing & Payments": 0.75, "Technical Support": 0.25, "Account & Access": 0.0}
    assert normalize_probs(None, TEAMS) == {}
    assert normalize_probs({"x": 0}, TEAMS) == {}


# ---------- auto-assign decision ----------

def test_decide_automatic_when_both_high():
    d = decide(result(conf=0.85), 0.8, 0.8)
    assert d.automatic and d.reasons == [] and d.top_prob == pytest.approx(0.9)


def test_decide_threshold_boundaries_are_inclusive():
    r = result(probs={"Billing & Payments": 0.8, "Technical Support": 0.2}, conf=0.8)
    assert decide(r, 0.8, 0.8).automatic
    assert not decide(r, 0.81, 0.8).automatic
    assert not decide(r, 0.8, 0.81).automatic


def test_decide_human_reasons():
    assert "Confidence" in decide(result(conf=0.5), 0.8, 0.8).reasons[0]
    tie = result(probs={"Billing & Payments": 0.5, "Technical Support": 0.45}, conf=0.9)
    assert any("Near tie" in r for r in decide(tie, 0.4, 0.8).reasons)
    crit = decide(result(urgency="critical"), 0.8, 0.8)
    assert not crit.automatic and any("Critical" in r for r in crit.reasons)
    wrong = result(team="Technical Support", probs={"Billing & Payments": 0.9, "Technical Support": 0.1})
    assert any("differs" in r for r in decide(wrong, 0.05, 0.5).reasons)


def test_decide_uses_lower_of_logprob_and_self_reported():
    d = decide(result(conf=0.95), 0.8, 0.8, logprob=0.6)
    assert d.top_prob == pytest.approx(0.6) and not d.automatic and d.prob_source == "logprobs+self-reported"


def test_team_logprob_joint_probability():
    text = '{"team": "Billing & Payments", "x": 1}'
    tokens = [('{"team": "', -0.01), ("Billing", math.log(0.5)), (" & Payments", math.log(0.9)), ('", "x": 1}', 0.0)]
    assert team_logprob(text, tokens, "Billing & Payments") == pytest.approx(0.45)
    assert team_logprob(text, [], "Billing & Payments") is None


# ---------- cost ----------

def _settings(**kw) -> Settings:
    base = dict(api_key="k", base_url="https://or.test/api/v1", jev_model="jev/model", compare_models=["other/model"],
                teams_file=ROOT / "teams.yaml", skills_dir=ROOT / "skills", severity_file=ROOT / "severity.yaml", tiers_file=ROOT / "tiers.yaml", max_skills=3, auto_min_prob=0.8,
                auto_min_confidence=0.8, human_review_mode="default", max_concurrency=2, request_timeout=10)
    base.update(kw)
    return Settings(**base)


def test_cost_prefers_openrouter_then_pricing():
    c = OpenRouterClient("k", "https://x", 5)
    c.pricing["m"] = {"prompt": 1e-6, "completion": 2e-6}
    m = c._metrics("m", {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.5}, 1.0, 0.2)
    assert m.cost_usd == 0.5 and m.cost_source == "openrouter"
    m = c._metrics("m", {"prompt_tokens": 100, "completion_tokens": 50}, 1.0, 0.2)
    assert m.cost_usd == pytest.approx(200e-6) and m.cost_source == "pricing"
    m = c._metrics("unknown", {"prompt_tokens": 100}, 1.0, None)
    assert m.cost_usd is None and m.cost_source == "unknown"


# ---------- end-to-end with a mocked OpenRouter ----------

def _sse(content: str, usage: dict) -> bytes:
    chunks = [{"choices": [{"delta": {"content": content[:10]}}]}, {"choices": [{"delta": {"content": content[10:]}}]},
              {"choices": [], "usage": usage}]
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


@pytest.mark.asyncio
async def test_run_ticket_streams_metrics_and_falls_back_on_bad_model():
    teams = load_teams(ROOT / "teams.yaml")
    payload = {"team": "Billing & Payments", "team_probabilities": {t.name: (0.9 if t.name == "Billing & Payments" else 0.025) for t in teams},
               "urgency": "high", "urgency_reason": "", "category": "refund", "sentiment": "negative", "summary": "s",
               "suggested_reply": "r", "next_steps": ["a"], "escalate": False, "confidence": 0.9,
               "severity_score": 42, "severity_reason": "single customer, refund pending"}
    seen_prompts = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen_prompts.append(body["messages"][1]["content"])
        if body["model"] == "other/model":
            return httpx.Response(400, json={"error": {"message": "Invalid model"}})
        return httpx.Response(200, content=_sse(json.dumps(payload), {"prompt_tokens": 300, "completion_tokens": 80, "cost": 0.0012}))

    client = OpenRouterClient("k", "https://or.test/api/v1", 5)
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    triager = Triager(client, _settings(), teams, load_skills(ROOT / "skills"))
    runs, skills = await triager.run_ticket(Ticket(subject="Charged twice", body="Please refund the duplicate charge"))

    jev, other = runs
    assert jev.is_jev and jev.result.team == "Billing & Payments" and jev.decision.automatic
    assert jev.metrics.prompt_tokens == 300 and jev.metrics.cost_usd == 0.0012 and jev.metrics.ttft_s is not None
    assert other.error and "Invalid model" in other.error and not other.decision.automatic
    assert jev.result.severity_score == 42 and jev.result.severity_level == "SEV-3"
    # the exact JSON body is kept for display, for successes and failures, without the API key
    assert jev.request["model"] == "jev/model" and jev.request["response_format"]["type"] == "json_schema"
    assert jev.request["messages"][1]["content"] == seen_prompts[0]
    assert other.request and other.request["model"] == "other/model"
    assert "Bearer" not in json.dumps(jev.request) and "Authorization" not in json.dumps(jev.request)
    assert skills == ["billing-refunds"]
    assert all("Skill: billing-refunds" in p for p in seen_prompts)  # every model gets the same skills


# ---------- teams / skills / csv ----------

def test_load_teams_valid_and_errors(tmp_path: Path):
    assert len(load_teams(ROOT / "teams.yaml")) == 5
    bad = tmp_path / "t.yaml"
    bad.write_text("teams:\n  - name: A\n  - name: a\n")
    with pytest.raises(TeamsError, match="duplicate"):
        load_teams(bad)
    bad.write_text("teams:\n  - name: Only\n")
    with pytest.raises(TeamsError, match="at least 2"):
        load_teams(bad)
    with pytest.raises(TeamsError, match="not found"):
        load_teams(tmp_path / "missing.yaml")


def test_skills_parse_and_select(tmp_path: Path):
    skills = load_skills(ROOT / "skills")
    assert {s.name for s in skills} >= {"security-incident", "billing-refunds", "outage-response", "account-recovery"}
    picked = select_skills(skills, Ticket(subject="Suspicious login", body="Someone hacked my account"), 3)
    assert [s.name for s in picked] == ["security-incident", "account-recovery"]  # priority first
    assert [s.name for s in select_skills(skills, Ticket(subject="Suspicious login", body="hacked"), 1)] == ["security-incident"]
    assert select_skills(skills, Ticket(body="Please add dark mode"), 3) == []
    assert "Skill: security-incident" in skills_block(picked)

    d = tmp_path / "x"
    d.mkdir()
    (d / "SKILL.md").write_text("no frontmatter")
    with pytest.raises(SkillsError, match="frontmatter"):
        parse_skill(d / "SKILL.md")
    (d / "SKILL.md").write_text("---\nname: x\nalways: true\ndescription: d\n---\nDo it.")
    assert parse_skill(d / "SKILL.md").matches("anything")
    assert load_skills(tmp_path / "nope") == []


def test_sample_csv_parses_and_labels_are_valid():
    tickets = parse_csv((ROOT / "samples" / "tickets.csv").read_bytes())
    names = {t.name for t in load_teams(ROOT / "teams.yaml")}
    assert len(tickets) == 40
    assert all(t.expected_team in names for t in tickets)
    assert all(t.expected_urgency in {"critical", "high", "medium", "low"} for t in tickets)
    assert all(t.expected_severity is not None and 0 <= t.expected_severity <= 100 for t in tickets)


def test_aggregate_accuracy_and_cost_per_correct():
    tickets = [Ticket(body="a", expected_team="Billing & Payments", expected_urgency="high"),
               Ticket(body="b", expected_team="Technical Support", expected_urgency="low")]

    def run(model, team, urg, cost, jev):
        r = ModelRun(model=model, is_jev=jev, result=result(team=team, urgency=urg, probs={team: 1.0}),
                     metrics=RunMetrics(latency_s=1, ttft_s=0.3, total_tokens=10, cost_usd=cost))
        r.decision = decide(r.result, 0.8, 0.8)
        return r

    all_runs = [[run("j", "Billing & Payments", "high", 0.01, True), run("o", "Technical Support", "medium", 0.002, False)],
                [run("j", "Technical Support", "medium", 0.01, True), run("o", "Technical Support", "low", 0.002, False)]]
    s = aggregate(tickets, all_runs, ["j", "o"], "j", TEAMS)
    j, o = s["models"]
    assert j["team_accuracy"] == 1.0 and o["team_accuracy"] == 0.5
    assert j["cost_per_correct_usd"] == pytest.approx(0.01) and o["cost_per_correct_usd"] == pytest.approx(0.004)
    assert j["urgency_accuracy"] == 0.5 and j["urgency_within_one"] == 1.0
    assert s["jev_confusion"]["matrix"]["Technical Support"]["Technical Support"] == 1


# ---------- severity ----------

def test_severity_level_boundaries():
    assert [severity_level(v) for v in (0, 29, 30, 59, 60, 79, 80, 100)] ==         ["SEV-4", "SEV-4", "SEV-3", "SEV-3", "SEV-2", "SEV-2", "SEV-1", "SEV-1"]
    assert severity_level(None) is None
    assert result(severity=85).model_dump()["severity_level"] == "SEV-1"  # computed field is serialized


def test_parse_severity_clamps_and_tolerates_missing():
    assert parse_severity(150) == 100 and parse_severity(-5) == 0 and parse_severity("72.4") == 72
    assert parse_severity(None) is None and parse_severity("high") is None
    triager = Triager(None, _settings(), load_teams(ROOT / "teams.yaml"))
    r = triager.parse_result({"team": "Technical Support", "urgency": "low", "confidence": 0.5})
    assert r.severity_score is None and r.severity_level is None


def test_severity_does_not_affect_decision():
    assert decide(result(severity=99), 0.8, 0.8) == decide(result(severity=None), 0.8, 0.8)


def test_aggregate_severity_metrics():
    tickets = [Ticket(body="a", expected_severity=90), Ticket(body="b", expected_severity=10)]

    def run(model, sev, jev):
        return ModelRun(model=model, is_jev=jev, result=result(severity=sev))

    all_runs = [[run("j", 80, True), run("o", 50, False)], [run("j", 20, True), run("o", 10, False)]]
    s = aggregate(tickets, all_runs, ["j", "o"], "j", TEAMS)
    j, o = s["models"]
    assert s["labeled_severity"]
    assert j["avg_severity"] == 50 and j["severity_diff_vs_jev"] is None
    assert o["severity_diff_vs_jev"] == pytest.approx(20)       # |50-80|, |10-20|
    assert j["severity_mae"] == pytest.approx(10) and o["severity_mae"] == pytest.approx(20)
    assert j["severity_level_accuracy"] == 1.0 and o["severity_level_accuracy"] == 0.5


# ---------- decision models (Jev via /api/alpha/decisions) ----------

from app.decisions import build_decisions_payload, is_decisions_model, parse_decisions  # noqa: E402

# shape captured from a real typesafe/jev-1.13 response
DECISIONS_RESPONSE = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {
        "team": {"type": "choice", "choice": "Technical Support",
                 "probabilities": {"Billing & Payments": 0, "Technical Support": 1, "Account & Access": 0}, "confidence": 1},
        "urgency": {"type": "choice", "choice": "critical",
                    "probabilities": {"critical": 0.9, "high": 0.1, "medium": 0, "low": 0}, "confidence": 0.85},
        "severity": {"type": "score", "score": 2.61, "legend": {"2": "SEV-2 (Major impact): ...", "3": "SEV-1 (Critical impact): ..."},
                     "probabilities": {"0": 0, "1": 0, "2": 0.39, "3": 0.61}, "confidence": 0.68},
        "escalate": {"type": "noul", "noul": 0.97},
        "sentiment": {"type": "choice", "choice": "negative", "probabilities": {"negative": 0.8}, "confidence": 0.8},
    },
    "usage": {"input_tokens": 523, "output_tokens": 76, "cost": 2.1966e-05},
    "provider": "TypeSafe",
}


def test_is_decisions_model():
    assert is_decisions_model("typesafe/jev-1.13") and is_decisions_model("~typesafe/jev-latest")
    assert is_decisions_model("openai/gpt-6-luna-decisions")
    assert not is_decisions_model("openai/gpt-5.4-mini")


def test_build_decisions_payload_uses_teams_and_guidelines():
    teams = load_teams(ROOT / "teams.yaml")
    p = build_decisions_payload("typesafe/jev-1.13", Ticket(subject="s", body="b", customer_tier="pro"), teams, "### Skill: x")
    assert p["model"] == "typesafe/jev-1.13"
    assert p["state"] == {"subject": "s", "body": "b", "customer_tier": "pro", "guidelines": "### Skill: x"}
    q = p["questions"]
    assert set(q) == {"team", "urgency", "severity", "escalate", "sentiment"}
    assert list(q["team"]["criteria"]) == [t.name for t in teams]
    assert q["severity"]["type"] == "score" and q["escalate"]["type"] == "noul"
    assert set(q["escalate"]["criteria"]) == {"true", "false"}
    assert "guidelines" not in build_decisions_payload("m", Ticket(body="b"), teams, "")["state"]


def test_parse_decisions_maps_to_triage_fields():
    d = parse_decisions(DECISIONS_RESPONSE)
    assert d["team"] == "Technical Support" and d["urgency"] == "critical"
    assert d["confidence"] == 0.85                      # weaker of team (1.0) and urgency (0.85)
    assert (d["team_confidence"], d["urgency_confidence"], d["severity_confidence"]) == (1, 0.85, 0.68)
    assert d["severity_score"] == pytest.approx(69.5 + 0.61 * 30.5)  # between the SEV-2 midpoint and 100
    assert d["escalate"] is True and d["escalate_probability"] == 0.97
    assert d["summary"] == "" and d["suggested_reply"] == "" and d["next_steps"] == []
    assert "SEV-1 61%, SEV-2 39%" in d["severity_reason"]
    with pytest.raises(ValueError):
        parse_decisions({"answers": {}})


@pytest.mark.asyncio
async def test_jev_decisions_end_to_end_and_chat_fallback():
    teams = load_teams(ROOT / "teams.yaml")
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append((req.url.path, body["model"]))
        if req.url.path.endswith("/alpha/decisions"):
            return httpx.Response(200, json=DECISIONS_RESPONSE)
        # a decisions model not recognized by id: chat returns OpenRouter's 400
        return httpx.Response(400, json={"error": {"message": f"{body['model']} is a decisions model and cannot be used with the chat/completions endpoint."}})

    client = OpenRouterClient("k", "https://or.test/api/v1", 5)
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    triager = Triager(client, _settings(jev_model="typesafe/jev-1.13", compare_models=["acme/new-decider"]), teams)
    runs, _ = await triager.run_ticket(Ticket(subject="API 500", body="checkout is down"))
    jev, other = runs

    assert ("/api/alpha/decisions", "typesafe/jev-1.13") in calls
    assert ("/api/v1/chat/completions", "typesafe/jev-1.13") not in calls       # detected by id, no chat attempt
    assert jev.api == "decisions" and jev.result.team == "Technical Support" and jev.result.severity_level == "SEV-1"
    assert jev.metrics.prompt_tokens == 523 and jev.metrics.cost_usd == 2.1966e-05 and jev.metrics.ttft_s is None
    assert jev.decision.prob_source == "decision model" and not jev.decision.automatic   # critical -> human
    assert jev.request["questions"]["team"]["type"] == "choice"
    assert other.api == "decisions" and other.result is not None                     # learned from the chat 400
    assert "acme/new-decider" in triager.decision_models


# ---------- severity ranks (severity.yaml) ----------

from app.severity import SeverityError, build_ranks, configure, load_severity, position_to_score, rank_for, ranks  # noqa: E402
from app.decisions import severity_criteria  # noqa: E402
from app.prompts import system_prompt  # noqa: E402


def test_severity_yaml_matches_defaults_and_derives_ranges():
    loaded = load_severity(ROOT / "severity.yaml")
    assert [(r.name, r.min_score, r.max_score) for r in loaded] ==         [("SEV-1", 80, 100), ("SEV-2", 60, 79), ("SEV-3", 30, 59), ("SEV-4", 0, 29)]
    assert loaded == ranks()  # app defaults == shipped file
    assert load_severity(ROOT / "missing.yaml") == ranks()


def test_severity_ranks_validation():
    with pytest.raises(SeverityError, match="min_score: 0"):
        build_ranks([{"name": "A", "min_score": 50, "description": "x"}, {"name": "B", "min_score": 10, "description": "y"}])
    with pytest.raises(SeverityError, match="unique"):
        build_ranks([{"name": "A", "min_score": 50, "description": "x"}, {"name": "a", "min_score": 0, "description": "y"}])
    with pytest.raises(SeverityError, match="description"):
        build_ranks([{"name": "A", "min_score": 50}, {"name": "B", "min_score": 0, "description": "y"}])
    with pytest.raises(SeverityError, match="at least 2"):
        build_ranks([{"name": "A", "min_score": 0, "description": "x"}])


def test_custom_ranks_drive_levels_prompt_and_jev_scale():
    default = ranks()
    custom = build_ranks([{"name": "P3", "label": "Low", "min_score": 0, "description": "minor"},
                          {"name": "P1", "label": "High", "min_score": 70, "description": "outage"},
                          {"name": "P2", "label": "Mid", "min_score": 40, "description": "blocked user"}])
    try:
        configure(custom)
        assert [r.name for r in ranks()] == ["P1", "P2", "P3"]                       # sorted most severe first
        assert rank_for(75).name == "P1" and rank_for(40).name == "P2" and rank_for(0).name == "P3"
        assert result(severity=55).model_dump()["severity_level"] == "P2"
        assert result(severity=55).model_dump()["severity_label"] == "Mid"
        assert "P1 (High), score 70-100: outage" in system_prompt(load_teams(ROOT / "teams.yaml"))
        assert [c.split(" ")[0] for c in severity_criteria()] == ["P3", "P2", "P1"]  # jev scale: least severe first
        for pos, expected in [(0, "P3"), (1, "P2"), (2, "P1")]:                      # whole positions stay in their rank
            assert rank_for(position_to_score(pos)).name == expected
    finally:
        configure(default)


# ---------- customer tiers (tiers.yaml) ----------

from app.prompts import user_prompt  # noqa: E402
from app.tiers import DEFAULT_TIERS, TiersError, describe, load_tiers  # noqa: E402


def test_tiers_yaml_keeps_defaults():
    loaded = load_tiers(ROOT / "tiers.yaml")
    assert [t.name for t in loaded] == ["free", "pro", "enterprise"]
    assert loaded == DEFAULT_TIERS
    assert load_tiers(ROOT / "missing.yaml") == DEFAULT_TIERS


def test_tiers_validation(tmp_path: Path):
    f = tmp_path / "t.yaml"
    f.write_text("tiers:\n  - name: gold\n  - name: Gold\n")
    with pytest.raises(TiersError, match="duplicate"):
        load_tiers(f)
    f.write_text("tiers: []\n")
    with pytest.raises(TiersError, match="non-empty"):
        load_tiers(f)


def test_tier_description_reaches_chat_and_jev():
    teams = load_teams(ROOT / "teams.yaml")
    assert describe("ENTERPRISE", DEFAULT_TIERS).startswith("Large contract")
    assert describe("gold", DEFAULT_TIERS) == "" and describe(None, DEFAULT_TIERS) == ""
    assert "Customer tier: pro (Paying customer" in user_prompt(Ticket(body="b", customer_tier="pro"), DEFAULT_TIERS)
    assert "Customer tier: gold\n" in user_prompt(Ticket(body="b", customer_tier="gold"), DEFAULT_TIERS)  # unknown: as-is
    assert "Customer tiers:\n- free:" in system_prompt(teams, DEFAULT_TIERS)
    state = build_decisions_payload("m", Ticket(body="b", customer_tier="enterprise"), teams, "", DEFAULT_TIERS)["state"]
    assert state["customer_tier"] == "enterprise" and state["customer_tier_description"].startswith("Large contract")


@pytest.mark.asyncio
async def test_run_ticket_reports_each_model_as_it_finishes():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=DECISIONS_RESPONSE)

    client = OpenRouterClient("k", "https://or.test/api/v1", 5)
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    triager = Triager(client, _settings(jev_model="typesafe/jev-1.13", compare_models=["typesafe/jev-1.14"]),
                      load_teams(ROOT / "teams.yaml"))
    seen = []

    async def on_done(i, run):
        seen.append((i, run.model, run.result is not None))

    runs, _ = await triager.run_ticket(Ticket(body="checkout is down"), on_done)
    assert sorted(seen) == [(0, "typesafe/jev-1.13", True), (1, "typesafe/jev-1.14", True)]
    assert len(runs) == 2


# ---------- urgency explanation ----------

from app.urgency import URGENCY_DEFINITIONS, urgency_meaning  # noqa: E402


def test_urgency_meaning_and_jev_urgency_probabilities():
    assert list(URGENCY_DEFINITIONS) == ["critical", "high", "medium", "low"]
    assert urgency_meaning("HIGH").startswith("A user or customer is blocked") and urgency_meaning(None) == ""
    assert result(urgency="low").model_dump()["urgency_meaning"] == URGENCY_DEFINITIONS["low"]
    d = parse_decisions(DECISIONS_RESPONSE)
    assert d["urgency_probabilities"] == {"critical": 0.9, "high": 0.1, "medium": 0, "low": 0}
    assert d["urgency_reason"] == "Jev probabilities: critical 90%, high 10%"
    teams = load_teams(ROOT / "teams.yaml")
    assert build_decisions_payload("m", Ticket(body="b"), teams, "")["questions"]["urgency"]["criteria"] == URGENCY_DEFINITIONS
    s = system_prompt(teams)
    assert "- critical: " + URGENCY_DEFINITIONS["critical"] in s and '"urgency_reason" explains' in s


# ---------- human review modes (HUMAN_REVIEW_MODE / ?review_mode=) ----------

def _jev_result(**kw) -> TriageResult:
    base = dict(team="Billing & Payments", team_probabilities={"Billing & Payments": 0.95, "Technical Support": 0.05},
                urgency="high", urgency_probabilities={"high": 0.9, "critical": 0.1}, confidence=0.9,
                team_confidence=0.95, urgency_confidence=0.9)
    base.update(kw)
    return TriageResult(**base)


def test_review_mode_default_is_unchanged():
    assert decide(_jev_result(), 0.8, 0.8).automatic
    d = decide(_jev_result(urgency="critical"), 0.8, 0.8)
    assert not d.automatic and d.mode == "default" and any("Critical" in r for r in d.reasons)


def test_review_mode_team_ignores_urgency():
    crit = _jev_result(urgency="critical", urgency_confidence=0.3, confidence=0.3)
    d = decide(crit, 0.8, 0.8, mode="team")
    assert d.automatic and d.mode == "team"                          # critical + low urgency confidence ignored
    tie = _jev_result(team_probabilities={"Billing & Payments": 0.5, "Technical Support": 0.45})
    assert any("Near tie" in r for r in decide(tie, 0.4, 0.8, mode="team").reasons)
    low = decide(_jev_result(team_confidence=0.5), 0.8, 0.8, mode="team")
    assert not low.automatic and low.reasons == ["Team confidence 0.50 is below 0.80"]


def test_review_mode_urgency_ignores_team():
    unsure_team = _jev_result(team_probabilities={"Billing & Payments": 0.5, "Technical Support": 0.5}, team_confidence=0.4)
    assert decide(unsure_team, 0.8, 0.8, mode="urgency").automatic   # team doubts ignored
    d = decide(_jev_result(urgency="critical", urgency_probabilities={"critical": 0.7}), 0.8, 0.8, mode="urgency")
    assert not d.automatic and d.reasons == ["Critical urgency always requires human review",
                                             "Urgency probability 0.70 is below 0.80"]
    # chat models have no per-question confidence: the overall one is used
    chat = TriageResult(team="Billing & Payments", urgency="low", confidence=0.5)
    assert decide(chat, 0.8, 0.8, mode="urgency").reasons == ["Urgency confidence 0.50 is below 0.80"]


@pytest.mark.asyncio
async def test_run_ticket_review_mode_override():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=DECISIONS_RESPONSE)   # critical urgency, team confidence 1.0

    client = OpenRouterClient("k", "https://or.test/api/v1", 5)
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    triager = Triager(client, _settings(jev_model="typesafe/jev-1.13", compare_models=[]), load_teams(ROOT / "teams.yaml"))
    (default,), _ = await triager.run_ticket(Ticket(body="checkout is down"))
    (team,), _ = await triager.run_ticket(Ticket(body="checkout is down"), review_mode="team")
    assert not default.decision.automatic and default.decision.mode == "default"
    assert team.decision.automatic and team.decision.mode == "team"
    assert team.decision.prob_source == "decision model"
