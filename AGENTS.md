# AGENT.md

## Execution Pace

For concrete, bounded implementation requests, inspect only enough code to make
the change safely, then implement it. Prefer finishing the requested work over
extended planning, repeated verification, or speculative research.

## Learning and Model Recovery

Keep DSPy/GEPA learning and RLM access to large contexts active by default.
LiteLLM owns provider selection, retries, fallbacks, and response-format recovery.
The application declares output schemas, validates task invariants, and records
exhausted rounds for the existing automatic retry schedule. Preserve evaluation
and promotion checks. Do not add persistent learning lockouts or disable RLM
in response to ordinary provider or model failures.

## Tests — Do Not Run

Never run tests unless explicitly prompted to by the user. Do not even look at them. Totally ignore tests.

When the user asks for work that involves or borders on tests, skip them, do not read them, do not run them, and do not report on them. Only run tests when the user explicitly asks you to run tests.

No need to update stale md files, no need to even work on the comments.
