import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import ROOT, load_settings
from app.llm import OpenRouterClient, build_payload
from app.schemas import ComparisonResponse, Ticket
from app.severity import configure as configure_severity
from app.severity import load_severity, ranks as severity_ranks
from app.skills import load_skills
from app.teams import load_teams
from app.tiers import load_tiers
from app.urgency import urgency_list
from app.triage import Triager, aggregate

HERE = Path(__file__).resolve().parent
MAX_BATCH_ROWS = 1000


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    teams = load_teams(settings.teams_file)
    skills = load_skills(settings.skills_dir)
    configure_severity(load_severity(settings.severity_file))
    tiers = load_tiers(settings.tiers_file)
    client = OpenRouterClient(settings.api_key, settings.base_url, settings.request_timeout)
    await client.load_pricing()
    app.state.settings = settings
    app.state.teams = teams
    app.state.skills = skills
    app.state.tiers = tiers
    app.state.client = client
    app.state.triager = Triager(client, settings, teams, skills, tiers)
    yield
    await client.aclose()


app = FastAPI(title="jev ticket triage", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/teams")
async def get_teams(request: Request):
    return [t.__dict__ for t in request.app.state.teams]


@app.get("/api/urgency")
async def get_urgency():
    """Urgency levels and their meaning, most urgent first."""
    return urgency_list()


@app.get("/api/tiers")
async def get_tiers(request: Request):
    """Customer tiers loaded from tiers.yaml."""
    return [t.to_dict() for t in request.app.state.tiers]


@app.get("/api/severity")
async def get_severity():
    """Severity ranks loaded from severity.yaml, most severe first."""
    return [r.to_dict() for r in severity_ranks()]


@app.get("/api/skills")
async def get_skills(request: Request):
    return [s.__dict__ for s in request.app.state.skills]


@app.get("/api/sample", response_class=PlainTextResponse)
async def sample_csv():
    return (ROOT / "samples" / "tickets.csv").read_text(encoding="utf-8")


@app.get("/api/models")
async def get_models(request: Request):
    s = request.app.state.settings
    pricing = request.app.state.client.pricing
    return {
        "jev_model": s.jev_model,
        "auto_min_prob": s.auto_min_prob,
        "auto_min_confidence": s.auto_min_confidence,
        "models": [
            {
                "id": m,
                "is_jev": m == s.jev_model,
                "known": m in pricing if pricing else None,
                "prompt_per_1m_usd": pricing[m]["prompt"] * 1e6 if m in pricing else None,
                "completion_per_1m_usd": pricing[m]["completion"] * 1e6 if m in pricing else None,
            }
            for m in s.models
        ],
    }


@app.post("/api/triage", response_model=ComparisonResponse)
async def triage(ticket: Ticket, request: Request):
    runs, skills = await request.app.state.triager.run_ticket(ticket)
    return ComparisonResponse(ticket=ticket, skills=skills, runs=runs)


@app.post("/api/triage/stream")
async def triage_stream(ticket: Ticket, request: Request):
    """Same as /api/triage, streamed as NDJSON for progress: a 'start' line with the models, one 'model' line
    per model as it finishes (metrics + status), then a 'result' line with the full ComparisonResponse."""
    triager = request.app.state.triager
    models = request.app.state.settings.models
    queue: asyncio.Queue = asyncio.Queue()

    async def on_done(i: int, run):
        await queue.put({"type": "model", "index": i, "model": run.model, "ok": run.result is not None,
                         "error": run.error, "metrics": run.metrics.model_dump()})

    async def work():
        runs, skills = await triager.run_ticket(ticket, on_done)
        await queue.put({"type": "result", **ComparisonResponse(ticket=ticket, skills=skills, runs=runs).model_dump()})

    async def stream():
        yield json.dumps({"type": "start", "models": models, "jev_model": request.app.state.settings.jev_model}) + "\n"
        task = asyncio.create_task(work())
        try:
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    msg = getter.result()
                    yield json.dumps(msg) + "\n"
                    if msg["type"] == "result":
                        break
                else:
                    getter.cancel()
                    if task.exception():
                        yield json.dumps({"type": "error", "message": str(task.exception())}) + "\n"
                        break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/preview")
async def preview(ticket: Ticket, request: Request):
    """Prompt and JSON body that would be sent to jev, without calling any model."""
    triager = request.app.state.triager
    user, skills = triager.build_user_prompt(ticket)
    messages = triager.messages(user)
    jev = request.app.state.settings.jev_model
    if jev in triager.decision_models:
        jev_api, jev_request = "decisions", triager.decisions_payload(jev, ticket)
    else:
        jev_api, jev_request = "chat", build_payload(jev, messages, {"type": "json_schema", "json_schema": triager.schema})
    return {"skills": skills, "messages": messages, "jev_api": jev_api, "request": jev_request}


def _severity_label(raw: str | None) -> int | None:
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 100 else None


def parse_csv(raw: bytes) -> list[Ticket]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames or "body" not in [f.strip().lower() for f in reader.fieldnames]:
        raise HTTPException(400, "CSV must have a header row with at least a 'body' column")
    tickets = []
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if not row.get("body"):
            continue
        tickets.append(Ticket(
            subject=row.get("subject", ""),
            body=row["body"],
            customer_tier=row.get("customer_tier") or None,
            expected_team=row.get("expected_team") or None,
            expected_urgency=row.get("expected_urgency") or None,
            expected_severity=_severity_label(row.get("expected_severity")),
        ))
    if not tickets:
        raise HTTPException(400, "CSV has no tickets with a non-empty 'body'")
    if len(tickets) > MAX_BATCH_ROWS:
        raise HTTPException(400, f"CSV has {len(tickets)} tickets; the maximum is {MAX_BATCH_ROWS}")
    return tickets


@app.post("/api/batch")
async def batch(request: Request, file: UploadFile = File(...)):
    """Streams NDJSON: a 'start' line, one 'ticket' line per finished ticket, then a 'summary' line."""
    tickets = parse_csv(await file.read())
    state = request.app.state
    models = state.settings.models

    async def run(i: int, t: Ticket):
        runs, skills = await state.triager.run_ticket(t)
        return i, runs, skills

    async def stream():
        yield json.dumps({"type": "start", "total": len(tickets), "models": models,
                          "jev_model": state.settings.jev_model}) + "\n"
        results: list = [None] * len(tickets)
        for fut in asyncio.as_completed([run(i, t) for i, t in enumerate(tickets)]):
            i, runs, skills = await fut
            results[i] = runs
            yield json.dumps({"type": "ticket", "index": i, "ticket": tickets[i].model_dump(), "skills": skills,
                              "runs": [r.model_dump(exclude={"request"}) for r in runs]}) + "\n"
        summary = aggregate(tickets, results, models, state.settings.jev_model, [t.name for t in state.teams])
        yield json.dumps({"type": "summary", **summary}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")
