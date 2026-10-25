# Fill the current application page

This guide runs INLINE in the existing application agent. It is not another agent,
approval pass, or intermediate JSON report. The assigned task supplies the page,
applicant facts, documents, answer policy, and final submission procedure.

1. Read labels, required indicators and current values. Use the full accessible/text
   view if an interactive snapshot omitted labels. Resolve unknown answers through
   the existing answer tools and evidence; do not guess from unnamed field positions.
2. Batch compatible text fields using the installed tool's advertised `fields` form.
   Keep supplied values as strings. Corrections replace: use `clear=true` where
   supported, or the documented select-all/clear operation. Explicit append is only
   for an intentional continuation. Preserve already-correct fields.
3. Use the returned changes as the batch observation. Reinspect only missing,
   rejected, or unexpected values. Do not issue a fresh snapshot after every field.
4. Native selects use native selection. Custom dropdowns use open → current option
   ref → click. Search/autocomplete widgets need an observed suggestion selection.
   Do not use JavaScript to hunt for an option already exposed by the browser.
5. Upload the supplied documents with `upload_application_document`. For hidden
   inputs, use a target returned by `inspect_application_uploads`, not the container
   or upload button's ref. Preserve successful attachments.
6. Move to the next page after the required fields are complete, then handle its
   current fields/errors. Do not perform extra review loops or regenerate documents.
7. For final submission, use the main task's submission operation and record the
   observed result. Do not repeat an uncertain submission or invent a confirmation.

When an action fails, use its actual observation to choose a different useful step.
A wrong target, missing label and unsupported option need different corrections;
repeating the same call with the same arguments does not supply new evidence.
