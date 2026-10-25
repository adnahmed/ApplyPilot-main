---
name: application-browser
description: Complete an application using the connected browser's native controls, supplied applicant facts, and returned observations.
---

# Application browser

ApplyPilot supplies the assigned posting, application session, applicant facts,
prepared documents, and submission authorization. Use them directly. The connected
MCP server's schemas and initialization instructions define its API; do not invent
helper methods, match-level metadata, fingerprints, or a second agent workflow.

## Read the form, then fill it

For BrowserOS, `open_application_page()` opens or recovers the assigned page. Keep
its numeric page ID. Work only in the application's owned tabs. For Playwright use
its connected page and advertised native tools instead of BrowserOS call names.

Read the form's labels, required indicators, current values, and relevant options.
An interactive-only snapshot can omit plain-text labels. When controls are unnamed,
use a full snapshot or a focused page read, rather than assigning values by index.
Use a screenshot when the accessible/text view does not explain the visible control.

Use exact personal details and previous answers supplied in the task. Ground actions
in the current observation's field references and option values. If the observation
reports uncertainty, use a focused read-only browser operation to inspect that state.
Do not overwrite a correct field just to demonstrate activity.

## Text fields

Use native form filling. Batch independent, mapped text fields when the advertised
schema supports it; keep dropdowns, navigation and other structure-changing actions
out of that batch. Preserve the supplied strings, including punctuation and Unicode.

A correction must REPLACE the existing value, not append another copy. When native
`act` advertises `clear`, use `clear=true` for fill/type; `clear=false` means deliberate
append. ApplyPilot forwards these arguments unchanged. When that option is absent,
use the native documented clear/select-all editing action
before replacement. Never guess that another call with the same text will clear it.

Use the action's returned observation to inspect the batch once. Do not alternate
fill/snapshot/fill/snapshot when the tool already returns the changed values. Re-read
only a value that is absent, rejected, or inconsistent. If it is concatenated or
partially accepted, correct that field before continuing; don't refill the whole form.

## Dropdowns and repeated sections

For a native select, use the advertised select operation with an observed option.
For a custom combobox, open it, then click the desired option using its returned
current ref. Options may be rendered in a portal outside the combobox DOM subtree;
an empty `querySelector` result is not proof that an option visible in the snapshot
is missing. Use a new snapshot only when the returned observation is insufficient.

Do not fill address autocomplete controls as ordinary independent text fields.
Select the intended observed suggestion and reuse the updated country/region/city
controls. For repeated experience/education sections, fill only the rows needed for
the supplied evidence and this form's requirements. Avoid re-adding existing entries.

## Uploads and navigation

Use `upload_application_document` with the supplied resume or cover letter. A visible
upload button is not necessarily a file input. `inspect_application_uploads` returns
target handles for hidden inputs. Once the supplied document is attached, continue;
do not remove/re-upload it unless the page actually rejects or loses it.

After a page transition or stale-ref error, use current refs. Preserve filled sections
when fixing validation. Next/Continue on a multi-page form is not the final Submit.
Use the native action result to read new errors/fields before another transition.

## Diagnose rather than repeat

A successful tool envelope means the operation returned, not that the task advanced.
If it did not achieve its purpose, inspect that observation and change the approach:
address the visible dialog/error/field, use an existing option ref, or read the missing
label. Do not repeat the same rejected input, search an empty subtree indefinitely,
create new copies of the application, or guess company career URLs.

`evaluate` runs in the page context: DOM objects live there, not the `browser` SDK.
`run` uses the BrowserOS SDK: follow its advertised signatures. Use these for a
specific gap in the native operations, not an invented browser runtime. Read-only
inspection must not delete dialogs or mutate a form to make an error disappear.

## Finish

Once required fields and requested attachments are complete, use the supplied final
submission operation. Read its response; correct reported validation or record the
observed confirmation immediately. Do not turn a confirmed submission into another
review loop. An uncertain submission must not be repeated. Report a genuine unresolved
blocker through the existing application controls, preserving the form and evidence.
