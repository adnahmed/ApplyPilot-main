import { exactRecord, readableValue } from "./readable.js";

/* Only panel preferences are persisted. Task payloads and credentials stay in memory. */
const saved = (k, fallback) => {
	try {
		return localStorage.getItem(k) ?? fallback;
	} catch {
		return fallback;
	}
};
const save = (k, v) => {
	try {
		localStorage.setItem(k, v);
	} catch {}
};
export function setActivityOpen(root, open, { focus = false } = {}) {
	const panel = root.querySelector(".task-drawer"),
		button = document.querySelector("#activity-toggle");
	if (!panel) return;
	panel.hidden = !open;
	root.dataset.activityOpen = String(open);
	save("ap-activity-open", String(open));
	if (button) {
		button.setAttribute("aria-expanded", String(open));
		button.classList.toggle("active", open);
	}
	if (focus)
		(open ? panel.querySelector("[data-close-activity]") : button)?.focus();
}
export function mountActivity(root, selector) {
	const content = root.querySelector(selector);
	if (!content) return;
	const card = content.closest("section.card");
	if (!card || card.closest(".task-drawer")) return;
	const panel = document.createElement("aside");
	panel.id = "workspace-activity";
	panel.className = "task-drawer";
	panel.setAttribute("aria-labelledby", "workspace-activity-title");
	panel.innerHTML =
		'<header class="task-drawer-head"><div><span class="eyebrow">WORKSPACE</span><h2 id="workspace-activity-title">Activity & results</h2></div><button class="icon-btn" type="button" data-close-activity aria-label="Close activity panel">✕</button></header>';
	const body = document.createElement("div");
	body.className = "task-drawer-body";
	body.append(card);
	panel.append(body);
	root.append(panel);
	const button = document.querySelector("#activity-toggle");
	if (button) {
		button.hidden = false;
		button.setAttribute("aria-controls", panel.id);
		button.onclick = () => setActivityOpen(root, panel.hidden, { focus: true });
	}
	panel.querySelector("[data-close-activity]").onclick = () =>
		setActivityOpen(root, false, { focus: true });
	panel.onkeydown = (e) => {
		if (e.key === "Escape") {
			e.stopPropagation();
			setActivityOpen(root, false, { focus: true });
		}
	};
	const open =
		(root.dataset.activityOpen ?? saved("ap-activity-open", "false")) ===
		"true";
	setActivityOpen(root, open);
}
export function unmountActivity() {
	const button = document.querySelector("#activity-toggle");
	if (button) {
		button.hidden = true;
		button.onclick = null;
		button.setAttribute("aria-expanded", "false");
	}
}
export function renderTaskResults(box, markup) {
	if (!box || box._taskMarkup === markup) return;
	const expanded = new Map(
		[...box.querySelectorAll("[data-task-id]")].map((item) => [
			item.dataset.taskId,
			[...item.querySelectorAll("details")].map((d) => d.open),
		]),
	);
	const scroll = box.closest(".task-drawer-body"),
		top = scroll?.scrollTop;
	box.innerHTML = markup;
	box._taskMarkup = markup;
	for (const item of box.querySelectorAll("[data-task-id]")) {
		const states = expanded.get(item.dataset.taskId);
		if (states)
			[...item.querySelectorAll("details")].forEach((d, i) => {
				d.open = states[i] === true;
			});
	}
	if (scroll) scroll.scrollTop = top;
}

const escapeText = (v) =>
	String(v ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
export function taskRuntimeLine(j) {
	const at = j.started_at
		? new Date(j.started_at).toLocaleString()
		: "Time not recorded";
	const build = j.runtime_build || "build not recorded";
	return `<p class="fine-print"><time>${escapeText(at)}</time>${j.current_build === false ? " · Saved history from an earlier/unidentified build, not a fresh check." : ""}</p><details class="technical-details"><summary>Task identity & runtime</summary><p class="fine-print">${escapeText(j.id)} · ${escapeText(build)}</p></details>`;
}
export function taskResult(j) {
	if (j.result && Object.keys(j.result).length) return j.result;
	if (["failed", "timed_out", "interrupted"].includes(j.state))
		return {
			error: j.message || "No saved error message",
			code: "historical_details_unavailable",
			note: "This task did not save structured diagnostics. A newer build cannot reconstruct missing old details.",
		};
	return null;
}
export function showActivityRuntime(root, runtime) {
	const head = root.querySelector(".task-drawer-head");
	if (!head) return;
	let note = head.querySelector("[data-runtime-label]");
	if (!note) {
		const details = document.createElement("details");
		details.className = "technical-details";
		details.innerHTML = "<summary>Runtime details</summary>";
		note = document.createElement("p");
		note.className = "fine-print";
		note.dataset.runtimeLabel = "";
		details.append(note);
		head.firstElementChild.append(details);
	}
	note.textContent = runtime?.build
		? `Running ${runtime.build} · Python ${runtime.python} · CloakBrowser ${runtime.cloakbrowser || "NOT INSTALLED"}`
		: "Runtime build unavailable. Restart the updated desktop application before retrying setup.";
}

export function taskDetails(j) {
	const result = taskResult(j);
	if (!result) return "";
	// Never replace a provider failure with a guessed success. Render only supplied facts.
	const alerts = ["error", "warning", "warnings", "problems"].filter(
		(k) => result[k] && (!Array.isArray(result[k]) || result[k].length),
	);
	const notice = alerts.length
		? `<div class="notice notice-warning">${alerts.map((k) => readableValue(result[k])).join("")}</div>`
		: "";
	return (
		notice +
		`<details class="technical-details"><summary>What happened · result details</summary>${readableValue(result)}${exactRecord(result, "Exact result · JSON")}</details>`
	);
}
