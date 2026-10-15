# jev — Support Ticket Triage & LLM Comparison

jev is a web application that triages customer support tickets with an LLM. For each ticket it decides:

- **Which team** handles it (from an editable list of teams)
- **How urgent** it is (critical / high / medium / low) and why
- **How severe** the issue is: a 0–100 score with a level (SEV-1 to SEV-4) and a reason
- A **category**, **sentiment** and short **summary**
- **Suggestions**: a draft reply (in the ticket's language), next steps, and whether to escalate
- Whether the assignment can be **automatic** or **requires human intervention**

Every ticket goes to **jev** (TypeSafe's decision model, `typesafe/jev-1.13`) and to every comparison model you configure. The results are shown side by side with **response time, time to first token, tokens and estimated cost**. All models are called through a single **OpenRouter** API key.

Triage can be steered with **Agent Skills**: `SKILL.md` files containing team- or situation-specific instructions, which are added to the prompt when a ticket matches them.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- An [OpenRouter](https://openrouter.ai/) API key

## Setup

```bash
uv sync
cp .env.example .env     # then edit .env: API key, JEV_MODEL, COMPARE_MODELS
uv run main.py
```

Open http://localhost:8000. Set `HOST` / `PORT` to change where the app listens.

## How jev is called (Decisions API)

jev is a **decision model**, not a chat model. OpenRouter rejects it on `/chat/completions`, so the app calls it through
`POST /api/alpha/decisions`:

- **`state`** holds the ticket (`subject`, `body`, `customer_tier`) plus, under `guidelines`, the instructions of any matching skills.
- **`questions`** asks the triage as typed questions:

| Question | Type | Maps to |
|---|---|---|
| `team` | `choice` (one option per team in `teams.yaml`) | team, team probabilities |
| `urgency` | `choice` (critical / high / medium / low) | urgency |
| `severity` | `score` (5 ordered levels) | severity score: position 0–4 → 0–100 |
| `escalate` | `noul` (probability of "yes") | escalate (≥ 0.5) and P(escalate) |
| `sentiment` | `choice` | sentiment |

jev answers with **probabilities computed by the model**, which are not self-reported like the chat models' numbers. Its confidence for the triage is the lower of its team and urgency confidences. jev **does not generate text**, so its card has no summary, suggested reply or next steps. The urgency and severity explanations show its probabilities instead.

The app detects decision models by id (`typesafe/…`, `…-decisions`). It also detects them automatically when OpenRouter answers a chat request with "is a decisions model", and retries through the Decisions API, so other decision models can be added to `COMPARE_MODELS` too. The chat models keep using `/chat/completions` with the prompt and JSON schema.

## Configuration

### `.env`

| Variable | Description | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | Your OpenRouter key (used for all models) | — (required) |
| `OPENROUTER_BASE_URL` | OpenRouter API base URL | `https://openrouter.ai/api/v1` |
| `JEV_MODEL` | OpenRouter model id used as jev, e.g. `typesafe/jev-1.13` | — (required) |
| `COMPARE_MODELS` | Comma-separated model ids to compare against jev | empty |
| `TEAMS_FILE` | Path to the teams file | `teams.yaml` |
| `SEVERITY_FILE` | Path to the severity ranks file | `severity.yaml` |
| `TIERS_FILE` | Path to the customer tiers file | `tiers.yaml` |
| `SKILLS_DIR` | Folder containing the Agent Skills | `skills` |
| `MAX_SKILLS_PER_TICKET` | Maximum number of skills added to one ticket's prompt | `3` |
| `AUTO_ASSIGN_MIN_PROB` | Minimum top-team probability for automatic assignment | `0.80` |
| `AUTO_ASSIGN_MIN_CONFIDENCE` | Minimum model confidence for automatic assignment | `0.80` |
| `MAX_CONCURRENCY` | Maximum number of model requests running at the same time | `4` |
| `REQUEST_TIMEOUT` | Seconds before a model call times out | `60` |

`.env` is git-ignored. Never commit your key. The header of the web page shows the configured models, with a ⚠ next to any id that OpenRouter doesn't recognize.

### `teams.yaml`

The teams that tickets can be routed to. Edit this file to add, remove or rename teams, then **restart the app**. The file is read at startup. A good `description` and a few `examples` noticeably improve routing.

```yaml
teams:
  - name: Billing & Payments
    description: Invoices, charges, refunds, payment methods, subscription plan changes.
    examples: ["I was charged twice", "Need a copy of my invoice"]
  - name: Technical Support
    description: Bugs, errors, outages, performance problems, integrations and API issues.
    examples: ["The app crashes on login", "API returns 500"]
  - name: Account & Access
    description: Login, password resets, MFA, user/role management, account closure.
    examples: ["Can't reset my password", "Add a user to my workspace"]
  - name: Product & Feature Requests
    description: How-to questions, feature requests, product feedback, onboarding.
    examples: ["Can you add dark mode?", "How do I export reports?"]
  - name: Security & Compliance
    description: Suspected breaches, phishing, data/privacy (GDPR) requests, vulnerability reports.
    examples: ["Someone accessed my account", "Delete all my personal data"]
```

Team names must be unique, and the file needs at least two teams. If the file is malformed, the app refuses to start and says what is wrong.

### `tiers.yaml`

The customer tiers: the plan or importance of the customer who opened the ticket. They fill the **Customer tier** selector and are explained to the models, which use them to weigh **urgency** and **escalation** (not the team). Defaults:

```yaml
tiers:
  - name: free
    description: Free plan. No SLA; best-effort support.
  - name: pro
    description: Paying customer on a self-serve plan. Standard support SLA.
  - name: enterprise
    description: Large contract with a dedicated SLA, many users and high revenue at stake. Issues blocking an enterprise account deserve higher urgency and escalation.
```

Add, rename or remove tiers and restart the app. Names must be unique. The chat models get the list of tiers in the system prompt and the ticket's tier with its description in the user prompt. jev gets `customer_tier` and `customer_tier_description` in its `state`. In a batch CSV, `customer_tier` can be any text: tiers not listed in the file are passed to the models as-is, without a description. If the file is missing, the defaults above are used.

### Agent Skills (`skills/*/SKILL.md`)

A skill is a folder containing a `SKILL.md` file: YAML frontmatter followed by Markdown instructions.

```markdown
---
name: billing-refunds
description: Policy for duplicate charges, refunds, invoices and failed payments.
triggers: [charged, refund, invoice, cobro, factura]   # case-insensitive, matched against subject + body
priority: 0        # optional; higher wins when more skills match than MAX_SKILLS_PER_TICKET
always: false      # optional; true = include for every ticket
---
- Duplicate charges: urgency high, next step "Verify in payment processor and refund the duplicate".
- Pro-rated refunds apply only to cancellations within the first 7 days of a billing cycle.
```

How it works:

1. Skills are loaded at startup. A malformed `SKILL.md` stops startup with a clear error.
2. For each ticket, every skill whose trigger appears in the subject or body is selected, highest `priority` first, up to `MAX_SKILLS_PER_TICKET`.
3. The selected skills' instructions are added to the prompt **for every model alike**, so the comparison stays fair. The extra prompt tokens show up in the token and cost metrics.
4. The UI shows which skills were applied to each ticket.

Four example skills are included: `security-incident`, `billing-refunds`, `outage-response` and `account-recovery`. Use them as templates for your own policies, SLAs and reply rules. To add one, create `skills/<name>/SKILL.md` and restart. The **Configuration** tab lists everything that is loaded.

## Using the app

### Single ticket

1. Enter the subject, the body and (optionally) the customer tier. **Load example** cycles through a few sample tickets.
2. Click **Triage**. All models run in parallel, and a progress panel shows one bar per model with the elapsed time. Each bar turns green (with latency, tokens and cost) or red (with the error) as that model finishes.
3. **jev decision** (in larger type), starting with five big-number tiles. Each tile has the text that explains its number:
   - **Team**: the assigned team, with its probability, the runner-up team and whether the auto-assign threshold is met.
   - **Confidence**: e.g. 97%, green, amber or red against the threshold, plus how it's calculated.
   - **Urgency**: e.g. CRITICAL, with the level's meaning and why it was chosen.
   - **Severity**: e.g. SEV-1 96/100, with the rank's range and description and why it was chosen.
   - **Escalate**: YES/NO with P(escalate).

   When jev is called through the Decisions API, each tile carries a tag with the question type behind it: `choice` (Team, Urgency), `score` (Severity) or `noul` (Escalate). Confidence is tagged `from choice`, because it isn't a question: it comes from the confidences of the choice answers. Hover over a tag to see what the type returns.

   Below the tiles: the AUTOMATIC / REQUIRES HUMAN INTERVENTION verdict with its reasons, gauges for team probability, confidence and severity, the urgency block, and probability bars per team (and per urgency level for jev).
4. **Comparison table**: for each model, its team, urgency, decision, latency, TTFT, input/output/total tokens, cost (USD), and whether it agrees with jev. ★ marks the best value in each column.
5. One card per model with its team, urgency, severity, decision and explanations. The chat models' cards also have a draft reply and next steps; jev's card shows its probabilities instead, since a decision model doesn't generate text. jev's card is highlighted.
6. **Prompt & request**: the exact JSON sent to jev (`state` + `questions`), the system and user prompts sent to the chat models (including any skills added), and the JSON sent to each other model, with a Copy button. The API key travels in an HTTP header and is never shown.

**Preview prompt** shows the prompt and the JSON that would be sent to jev **without calling any model** (no tokens spent).

### Automatic vs human review

jev returns a probability for each team plus an overall confidence. A ticket is marked **AUTOMATIC** only when all of these hold:

- the top team's probability ≥ `AUTO_ASSIGN_MIN_PROB`
- confidence ≥ `AUTO_ASSIGN_MIN_CONFIDENCE`
- the top two teams are not nearly tied (within 0.15), and the chosen team is the most probable one
- urgency is not `critical` (critical tickets always get human review)

Otherwise it is marked **REQUIRES HUMAN INTERVENTION**, and each reason is listed. If the provider returns token logprobs, the team probability is the lower of the logprob-based value and the self-reported one.

> The self-reported probabilities are not calibrated. Run a labeled batch and look at **Acc. when auto** (the accuracy on tickets that would have been auto-assigned) before you trust the thresholds, then tune them in `.env`.

The same rule is applied to the other models, so their auto-assign rates can be compared.

**Escalate is not human intervention.** *Human intervention* is the app's decision about whether the team **assignment** can be automatic. *Escalate* is the model's judgment that the ticket should go **beyond the normal support queue** (on-call engineering, a senior agent, management). Escalate doesn't change the automatic / human-review decision. A ticket can be auto-assigned and escalated (jev is sure it's Technical Support and it's a production outage), or need human review without being escalated (a vague ticket that isn't urgent).

### Urgency explanation

Urgency is how fast the ticket needs a response. The four levels are defined once in [app/urgency.py](app/urgency.py) and that same text is used in the chat prompt, in jev's urgency question and in the UI:

| Urgency | Meaning |
|---|---|
| critical | Outage, security incident, or many users blocked; revenue or data at risk right now. Respond immediately. |
| high | A user or customer is blocked or significantly impacted. Needs action today. |
| medium | Impaired but has a workaround, or a time-bound request. Handle within the normal SLA. |
| low | Questions, feedback, cosmetic issues; no time pressure. |

Every result shows the level, **what it means**, and **why** it was chosen:
- **Chat models** write a short reason citing facts from the ticket (`urgency_reason`).
- **jev** doesn't generate text, so its explanation is the level's meaning plus its probability for each level (e.g. "high 91%, critical 9%"), shown as bars in jev's panel.

jev's panel has an **Urgency** block with the level, its meaning, the reason, the probability bars and a legend of all levels. The **Configuration** tab lists the levels, and `GET /api/urgency` returns them.

### Severity score

Every model returns a **severity score from 0 to 100**, which places the ticket in a **severity rank** (SEV-1 to SEV-4 by default). Severity measures how serious the impact is: how many users are affected, revenue or data loss, security risk, and whether there is a workaround. **Urgency** measures how fast you need to respond. The two can differ: a GDPR request can be medium urgency but low severity.

The ranks live in **`severity.yaml`**, which you can edit like `teams.yaml` (restart the app to apply changes):

| Rank | Score | Label | Meaning |
|---|---|---|---|
| SEV-1 | 80–100 | Critical impact | Confirmed security breach or data loss, full production outage, or core functionality broken for many users or a whole enterprise account with revenue impact |
| SEV-2 | 60–79 | Major impact | An important feature broken or a key customer blocked, with no reasonable workaround |
| SEV-3 | 30–59 | Moderate impact | A single user blocked, or degraded service with a workaround |
| SEV-4 | 0–29 | Low impact | Minor or cosmetic issue, feedback, how-to or billing question, time-bound request |

```yaml
ranks:
  - name: SEV-1
    label: Critical impact
    min_score: 80          # inclusive; the rank runs up to the next rank's min_score - 1 (or 100)
    description: Confirmed security breach or data loss, full production outage, ...
```

Rules: at least 2 ranks, unique names, different `min_score` values, and one rank with `min_score: 0`. The order in the file doesn't matter. You can rename the ranks (e.g. P1–P4) or add more.

How the ranks are used:
- **Chat models** get the ranks, with their score ranges, as the rubric in the system prompt, and are asked for a score inside the best-fitting rank.
- **jev** gets them as the ordered scale of its `score` question (least severe first) and answers a position from 0 to n-1, with a probability for each rank. The app turns the position into 0–100 so that a whole position always falls inside its rank, and shows the probability of each rank as the explanation (e.g. "SEV-1 61%, SEV-2 39%").
- **The UI** shows the rank and its label on every result, a gauge with the rank zones, a legend table (with the current rank highlighted) under jev's gauge, and all the ranks in the **Configuration** tab. `GET /api/severity` returns them.

If a model doesn't return a score, its triage is still used and severity shows as "—". Severity is **informational only**: it does not change the automatic / human-review decision. Skills can refine the criteria (see `security-incident` and `outage-response`).

### Batch (CSV)

Upload a CSV, or click **Run sample CSV** to use `samples/tickets.csv`. A progress bar updates as each ticket finishes.

| Column | Required | Description |
|---|---|---|
| `subject` | no | Ticket subject |
| `body` | yes | Ticket text |
| `customer_tier` | no | e.g. `free`, `pro`, `enterprise` |
| `expected_team` | no | Correct team (enables accuracy metrics) |
| `expected_urgency` | no | Correct urgency (enables accuracy metrics) |
| `expected_severity` | no | Correct severity, 0–100 (enables severity metrics) |

Comma, semicolon and tab delimiters are detected automatically. Maximum 1,000 tickets per batch.

The summary shows, for each model:

- average and p95 latency, average and p95 TTFT, total tokens, total cost, projected **cost per 1,000 tickets**
- % auto-assigned, errors, agreement with jev (team / urgency)
- average severity, and the mean absolute severity difference from jev on the same tickets
- with labels: **team accuracy**, **accuracy on auto-assigned tickets**, **cost per correct triage**, and urgency accuracy (exact match and within one level), **severity MAE** (mean absolute error against `expected_severity`) and **SEV level accuracy**
- bar charts, jev's **confusion matrix** (expected vs predicted team), and a table of per-ticket results

**Download results** exports one CSV row per ticket × model, with every field and metric.

`samples/tickets.csv` has 40 labeled tickets: all five teams, every urgency level, a severity label for each ticket, free/pro/enterprise tiers, 5 tickets in Spanish, and 5 deliberately ambiguous tickets at the end (e.g. "Something wrong with my account and bill") that should go to human review.

## How metrics are measured

- **Latency:** wall-clock time of each API call, as seen by the app (includes network and OpenRouter queueing).
- **Time to first token (TTFT):** chat responses are streamed, so this is how long until the model starts answering. The Decisions API doesn't stream, so jev's TTFT shows as "—" and only its total latency counts.
- **Tokens:** `prompt_tokens` / `completion_tokens` reported in OpenRouter's usage data (for jev: `input_tokens` / `output_tokens`; jev only charges for input tokens).
- **Cost:** the USD cost OpenRouter reports for each request. If it is missing, the cost is calculated from the model's price in OpenRouter's `/models` list (marked with `*`).
- Requests use `temperature: 0`. Structured output is requested with a strict JSON schema, falling back to JSON mode or plain text for models that don't support it.

Latency varies from run to run, and `MAX_CONCURRENCY` affects it too. Compare models on a batch, not on a single ticket.

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/api/teams` | Teams loaded from `teams.yaml` |
| `GET` | `/api/urgency` | Urgency levels and their meaning |
| `GET` | `/api/tiers` | Customer tiers loaded from `tiers.yaml` |
| `GET` | `/api/severity` | Severity ranks loaded from `severity.yaml` |
| `GET` | `/api/skills` | Skills loaded from `skills/` |
| `GET` | `/api/models` | Configured models, their prices, and the thresholds |
| `GET` | `/api/sample` | The sample CSV |
| `POST` | `/api/preview` | JSON `{subject, body, customer_tier?}` → the skills, messages and JSON body that would be sent to jev (no model is called) |
| `POST` | `/api/triage/stream` | Same input; NDJSON stream: a `start` line, one `model` line per model as it finishes (status + metrics), then a `result` line with the full response (used by the UI for the progress bars) |
| `POST` | `/api/triage` | JSON `{subject, body, customer_tier?}` → applied skills, plus results, metrics and the JSON request (`request`) for each model |
| `POST` | `/api/batch` | `multipart/form-data` with a `file` CSV → NDJSON stream: a `start` line, one `ticket` line per ticket as it finishes, then a `summary` line |

Examples:

```bash
curl -X POST http://localhost:8000/api/triage \
  -H "Content-Type: application/json" \
  -d '{"subject": "Charged twice", "body": "My card was charged twice for October.", "customer_tier": "pro"}'

curl -N -F file=@samples/tickets.csv http://localhost:8000/api/batch
```

## Troubleshooting

**`CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`**: an antivirus (e.g. Kaspersky) or a corporate proxy is inspecting HTTPS traffic and re-signing it with its own root CA. The app verifies certificates against the **operating system's certificate store** (via [`truststore`](https://pypi.org/project/truststore/)), so it works as long as that root CA is installed in Windows/macOS/Linux, which the antivirus or IT department normally does. If the error persists, install the proxy's root CA in the OS store. Don't disable certificate verification.

## Tests

```bash
uv run pytest
```

The tests cover JSON parsing, probability normalization, the auto-assign rule at its threshold boundaries, severity levels, clamping and metrics, the logprob calculation, cost fallback, loading teams and skills, the sample CSV, batch aggregation, and an end-to-end run against a mocked OpenRouter stream. They make no real API calls.

## Project structure

```
app/
  config.py      settings from .env
  teams.py       teams.yaml loader
  severity.py    severity.yaml loader, score -> rank, jev scale mapping
  tiers.py       tiers.yaml loader
  skills.py      SKILL.md loader, matching and prompt block
  prompts.py     triage prompt + JSON schema (built from the teams)
  llm.py         OpenRouter client (chat streaming + Decisions API): latency, TTFT, tokens, cost
  decisions.py   jev payload (state + typed questions) and answer mapping
  triage.py      runs all models in parallel, auto-assign decision, batch aggregation
  main.py        FastAPI app and routes
  templates/     HTML
  static/        JS / CSS
skills/          Agent Skills (one folder per skill, each with a SKILL.md)
samples/tickets.csv
teams.yaml
severity.yaml
tiers.yaml
.env.example
main.py          entry point
tests/
```
