# SwasthiQ — Clinic Front Desk Agent

A deterministic Python REST API + React console for the synthetic Sunrise Clinic front-desk assignment.

## Run in one command

From the repository root:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r backend/requirements.txt && uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

In another terminal, run the React UI:

```bash
cd frontend && npm install && npm run dev
```

Evaluation endpoint:

`POST http://localhost:8000/agent/run`

The supplied runner works unchanged:

```bash
python3 runner.py --url http://localhost:8000/agent/run
python3 runner.py --repeat 3
```

## Architecture

- `/backend` — FastAPI REST API and the six ground-truth tools.
- `/frontend` — React/Vite Handoff Queue and Conversation Detail screens.
- `/adversarial` — eight additional cases designed to break naive agents.
- `/tests` — non-happy-path and baseline tests.
- `clinic.json`, `schema.md`, `runner.py`, `conversations/` — supplied starter data/harness.

The runtime agent intentionally does **not** use an LLM. The brief permits this, and deterministic policy is useful for a safety-critical front-desk workflow where the tool layer is the source of truth.

## API contract

### Request

```json
{
  "conversation_id": "cv_0001",
  "today": "2026-10-01",
  "turns": ["caller turn 1", "caller turn 2"]
}
```

### Response

```json
{
  "conversation_id": "cv_0001",
  "tool_calls": [{"name":"search_slots","arguments":{"doctor_id":"dr_rao","date":"2026-10-03"}}],
  "terminal_state": "booked",
  "escalation_reason": null,
  "patient_id": "pt_0014",
  "appointment_id": "ap_0026",
  "reply": "...",
  "metrics": {"turns": 2, "tokens": 20, "latency_ms": 3}
}
```

The response follows `schema.md`. Failed/attempted tool calls are included in `tool_calls`. The reply never introduces a slot, patient or appointment that was not returned by the tool layer.

## Six tools

1. `search_slots(doctor_id, date)` — computes working windows, leave/holiday closures and free 15-minute slots.
2. `book_appointment(patient_id, doctor_id, date, start, caller_id?)` — validates patient, authorization and free slot before creating a deterministic appointment ID.
3. `reschedule_appointment(patient_id, new_date, new_start, doctor_id?, old_date?, caller_id?)` — finds exactly one existing appointment and validates the destination before mutation.
4. `cancel_appointment(patient_id, date?, doctor_id?, caller_id?)` — requires exactly one matching appointment and authorization.
5. `lookup_patient(name?, phone?, target_name?)` — returns all candidates and never picks an ambiguous record itself.
6. `escalate_to_human(reason, detail)` — only accepts the five schema-defined escalation reasons.

## Consistency on update

Mutations are performed only after the tool layer validates the current state. Booking validates that the slot is free inside a lock-protected critical section; concurrent requests reserve the slot so both cannot succeed. Rescheduling validates the destination slot before changing the existing appointment. Cancellation requires exactly one matching booked appointment. Every request reloads `clinic.json`, so example conversations remain isolated as required by the harness.

## Safety / restraint policy

- Urgent symptoms stop the workflow immediately and escalate `clinical_urgent`.
- Medication/dose/diagnostic questions escalate `medical_advice`.
- Acting for another patient without a listed guardian relationship escalates `not_authorised`.
- Ambiguous identity escalates `ambiguous_patient` rather than guessing.
- Unsupported legitimate requests escalate `out_of_scope`.
- Prompt injection / bulk destructive requests are `refused`, not escalated.
- Empty/no-op conversations are `abandoned`.

## Model, tokens and latency

**Runtime model:** no LLM. A deterministic policy engine is used. This is deliberate for repeatability and safe tool invocation.

**Tokens:** best-effort proxy reported as `len(conversation_text) // 4`; there is no model billing token count because no LLM is called.

**Latency:** measured server-side for the request, and the supplied runner also reports end-to-end latency.

## Testing

```bash
pytest -q
python3 runner.py --repeat 3
```

The adversarial directory contains eight cases covering urgent clinical interruption, ambiguous identity, guardian disambiguation, occupied slots, doctor leave, unauthorized third-party action, prompt injection/bulk mutation, and medical advice.

## Submission extras

- `DECISIONS.md` explains ambiguities and design choices.
- `AI_TRANSCRIPT.txt` records the coding-assistant instructions used during implementation.
- The three-minute video and public deployment link should be added to the final submission after deployment.
