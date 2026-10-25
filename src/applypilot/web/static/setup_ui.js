import { impactRecord } from "./readable.js";
/* Shared material/diary controls. Source text is always escaped. */
export const esc = (v) =>
	String(v ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
let nextHelp = 0;
export function help(text) {
	if (!text) return "";
	const id = "ap-help-" + ++nextHelp;
	return `<span class="setup-help"><button type="button" class="help-trigger" aria-label="More information" aria-describedby="${id}">?</button><span id="${id}" role="tooltip" hidden>${esc(text)}</span></span>`;
}
export function wireHelp(root) {
	for (const wrap of root.querySelectorAll(".setup-help")) {
		const button = wrap.querySelector("button"),
			tip = wrap.querySelector("[role=tooltip]");
		const show = () => {
			tip.hidden = false;
		};
		const hide = () => {
			tip.hidden = true;
		};
		wrap.onmouseenter = show;
		wrap.onmouseleave = hide;
		button.onfocus = show;
		button.onblur = hide;
		button.onclick = show;
		button.onkeydown = (e) => {
			if (e.key === "Escape") {
				hide();
				e.stopPropagation();
			}
		};
	}
}
export function tokenLink(mode = "read") {
	const p = new URLSearchParams({
		name: mode === "write" ? "ApplyPilot backup" : "ApplyPilot evidence",
		contents: mode,
		expires_in: "90",
	});
	return `https://github.com/settings/personal-access-tokens/new?${p}`;
}
function diaryButtons(kind, id, readOnly) {
	return `<div class="setup-inline"><button type="button" class="btn btn-sm" data-diary-edit data-diary-kind="${esc(kind)}" data-diary-id="${esc(id)}" ${readOnly ? "disabled" : ""}>Edit</button><button type="button" class="btn btn-sm" data-diary-archive data-diary-kind="${esc(kind)}" data-diary-id="${esc(id)}" ${readOnly ? "disabled" : ""}>Archive</button></div>`;
}
export function impactEntry(row, { extract = false, readOnly = false } = {}) {
	const when = row.created_at
		? new Date(row.created_at).toLocaleString()
		: row.recency || "Date not recorded";
	return `<article class="impact-entry"><div class="impact-entry-head"><h3>${esc(row.title)}</h3><span class="badge">${esc(row.state)}</span></div><p class="fine-print">${esc(when)} · ${row.kind === "note" ? "Your note" : "Saved impact accomplishment"}</p><p class="impact-text">${esc(row.text)}</p>${!row.note_id && row.records?.length === 1 ? diaryButtons("record", row.records[0].id, readOnly) : ""}${row.note_id ? diaryButtons("note", row.note_id, readOnly) : ""}${row.records?.length ? `<details><summary>Supporting details · ${row.records.length} record${row.records.length === 1 ? "" : "s"}</summary>${row.records.map((r) => `<div class="impact-entry"><strong>${esc(r.problem || r.id)}</strong>${diaryButtons("record", r.id, readOnly)}${impactRecord(r)}</div>`).join("")}</details>` : ""}${extract && row.note_id && row.state !== "completed" ? `<button type="button" class="btn btn-sm" data-extract="${esc(row.note_id)}" ${readOnly ? "disabled" : ""}>Structure this note</button>` : ""}</article>`;
}
