/* Presentation only: retain exact records, provenance and uncertainty. No inferred claims. */
export const esc = (value) =>
	String(value ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const labels = {
	full_name: "Name",
	province_state: "State / province",
	professional_summary: "Professional summary",
	skills_boundary: "Skills",
	work_authorization: "Work eligibility",
	by_country: "By country",
	legally_authorized_to_work: "Allowed to work",
	require_sponsorship: "Sponsorship needed",
	numeric_value: "Recorded value",
	source_ref: "Source reference",
	source_refs: "Source references",
	source_text: "Source excerpt",
	source_id: "Source",
	line_start: "First line",
	line_end: "Last line",
	confidence: "Evidence confidence",
	attribution: "Your contribution",
	measurement_status: "Measurement status",
	claimability: "How this may be used",
	current_job_title: "Current role",
	x_url: "X / Twitter profile",
	linkedin_url: "LinkedIn profile",
	public_evidence: "Approved project evidence",
};
export const label = (key) =>
	String(key || "")
		.split(".")
		.map(
			(k) =>
				labels[k] ||
				k.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase()),
		)
		.join(" › ");
const present = (v) =>
	v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && !v.length);
export function readableValue(value, depth = 0) {
	if (value === null || value === undefined || value === "")
		return '<span class="muted">Not recorded</span>';
	if (typeof value === "boolean") return value ? "Yes" : "No";
	if (typeof value !== "object") return esc(value);
	if (depth > 3)
		return '<span class="muted">Nested detail available in the exact record below</span>';
	if (Array.isArray(value)) {
		if (!value.length) return '<span class="muted">None recorded</span>';
		const rows = value
			.slice(0, 12)
			.map((v) => `<li>${readableValue(v, depth + 1)}</li>`)
			.join("");
		return `<ul class="readable-list">${rows}</ul>${value.length > 12 ? `<p class="fine-print">${value.length - 12} more entries in the exact record below.</p>` : ""}`;
	}
	const entries = Object.entries(value).filter(([, v]) => present(v));
	return entries.length
		? `<dl class="readable-facts">${entries.map(([k, v]) => `<div><dt>${esc(label(k))}</dt><dd>${readableValue(v, depth + 1)}</dd></div>`).join("")}</dl>`
		: '<span class="muted">No details recorded</span>';
}
export function exactRecord(value, title = "Exact record · JSON") {
	return `<details class="technical-details"><summary>${esc(title)}</summary><pre class="setup-code">${esc(JSON.stringify(value, null, 2))}</pre></details>`;
}
function fact(title, value) {
	return present(value)
		? `<div><dt>${esc(title)}</dt><dd>${readableValue(value)}</dd></div>`
		: "";
}
export function impactRecord(r) {
	const measurements = Array.isArray(r.measurements) ? r.measurements : [];
	const outcomes = Array.isArray(r.outcomes) ? r.outcomes : [];
	return `<div class="evidence-story">${r.resume_eligible === false ? '<p class="notice notice-warning">This record is not eligible for résumé tailoring.</p>' : ""}<dl class="readable-facts">
 ${fact("Situation", r.problem)}${fact("What you did", r.intervention?.summary)}
 ${fact("Project", r.scope?.project)}${fact("Your contribution", r.attribution ? label(r.attribution) : null)}
 ${fact("Evidence confidence", r.confidence ? label(r.confidence) : null)}${fact("Review status", r.review_status ? label(r.review_status) : null)}</dl>
 ${measurements.length ? `<h4>Recorded measurements</h4>${measurements.map((m) => `<section class="evidence-measurement"><strong>${esc(m.name || "Measurement")}</strong><p>${esc(m.claim || m.quantity_text || "No claim recorded")}</p><dl class="readable-facts">${fact("Before", m.baseline)}${fact("After", m.result)}${fact("Measurement status", m.measurement_status ? label(m.measurement_status) : "Not recorded")}</dl>${m.source_text ? `<blockquote>${esc(m.source_text)}</blockquote>` : ""}<details class="technical-details"><summary>Source & measurement method</summary>${readableValue({ source_ref: m.source_ref, method: m.method })}</details></section>`).join("")}` : '<p class="fine-print">No measurements recorded. This does not mean there was no impact.</p>'}
 ${outcomes.length ? `<h4>Recorded outcomes</h4>${outcomes.map((o) => `<section class="evidence-measurement"><p>${esc(o.claim || "No outcome description recorded")}</p><p class="fine-print">${esc(["hypothesis", "unknown"].includes(o.claimability) ? label(o.claimability) + " · not a supported résumé claim" : o.claimability ? label(o.claimability) : "Use status not recorded")}${o.grounded === true ? " · Marked as grounded in the saved record" : o.grounded === false ? " · Not grounded in the saved record" : ""}</p>${o.source_text ? `<blockquote>${esc(o.source_text)}</blockquote>` : ""}</section>`).join("")}` : ""}
 ${r.source_refs?.length ? `<details class="technical-details"><summary>Supporting source references</summary>${readableValue(r.source_refs)}</details>` : ""}
 ${exactRecord(r)}</div>`;
}
export function changeReview(changes = []) {
	const changed = changes.filter((c) => c.changed);
	if (!changed.length)
		return '<div class="notice">No configuration text changes to review. Applying can still initialize missing workspace files.</div>';
	const names = {
		"profile.json": "Profile facts",
		"searches.yaml": "Search preferences",
		"resume.txt": "Master résumé",
		"resume.pdf": "Master résumé PDF",
	};
	const effects = {
		"resume.pdf":
			"The selected original PDF replaces the saved master PDF. Its extracted text is reviewed separately.",
		"profile.json":
			"Future profile reads use these facts. Changing a profile URL does not switch the authorized sending account.",
		"searches.yaml":
			"Future searches use this configuration; saved job records remain in your workspace.",
		"resume.txt":
			"Future document preparation can use this résumé. Saving does not regenerate previously prepared documents.",
	};
	return changed
		.map(
			(
				c,
			) => `<section class="change-review"><h3>${esc(names[c.file] || c.file)}</h3><p>${esc(c.action === "remove" ? "The saved master PDF will be removed because you changed the résumé text without selecting a new PDF. Previously prepared application documents are not rebuilt." : effects[c.file] || "This saved configuration will change.")}</p>
 ${c.review?.fields?.length ? `<div class="table-scroll"><table class="change-table"><caption class="sr-only">${esc(names[c.file] || c.file)}: current and proposed values</caption><thead><tr><th scope="col">Change</th><th scope="col">Current</th><th scope="col">After applying</th></tr></thead><tbody>${c.review.fields.map((f) => `<tr><th scope="row">${esc(label(f.path))}</th><td>${f.before_exists ? readableValue(f.before) : '<span class="muted">Not set</span>'}</td><td>${f.after_exists ? readableValue(f.after) : '<span class="muted">Removed</span>'}</td></tr>`).join("")}</tbody></table></div>` : c.file === "resume.txt" && c.review ? `<p>${c.review.before_lines} lines → ${c.review.after_lines} lines. Open the text changes below to review the wording.</p>` : "<p>Open the exact changes below to review this file.</p>"}
 ${c.diff_truncated ? '<p class="notice notice-warning">The diff is too large to display in full. Inspect the current and proposed files before applying.</p>' : ""}
 ${c.review?.truncated ? '<p class="notice notice-warning">This summary is shortened. Review the remaining changes in the exact diff.</p>' : ""}
 <details class="technical-details"><summary>${esc(c.file === "resume.txt" ? "Review résumé text changes" : c.file === "resume.pdf" ? "PDF change details" : "Exact file diff · " + c.file)}</summary><pre class="setup-code">${esc(c.diff || "No diff available")}</pre></details></section>`,
		)
		.join("");
}
