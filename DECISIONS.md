# Decisions

## 1. Deterministic policy engine instead of an LLM
The brief allows any LLM API but does not require one. The evaluation explicitly rewards safety, determinism, correctness and restraint, so the submission uses a small deterministic conversation policy. This makes the same input produce the same terminal state and tool sequence and keeps the six tools as the source of truth.

## 2. Clinical safety gate has absolute priority
Every turn is scanned for urgent clinical symptoms before identity resolution or booking. If an urgent symptom appears, the agent immediately calls `escalate_to_human` with `clinical_urgent`. Medication/dose/diagnosis questions use `medical_advice`. This directly follows the hard rule on page 6 of the brief.

## 3. Caller instructions cannot expand capabilities
Administrator-mode, bulk cancellation, prompt-injection-style instructions are refused. There is intentionally no bulk mutation tool. This follows cv_0014's expected `refused` outcome.

## 4. Identity resolution never guesses
`lookup_patient` returns all matching candidates. Exact phone/name can resolve a caller; a surname-only ambiguous result escalates as `ambiguous_patient`. Guardian booking uses the listed `guardian_of` relationship and a child-name hint.

## 5. Date handling is request-relative
Dates are always resolved against the request's `today` field, never system time. A later date mention wins, so mid-sentence corrections such as "6 tareekh... nahi, 7 tareekh" use the corrected date.

## 6. State reset vs concurrent double booking
The harness requires each POST to start from `clinic.json`. Therefore completed requests do not persist mutations. To still satisfy the concurrency safety requirement, the tool layer uses a process-local reservation set protected by a lock around slot validation + creation. Two simultaneous requests cannot both pass the same critical section for the same slot.

## 7. Reschedule semantics
The only appointment-specific mutation tool supplied by the brief is `reschedule_appointment`; it locates the caller's existing booked appointment using patient/date/doctor filters and changes it atomically after validating the target slot.

## 8. UI scope
The React UI mirrors the required Handoff Queue and Conversation Detail screens and keeps a shared sidebar. The frontend can run against `/agent/run`; the queue is seeded with representative handoffs because the evaluation contract exposes only the agent endpoint.

## 9. Synthetic data
No additional patient records were introduced. All identifiers shown by the backend come from `clinic.json` or deterministic IDs generated from its appointment sequence.
