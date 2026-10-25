# Application context and files

The worker receives these inputs from ApplyPilot:

| Input | Use |
| --- | --- |
| `job` | Assigned URL, role, company, description, location, route evidence |
| `profile` | Candidate facts relevant to this application |
| `planned_answers` | Answers prepared for this job |
| `known_answers` | Reusable answers subject to the answer policy |
| `selected_work_arrangement`, `search_intent` | Arrangement and preferences |
| `documents.resume`, `documents.cover_letter` | Prepared artifacts |
| `networking` | Authorized referral context when present |

Generation stages receive resume text, evidence, and job context in their
messages. Use those inputs without searching for the most recent job folder.

ApplyPilot owns its configured data directory, database, generated documents,
and history. Use supplied paths through upload tools. Save generated form
answers with `applypilot_control.save_answer`; do not modify the profile or
maintain another application log.

Worker skills live in `.claude/skills/`. Resolve Markdown links relative to
their containing file. Global installations keep skill folders and shared
references together.

## Workflow file bindings

The imported workflows use logical material names. In an ApplyPilot worker,
resolve those names from the supplied task rather than searching for new folders:

| Workflow material | ApplyPilot binding |
| --- | --- |
| `DATA_DIR/profile.md` / work history profile | Supplied profile and attributed evidence |
| `DATA_DIR/resume/*` / original resume | Supplied original resume text and resume artifact |
| Per-job `posting.md` | Assigned `job` and its description |
| Per-job `resume.md` | Supplied tailored resume content or `documents.resume` |
| Per-job `cover-letter.md` | Supplied letter content or `documents.cover_letter` |
| `DATA_DIR/application-data.md` | `profile`, `planned_answers`, `known_answers` |
| `DATA_DIR/preferences.md` | Supplied search intent and selected arrangement |
| Application/history writes | ApplyPilot guard and application tracking tools |

Setup and gathering are already fulfilled when these materials are supplied.
The generation stage returns its JSON result to the renderer instead of writing
these logical files. The application worker uses `save_answer` for generated
answers and the submission guard for application records. Use the profile editor
for actual user corrections; a worker does not mutate the canonical profile.
