# CUBE Build-A-Thon Round 3 Recovery Manager

CUBE Recovery Manager is a five-agent commerce workflow application. It
follows one unit through receiving, preparation, packing, returns, and
recovery, while preserving the evidence needed to explain every decision.

The application is an integration of the existing agents; it is not a
replacement implementation. Agents do not call each other. The orchestrator
owns sequencing, workflow state, evidence propagation, retries, failures,
resume, overrides, and final-outcome derivation.

## Architecture

```text
                Streamlit UI
                     |
                     v
              Orchestrator
                     |
    +----------------+----------------+
    |        |        |        |       |
    v        v        v        v       v
Receiving   Prep    Pack   Returns  Recovery
    |        |        |        |       |
    +--------+--------+--------+-------+
                     |
                     v
              Evidence Store
                     |
                     v
              Final Outcome
```

The normal route is:

```text
Receiving -> Prep (FBA) or Pack (MFN) -> Returns (when returned) -> Recovery
```

Every agent has an `agent.json` manifest and an in-process `handle(request)`
entry point. The orchestrator can also call an agent over HTTP using the
contract endpoints. The five agents are:

- **Receiving** — receiving identity, quantity, carton, damage, and quality checks.
- **Prep** — FBA preparation checks; AI observes when captures and credentials
  are available, while deterministic rules produce the contract verdict.
- **Pack** — merchant-fulfilled packing checks, with optional Gemini vision.
- **Returns** — returns identity, completeness, condition, and disposition;
  its live path uses a batched OpenAI-compatible vision call.
- **Recovery** — evaluates charges against all upstream evidence and does not
  claim unsupported charges.

The Streamlit layer does **not** call agents directly. It calls the
orchestrator's reusable Python functions. The API layer exposes the same
orchestrator through FastAPI.

## Current entry point

Start the application with:

```powershell
python -m streamlit run app.py
```

The UI supports:

- Creating or loading a workflow
- Entering organization, case/unit, route, return, and JSON context data
- Viewing progress for all five stages
- Viewing attempts, timings, results, and failures
- Filtering traceable evidence by source agent
- Reviewing missing evidence and explicit contradictions
- Viewing the Recovery decision and recommended action
- Recording append-only human overrides
- Inspecting workflow timestamps, errors, overrides, and transition history

## API

Start the API separately when an HTTP front door is required:

```powershell
python -m uvicorn orchestration.api:app --port 8100
```

Available endpoints:

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Orchestrator and agent availability |
| POST | `/workflows` | Create and run a workflow |
| GET | `/workflows/{id}` | Read workflow state |
| GET | `/workflows/{id}/evidence` | Read workflow state plus evidence |
| POST | `/workflows/{id}/resume` | Resume a failed or paused workflow |
| POST | `/workflows/{id}/overrides` | Record an operator override |

Example:

```powershell
Invoke-RestMethod http://127.0.0.1:8100/health
```

The API has no authentication. Do not expose it publicly until authentication
and tenant authorization have been added.

## Persistence and workflow behavior

The existing JSON-backed `orchestration.store.FileStore` persists:

```text
out/workflows/<workflow_id>.json
out/evidence/<record_id>.json
```

Workflow state survives Streamlit reruns and process restarts. Evidence is
retained and written immutably by record ID. Overrides are retained as
append-only workflow history. Failed or paused workflows can be resumed
without deleting earlier evidence.

The orchestrator:

1. Creates a workflow ID from the organization and unit.
2. Loads the flow and agent manifests.
3. Invokes each applicable `handle(request)` in order.
4. Passes all earlier evidence and overrides to later stages.
5. Validates schema, stage, workflow, tenant, consistency, and content hash.
6. Stores valid evidence and records degraded evidence for failures.
7. Retries transient timeout/unavailable failures.
8. Supports resume from failed or halted stages.
9. Applies human overrides without rewriting agent evidence.
10. Derives the final outcome from the stored evidence chain.

## Evidence policy

Evidence includes workflow, organization, subject, stage, record ID, checks,
verdicts, confidence, inputs, upstream references, timestamps, and a content
hash. Conclusions are traceable to the records and inputs that support them.

Missing evidence is represented as missing, pending, or uncertain. It is never
converted into positive evidence. Contradictions are represented explicitly
with their source records. Visual evidence that cannot be confirmed may
produce `UNCERTAIN` or `PENDING`, and Recovery must not claim an unsupported
charge.

Failure semantics are explicit:

- A timeout becomes a retryable recorded failure.
- An unavailable agent becomes a retryable recorded failure.
- A failed or invalid agent output becomes an error/pending evidence record.
- Retries increase the stage attempt count.
- Resume preserves previous evidence and retries the failed stage.
- `UNCERTAIN` remains uncertain until an authorized human override is recorded.

## AI and replay/fallback behavior

The agents have real model-backed paths when the required captures and
credentials are present. They also have deliberately labelled fallback paths
for local operation and contract tests:

- Receiving uses deterministic sample-data fallback without a Gemini key.
- Prep returns honest `UNCERTAIN` evidence when visual observation is
  unavailable.
- Pack can replay its sample CSV when no images are supplied.
- Returns uses a labelled CSV replay only when no photos are supplied; that
  replay copies an operator disposition and is not an agent judgment.
- Recovery uses deterministic evidence rules and may optionally call Gemini.

Replay/fallback data is synthetic or operator-provided test data. It is not
real-world evidence and is never silently represented as live model evidence.

## Installation

From a clean checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Optional live model dependencies:

```powershell
pip install -r requirements-live.txt
```

The core requirements are sufficient to run the default application and test
suite without model credentials. Copy `.env.example` to `.env` only when
local configuration is needed; never commit `.env`.

## Configuration

Configuration is read from environment variables. Relevant names include:

- `ORCH_MODE`, `ORCH_FLOW`
- `OUT_DIR`, `DATA_DIR`, `INPUT_DIR`
- `RECEIVING_URL`, `PREP_URL`, `PACK_URL`, `RETURNS_URL`, `RECOVERY_URL`
- `GEMINI_API_KEY`, `GOOGLE_API_KEY`
- `VLM_API_KEY`, `VLM_BASE_URL`, `VLM_MODEL`
- `VLM_TIMEOUT_SECONDS`, `VLM_MAX_RETRIES`
- `LOG_LEVEL`, `LOG_FORMAT`

See [.env.example](.env.example) for placeholder names only.

## Testing

Run:

```powershell
python -m pytest -q
```

Current expected result:

```text
92 passed, 1 skipped, 0 failed
```

The one skipped test is the historical organiser-stub golden-output check.
It is skipped because the manifests now describe the integrated real agent
implementations rather than the original all-stub configuration.

Additional checks:

```powershell
python -m compileall .
python -m streamlit run app.py
```

## Security

Organization IDs are carried through workflow requests, evidence, storage, and
agent validation. Agents reject unknown or cross-tenant subjects, and the
orchestrator rejects evidence for the wrong organization or subject.

The API is currently unauthenticated. Add authentication, authorization, and
tenant-aware access controls before any public deployment. Never put API keys,
tokens, passwords, or private keys in source, `.env.example`, logs, evidence,
or Git history.

## Repository layout

```text
app.py                    Streamlit UI
agents/                   Five existing agent implementations
orchestration/            Flow, clients, API, persistence, and rollup
shared/                   Schemas, contracts, hashing, and utilities
tests/                    Contract, integration, and end-to-end tests
data/sample/              Synthetic sample data
data/input/               Local capture inputs
examples/                 Valid and historical/replay-oriented examples
requirements.txt          Core runtime and test dependencies
requirements-live.txt     Optional live model dependencies
```

### Example data classification

- `examples/uncertain-path` — **valid**: demonstrates missing visual evidence
  and an honest uncertain outcome.
- `examples/failure-path` — **valid**: demonstrates timeout/unavailable-agent
  failure semantics.
- `examples/happy-path` — **historical/test fixture**: generated for the
  original organiser-stub contract and not a claim that current visual
  evidence is complete.
- `examples/end-to-end` — **historical/test fixture**: retained for regression
  coverage and replay context; it must not be interpreted as live evidence.

The application does not load these historical examples as real production
evidence.
