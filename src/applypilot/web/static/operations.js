import { STAGE_CATALOG } from "./stage_catalog.js";

const OPERATION_STAGES = [
	...new Set([
		...STAGE_CATALOG.map((row) => row.stage),
		"pipeline",
		"integrations",
		"operations",
		"unknown",
	]),
];

import { trackForm, updatesPaused } from "./ux.js";

/* Incident-first local journal. Presentation never changes execution or audit history. */
let teardown = () => {};
export function unmountOperations() {
	teardown();
	teardown = () => {};
}
const esc = (v) =>
	String(v ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const when = (value) =>
	value ? new Date(Number(value) * 1000).toLocaleString() : "Not recorded";
const fmt = (value) => Number(value || 0).toLocaleString();
const human = (value) => String(value || "Operation").replace(/_/g, " ");
const pre = (value) =>
	`<pre class="setup-code" style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(typeof value === "string" ? value : JSON.stringify(value, null, 2))}</pre>`;
const card = (title, note, body) =>
	`<section class="card"><div class="card-head"><div><h2>${esc(title)}</h2><p>${esc(note)}</p></div></div><div class="card-content">${body}</div></section>`;
const tag = (value) =>
	`<span class="badge ${["error", "critical", "failed", "stale"].includes(value) ? "red" : ["warning", "partial"].includes(value) ? "orange" : "blue"}">${esc(human(value))}</span>`;
const reviewed = (item) =>
	Boolean(item.acknowledged_at && item.last_at <= item.acknowledged_at);
const storedChoice = (key, allowed, fallback) => {
	try {
		const value = Number(localStorage.getItem(key));
		return allowed.includes(value) ? value : fallback;
	} catch {
		return fallback;
	}
};
const rememberChoice = (key, value) => {
	try {
		localStorage.setItem(key, String(value));
	} catch {
		/* Persistence may be unavailable in private mode. */
	}
};
const downloadJson = (name, value) => {
	const url = URL.createObjectURL(
			new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
		),
		a = document.createElement("a");
	a.href = url;
	a.download = name;
	a.click();
	setTimeout(() => URL.revokeObjectURL(url), 1000);
};
async function copyText(value) {
	if (navigator.clipboard?.writeText) {
		await navigator.clipboard.writeText(value);
		return;
	}
	const area = document.createElement("textarea");
	area.value = value;
	area.setAttribute("readonly", "");
	area.style.position = "fixed";
	area.style.opacity = "0";
	document.body.append(area);
	area.select();
	const copied = document.execCommand?.("copy");
	area.remove();
	if (!copied) throw new Error("Clipboard is unavailable in this browser.");
}

export function explain(item) {
	const message = String(item.message || "");
	const chain = item.details?.exception_chain || [];
	const text = [item.message, ...chain.map((e) => e.message)].join(" ");
	const types = new Set(chain.map((e) => e.type));
	const hasMsg = (...fragments) =>
		fragments.some((fragment) => message.toLowerCase().includes(fragment));
	const hasText = (...fragments) =>
		fragments.some((fragment) => text.toLowerCase().includes(fragment));
	if (hasMsg("ERR_NAME_NOT_RESOLVED", "NameResolutionError", "getaddrinfo failed"))
		return {
			title: "Hostname could not be resolved",
			meaning:
				"The recorded request failed during hostname resolution, before the destination could be reached.",
			action:
				"Check the recorded host, Windows DNS, network connectivity and any VPN/proxy. Repeated retry or changing dashboard transport will not repair DNS.",
		};
	if (
		hasMsg(
			"email writes paused:",
			"incomplete inbox sync; email writes paused",
		)
	)
		return {
			title: "Email sending is waiting for inbox processing",
			meaning:
				"Mailbox listing can be complete while saved messages remain pending or scheduled for retry. This warning does not, by itself, mean Gmail authentication failed.",
			action:
				"Inspect sync_complete, errors, backlog, backlog_due, backlog_deferred and backlog_next_retry_at. Resolve unread/retry messages before sending; do not bypass this guard or delete the inbox history.",
			href: "#activity",
			link: "Open recorded mailbox activity",
		};
	if (hasMsg("coordinator heartbeat stopped"))
		return {
			title: "Engine supervisor missed a launch heartbeat",
			meaning:
				"The supervisor restarted its owned child. This record alone does not prove its stage workers were dead; the old exact-PID check can miss a launched interpreter behind a Windows redirector.",
			action:
				"The repaired supervisor matches a per-launch identity and heartbeat. Compare the latest run after restarting the application; historical incidents are retained.",
		};
	if (hasMsg("ERR_CONNECTION_TIMED_OUT", "ERR_TIMED_OUT", "ConnectionTimeoutError"))
		return {
			title: "Remote connection timed out",
			meaning:
				"The request exceeded its connection deadline. Digits in a job URL are not an HTTP authorization or rate-limit status.",
			action:
				"Check source availability and the network/proxy. This timeout alone is not a reason to replace credentials or reset the job.",
		};
	if (message === "Dashboard database read failed")
		return {
			title: "Dashboard read could not complete",
			meaning: `Route: ${item.details?.route || "not recorded"} · PostgreSQL SQLSTATE: ${item.details?.sqlstate || "not recorded"}. This is not an empty queue or proof of corrupt data.`,
			action:
				"Use Workers for the independent live-report path. SQLSTATE 57014 indicates a cancelled/time-budgeted read; inspect the recorded code before retrying.",
			href: "#workers",
			link: "Open Workers",
		};
	if (hasMsg("extraction_empty", "extraction_provider", "model_invalid_json", "model_schema_error", "model_output_incomplete"))
		return {
			title: "Job extraction did not return usable data",
			meaning: message,
			action:
				"Check the source and the preceding model/provider error. The updated classifier schedules bounded retries; it does not treat an empty model answer as proof of a closed posting.",
		};
	if (hasMsg("gmail read failed: authentication"))
		return {
			title: "Gmail needs reauthorization",
			meaning:
				"The mailbox reader rejected authentication. This is not an empty inbox.",
			action:
				"Reconnect the intended Gmail account and run its read-only verification before retrying tracking.",
			href: "#connections?group=gmail",
			link: "Open Gmail connection",
		};
	if (hasMsg("network-worker: networking worker exited"))
		return {
			title: "Networking worker stopped unexpectedly",
			meaning:
				"The supervisor recorded a nonzero child exit. The exit record alone does not identify the original exception.",
			action:
				"Open this run and inspect the preceding unattended/BrowserOS/Gmail failure. Verify the selected account in Outreach before retrying.",
		};
	if (hasMsg("networking dispatcher ownership lost"))
		return {
			title: "Networking dispatcher lost its ownership lease",
			meaning:
				"The network dispatcher could not renew its ownership lease within the renewal window. Another controller or competing process may have claimed or evicted the lease, or renewal stalled.",
			action:
				"Check for another live Autopilot controller or a stale network-dispatcher lease before retrying. Plain retry can hit the same ownership guard until the previous owner's lease expires or is released.",
		};
	if (hasMsg("network-supervisor: runtimeerror"))
		return {
			title: "Networking supervisor stopped on a fatal error",
			meaning:
				"The supervisor recorded a fatal failure (for example, a lost network-dispatcher ownership lease). The exit record alone does not identify the original exception.",
			action:
				"Open this run and inspect the preceding unattended/dispatcher failure. Verify controller and lease ownership state before retrying; retry alone may re-trigger the same guard.",
		};
	if (hasMsg('page.goto: timeout', 'waiting until "load"'))
		return {
			title: "Job page navigation timed out",
			meaning: message,
			action:
				"Inspect the page and its useful content. The updated reader uses DOM readiness plus a bounded content wait, rather than waiting for all network activity.",
		};
	if (message === "HTTP 404")
		return {
			title: "Job URL returned HTTP 404",
			meaning:
				"The requested resource was not found. This record alone does not prove the job permanently closed.",
			action:
				"Check the canonical posting URL and source before retrying; avoid repeated immediate retries of the same missing URL.",
		};
	if (message === "no data extracted")
		return {
			title: "No usable job details extracted",
			meaning:
				"The reader produced no usable detail record. The cause may be page shape, an access wall or an empty model result.",
			action:
				"Inspect the source page and preceding extraction errors. Keep this separate from a confirmed closed job.",
		};
	if (hasMsg("ERR_CONNECTION_CLOSED"))
		return {
			title: "Remote connection closed",
			meaning: message,
			action:
				"Check the source and network. Retry reads with backoff; do not blindly retry a write with an unknown result.",
		};
	if (
		[...types].some((type) => type.endsWith("OperationalError")) &&
		hasText("server closed the connection unexpectedly", "terminated abnormally")
	)
		return {
			title: "PostgreSQL connection was dropped mid-request",
			meaning:
				"The database server closed an in-flight operation, so the recorded action could not complete. This is a connection failure, not a schema, data or logic error.",
			action:
				"Check the embedded PostgreSQL service for memory pressure or restarts, then allow the run to continue: selection now backs off and retries on a fresh connection instead of stopping the worker.",
		};

	if (item.component === "pdf" && hasText("latex rendering failed"))
		return {
			title: "LaTeX rendering failed; fallback was attempted",
			meaning: chain.some((e) => e.type === "KeyError")
				? `Template field error: ${chain.find((e) => e.type === "KeyError").message}. The converter attempted HTML/Chromium instead.`
				: "The LaTeX renderer failed and the converter attempted HTML/Chromium instead.",
			action:
				"Inspect the document completion in this run. A fallback warning does not by itself mean the PDF failed. Fix the reported template field rather than the model provider.",
		};
	if (hasText("llm bridge timed out"))
		return {
			title: "Local model-call bridge timed out",
			meaning:
				"The local waiting deadline expired; this record does not prove which part of the proxy/provider was stalled.",
			action:
				"Find the matching call_id in logs/llm-responses. A request/cancelled record with no response means ApplyPilot did not receive a response body before cancellation.",
		};
	if (types.has("GmailTransportError"))
		return {
			title: "Gmail subprocess/transport failed",
			meaning: message,
			action:
				"Inspect the nested exception types and any errno/winerror. The high-level permission/file-lock message does not establish which file or subprocess operation failed. Do not delete credentials or bypass the mail-write guard.",
		};
	if (hasText("incomplete_output", "no usable visible output", "no usable output"))
		return {
			title: `${human(item.stage)}: model returned no usable answer`,
			meaning:
				"The request did not produce usable visible model output. A successful HTTP status is not a successful stage result.",
			action:
				"Inspect the matching call_id in workspace logs/llm-responses: response content, finish_reason, usage, and any proxy request ID. The response alone does not establish which gateway plugin caused it.",
			href: "#connections?group=llm",
			link: "Model gateway",
		};
	if (hasText("no parseable score line"))
		return {
			title: "Scoring response did not match the required format",
			meaning:
				"The model returned text without a usable score. ApplyPilot did not invent one or advance this job as scored.",
			action:
				"Inspect the model response and gateway formatting. A reasoning-only or truncated response is not a successful scoring result.",
			href: "#connections?group=llm",
			link: "Model gateway",
		};
	if (hasText("autopilot-supervisor.guard"))
		return {
			title: "Another Autopilot controller already owns the workspace",
			meaning:
				"A second start was refused. This historical record is not proof that the current controller is stopped.",
			action:
				"Use the live Autopilot control above. Pause or stop the owner normally; do not remove an ownership guard to force another copy.",
		};
	if (hasText("no compatible reader is connected"))
		return {
			title: "A discovery source needs a connected reader",
			meaning:
				"This source cannot run without an authorized, compatible reader. Other sources can still succeed.",
			action:
				"Connect or verify the relevant social reader, or disable that source rather than repeatedly retrying it.",
			href: "#connections?group=browser",
			link: "Reader connections",
		};
	if (
		text.includes(
			"allocation_limit must be an integer at least as large as limit",
		)
	)
		return {
			title: "Application worker could not select its next job",
			meaning:
				"An internal allocation-limit mismatch stopped queue selection. This is a worker failure, not an empty queue.",
			action:
				"Use the corrected application runtime before restarting. Preserve existing application outcomes; do not reset uncertain submissions.",
		};
	if (
		text.includes("another discovery cycle owns the active context") ||
		item.details?.reason === "another_discovery_cycle_is_active"
	)
		return {
			title: "Preparation found another active discovery cycle",
			meaning:
				"The existing ownership lease blocked a second preparation start. That guard prevents competing context changes.",
			action:
				"Check the existing run. Let it finish or stop it normally; do not delete its lease while it may still be active.",
		};
	if (
		["pipeline", "engine"].includes(item.stage) &&
		["child_exit", "continuous_exit"].includes(item.operation)
	)
		return {
			title: "Preparation process stopped with an error",
			meaning:
				"The parent recorded a failed subprocess. This may be the consequence of another engine exception, not a separate root cause.",
			action:
				"Inspect the same run for the preceding preparation error before retrying.",
		};
	if (
		item.stage === "backup" &&
		hasText("incomplete asset", "upload remains incomplete", "draft asset is still changing", "previously verified backup asset changed")
	)
		return {
			title: "Backup upload needs verification",
			meaning:
				"This attempt did not confirm a complete replacement recovery point. The retained encrypted snapshot must remain available for reconciliation.",
			action:
				"Check the latest verified recovery point in Backups & recovery. Do not delete pending backup state just to dismiss this record.",
		};
	if (
		item.outcome === "cancelled" ||
		types.has("KeyboardInterrupt")
	)
		return {
			title: `${human(item.stage)} was interrupted`,
			meaning:
				"This operation ended through cancellation or interruption, not recorded completion. That alone does not establish corruption.",
			action: "Check the latest task or recovery state before restarting work.",
		};
	if (item.stage === "backup" && item.outcome === "success")
		return {
			title: "Backup sync reported success",
			meaning:
				"This is a successful sync record. Check Backups & recovery for the actual snapshot and verification details.",
			action: "",
		};
	if (item.stage === "backup" && item.message === "backoff")
		return {
			title: "Backup is waiting before another attempt",
			meaning:
				"This is a scheduled retry-delay check, not another upload failure.",
			action: "",
		};
	if (item.stage === "backup" && item.message === "not_due")
		return {
			title: "No backup was due at this check",
			meaning:
				"The scheduler checked the interval and did not start an upload. This is not a backup verification.",
			action: "",
		};
	return {
		title: `${human(item.stage)} · ${human(item.operation)}`,
		meaning: item.message || "No explanation was recorded.",
		action: ["warning", "error", "critical"].includes(item.severity)
			? "Inspect the recorded details before deciding whether another attempt is safe."
			: "",
	};
}

function technical(item) {
	const chain = item.details?.exception_chain || [];
	const traces = chain
		.map(
			(e) =>
				`<p><strong>${esc(e.type)}</strong>: ${esc(e.message || "No exception message")}</p>${(e.frames || []).map((f) => `<p><code>${esc(f.file)}:${esc(f.line)}</code> · ${esc(f.function)}</p>`).join("")}`,
		)
		.join("");
	return `${traces}<details class="setup-details"><summary>Exact recorded data</summary>${pre(item)}</details>`;
}

export function mountOperations(root, { api, notify, readOnly }) {
	unmountOperations();
	const $ = (selector) => root.querySelector(selector);
	const $$ = (selector) => [...root.querySelectorAll(selector)];
	const initial = new URLSearchParams(location.hash.split("?")[1] || "");
	let disposed = false,
		timer,
		loading = false,
		mutation = false,
		revision = 0;
	let data = null,
		displayedEvents = null,
		before = "",
		nextBefore = null,
		history = [];
	let page = 0,
		selected = new Set(),
		inspector = null,
		returnFocus = null,
		settingsTracker = null;
	const problemPageSizes = [10, 25, 50, 100],
		eventPageSizes = [30, 50, 100, 250];
	let problemPageSize = storedChoice(
		"ap-operations-problem-page-size",
		problemPageSizes,
		25,
	);
	let eventPageSize = storedChoice(
		"ap-operations-event-page-size",
		eventPageSizes,
		30,
	);
	const defaults = () => ({
		run_id: "",
		stage: "",
		severity: "",
		component: "",
		q: "",
		view: "significant",
	});
	let filters = { ...defaults(), run_id: initial.get("run_id") || "" };
	root.innerHTML = `<div class="ops-workbench">
    <div id="ops-error" class="notice notice-warning" role="alert" hidden></div>
    <div class="ops-heading"><div><h2>Understand the problem. Keep the evidence.</h2><p>Review is not recovery. Display changes never alter the original events or retry behavior.</p></div><div class="ops-heading-side"><div id="ops-stats" class="ops-counts"></div><div class="ops-share-actions"><button class="btn" id="ops-copy-report" type="button" disabled>Copy problems report</button><button class="btn" id="ops-export-report" type="button" disabled>Export operations report</button><button class="btn btn-primary" id="ops-export-bundle" type="button">Export support ZIP</button></div></div></div>
    <div class="view-switch" role="group" aria-label="Operations views">
      <button class="btn" data-view="problems" aria-controls="ops-problems" aria-pressed="true">Problems</button>
      <button class="btn" data-view="history" aria-controls="ops-journal" aria-pressed="false">Event history</button>
      <button class="btn" data-view="monitoring" aria-controls="ops-tools" aria-pressed="false">Monitoring</button>
    </div>
    <div id="ops-recording-warning" class="notice notice-warning" role="status" hidden></div>
    <section id="ops-problems" aria-label="Problem groups">
      <form id="ops-problem-filter" class="toolbar ops-problem-filter">
        <label>Review state<select name="review"><option value="pending">Not reviewed</option><option value="all">All retained groups</option><option value="reviewed">Reviewed</option><option value="overridden">Severity changed</option></select></label>
        <label>Search problems<input name="q" type="search" maxlength="200" placeholder="DNS, Gmail, preparation…"></label>
        <label>Stage<select name="stage"><option value="">All stages</option>${OPERATION_STAGES.map((v) => `<option value="${v}">${v === "pipeline" ? "engine (legacy records)" : v}</option>`).join("")}</select></label>
      </form>
      <p class="ops-scope" id="ops-scope">Loading retained incident groups…</p>
      <div id="ops-bulk" class="ops-bulk" hidden><strong id="ops-selected" role="status"></strong><button class="btn" data-bulk="acknowledge">Mark reviewed</button><button class="btn" data-bulk="unacknowledge">Mark not reviewed</button><button class="btn" data-bulk="reset">Restore original severity</button><button class="btn btn-quiet" id="ops-clear-selection">Clear selection</button></div>
      <div id="ops-incidents" class="card"></div>
      <div class="ops-pagination"><label class="ops-page-size">Rows per page<select id="ops-problem-size" aria-label="Problem groups per page">${problemPageSizes.map((v) => `<option value="${v}"${v === problemPageSize ? " selected" : ""}>${v}</option>`).join("")}</select></label><button class="btn" id="ops-problem-prev">Previous</button><span id="ops-problem-page" role="status"></span><button class="btn" id="ops-problem-next">Next</button></div>
      <p class="fine-print">Up to 100 retained groups are loaded here, prioritizing unreviewed occurrences. Copy/export includes every group matching the current problem filters, not only the visible page. Groups and record counts are not independent causes.</p>
    </section>
    <section id="ops-journal" aria-label="Event history" hidden>
      <form class="toolbar" id="ops-filter">
        <label>Show<select name="view"><option value="significant">Meaningful outcomes</option><option value="all">All recorded events</option></select></label>
        <label>Run<select name="run_id" id="ops-run"><option value="">All retained runs</option></select></label>
        <label>Stage<select name="stage"><option value="">All stages</option>${OPERATION_STAGES.map((v) => `<option value="${v}">${v === "pipeline" ? "engine (legacy records)" : v}</option>`).join("")}</select></label>
        <label>Severity<select name="severity"><option value="">All severities</option><option>error</option><option>warning</option><option>critical</option><option>info</option></select></label>
        <label>Component<input name="component" maxlength="80" placeholder="e.g. gmail"></label>
        <label>Recorded message<input name="q" maxlength="200" placeholder="Message or error category"></label>
        <button class="btn" type="submit">Apply filters</button><button class="btn" type="button" id="ops-reset">Reset filters</button>
      </form>
      <div id="ops-events"></div><div class="ops-pagination"><label class="ops-page-size">Rows per page<select id="ops-event-size" aria-label="Event history rows per page">${eventPageSizes.map((v) => `<option value="${v}"${v === eventPageSize ? " selected" : ""}>${v}</option>`).join("")}</select></label><button class="btn" id="ops-newer">Newer page</button><button class="btn" id="ops-older">Older page</button><span id="ops-page-note"></span><button class="btn" id="ops-export" disabled>Export event page</button></div>
      <p class="fine-print">Event-page export includes this page, its filters and pagination marker. Use Export operations report above when you want one file containing the current problem groups, recorder/runtime context, and saved connection/account checks. Export reads local records only and never starts verification. Review exports before sharing.</p>
    </section>
    <section id="ops-tools" aria-label="Monitoring and retention" hidden>
      <p class="notice">The local operations journal is excluded from job-search backups. Recorder heartbeats do not establish task progress.</p>
      <div class="setup-inline"><button class="btn" id="ops-check">Check Prometheus & Grafana</button><a class="btn" href="http://localhost:3000/d/applypilot-operations" target="_blank" rel="noopener noreferrer">Open Grafana ↗</a></div>
      <div id="ops-diagnostics" hidden></div><div class="two-col"><div id="ops-workers"></div><div id="ops-active"></div></div><div id="ops-queues"></div>
      ${card("Local retention", "Telemetry limits, not application or research quotas.", `<form id="ops-settings" class="setup-inline"><label>Days of events<input name="retention_days" type="number" min="1" max="90" value="7"></label><label>Maximum event rows<input name="max_events" type="number" min="1000" max="2000000" value="200000"></label><button class="btn" type="submit" ${readOnly ? "disabled" : ""}>Save retention</button></form><p>Both limits apply to events. Incident groups use the day limit and a separate 10,000-group storage bound. Metrics counters survive pruning; PostgreSQL can reuse deleted-row space; retained events are not a cluster disk-size guarantee.</p>`)}
    </section>
    <dialog id="ops-inspector" class="ops-inspector" aria-labelledby="ops-inspector-title">
      <header class="ops-inspector-head"><div><span class="section-kicker">RECORDED INCIDENT</span><h2 id="ops-inspector-title"></h2></div><button class="btn" id="ops-close" autofocus>Close</button></header>
      <div class="ops-inspector-body"><div id="ops-inspector-summary"></div>
        <div class="ops-inspector-actions"><button class="btn btn-primary" id="ops-review"></button><button class="btn" id="ops-inspect-run">Open run history</button>
          <label>Display severity<select id="ops-severity" aria-describedby="ops-severity-help"><option value="reset">Original severity</option><option value="warning">Warning (display only)</option></select></label><button class="btn" id="ops-severity-save">Apply display choice</button></div>
        <p id="ops-severity-help" class="fine-print">To undo a warning override, choose Original severity and apply. This does not recover the operation. Critical, security, storage and uncertain-send incidents cannot be demoted.</p>
        <p id="ops-action-status" role="status"></p><div id="ops-inspector-next"></div>
        <details class="setup-details"><summary>Technical evidence & original record</summary><div id="ops-inspector-technical"></div></details>
      </div>
    </dialog>
  </div>`;
	settingsTracker = trackForm($("#ops-settings"), { readOnly });
	function switchView(view) {
		for (const button of $$("[data-view]"))
			button.setAttribute("aria-pressed", String(button.dataset.view === view));
		$("#ops-problems").hidden = view !== "problems";
		$("#ops-journal").hidden = view !== "history";
		$("#ops-tools").hidden = view !== "monitoring";
	}
	$$("[data-view]").forEach(
		(b) => (b.onclick = () => switchView(b.dataset.view)),
	);
	switchView(initial.get("run_id") ? "history" : "problems");
	function showError(message = "") {
		if (disposed) return;
		$("#ops-error").hidden = !message;
		$("#ops-error").textContent = message;
	}
	const beforeNavigate = (e) => {
		if (settingsTracker?.dirty() && e.detail)
			e.detail.confirmations.push("Leave your unsaved retention settings?");
	};
	const beforeUnload = (e) => {
		if (settingsTracker?.dirty()) {
			e.preventDefault();
			e.returnValue = "";
		}
	};
	const invalidate = () => load(true);
	window.addEventListener("applypilot:before-navigate", beforeNavigate);
	window.addEventListener("beforeunload", beforeUnload);
	window.addEventListener("applypilot:invalidate", invalidate);
	teardown = () => {
		disposed = true;
		clearTimeout(timer);
		if ($("#ops-inspector").open) $("#ops-inspector").close();
		settingsTracker?.destroy();
		window.removeEventListener("applypilot:before-navigate", beforeNavigate);
		window.removeEventListener("beforeunload", beforeUnload);
		window.removeEventListener("applypilot:invalidate", invalidate);
	};
	function problemFilterValues() {
		return Object.fromEntries(new FormData($("#ops-problem-filter")));
	}
	function matching() {
		const values = problemFilterValues(),
			query = values.q.trim().toLowerCase();
		return (data?.incidents || []).filter(
			(i) =>
				(values.review !== "pending" || !reviewed(i)) &&
				(values.review !== "reviewed" || reviewed(i)) &&
				(values.review !== "overridden" || !!i.display_severity) &&
				(!values.stage || i.stage === values.stage) &&
				(!query ||
					[i.message, i.stage, i.component, i.error_kind, explain(i).title]
						.join(" ")
						.toLowerCase()
						.includes(query)),
		);
	}
	function problemReportText() {
		const problems = matching(),
			values = problemFilterValues(),
			lines = [
				"ApplyPilot Operations problem report",
				`Generated: ${new Date().toISOString()}`,
				`Matching groups: ${problems.length} of ${data?.incidents?.length || 0} loaded; retained events: ${data?.event_count ?? "unknown"}`,
				`Filters: ${JSON.stringify(values)}`,
				"Note: review/display overrides are presentation state only; original severity remains in each record.",
				"",
			];
		problems.forEach((item, index) => {
			const e = explain(item);
			lines.push(
				`## ${index + 1}. ${e.title}`,
				`display severity: ${item.display_severity || item.severity}; original severity: ${item.severity}; review: ${reviewed(item) ? "reviewed" : "needs review"}`,
				`stage/component/operation: ${item.stage} / ${item.component} / ${item.operation}`,
				`records: ${item.occurrences}; first: ${when(item.first_at)}; last: ${when(item.last_at)}`,
				`message: ${item.message || ""}`,
				`next step: ${e.action || ""}`,
				"exact recorded group:",
				JSON.stringify(item, null, 2),
				"",
			);
		});
		return lines.join("\n");
	}
	function operationsReport() {
		const problems = matching();
		return {
			format: "applypilot_operations_support_report",
			format_version: 2,
			generated_at: new Date().toISOString(),
			scope: {
				problem_filters: problemFilterValues(),
				problem_groups_loaded: data?.incidents?.length || 0,
				problem_groups_matching: problems.length,
				problem_page_size: problemPageSize,
				event_filters: { ...filters },
				event_page_size: eventPageSize,
				event_page_before: before || null,
			},
			summary: {
				available: !!data?.available,
				error: data?.error || null,
				unreviewed_groups: data?.unacknowledged ?? null,
				retained_events: data?.event_count ?? null,
				retention: data?.settings || null,
			},
			problem_groups: problems,
			event_page: displayedEvents || { items: [], next_before: null },
			runs: data?.runs || [],
			processes: data?.processes || [],
			active_scopes: data?.active || [],
			gauges: data?.gauges || [],
			counters: data?.counters || [],
		};
	}
	let exportingBundle = false;
	$("#ops-export-bundle").onclick = async () => {
		if (exportingBundle) return;
		if (
			!window.confirm(
				"Export bounded database diagnostics, configuration and recent log tails? Credentials are redacted, but job prose and logs can contain personal information. Review the ZIP before sharing. No workers or network checks will be started.",
			)
		)
			return;
		exportingBundle = true;
		const button = $("#ops-export-bundle");
		button.disabled = true;
		const key = "applypilot-support-export";
		const readSaved = () => {
			try {
				return sessionStorage.getItem(key);
			} catch {
				return null;
			}
		};
		const save = (id) => {
			try {
				id ? sessionStorage.setItem(key, id) : sessionStorage.removeItem(key);
			} catch {}
		};
		try {
			let task = null,
				saved = readSaved();
			if (saved) {
				const existing = await fetch(
					"/api/operations/support-bundle/" + encodeURIComponent(saved),
					{
						credentials: "same-origin",
						cache: "no-store",
						signal: AbortSignal.timeout(15000),
					},
				);
				if (existing.ok) task = await existing.json();
				else if (existing.status === 404) save(null);
				else
					throw new Error(
						"Unable to check the saved support export. Reopen Operations and retry.",
					);
				if (task?.state === "failed") {
					task = null;
					save(null);
				}
			}
			if (!task) {
				const session = await api("session");
				const response = await fetch("/api/operations/support-bundle", {
					method: "POST",
					credentials: "same-origin",
					cache: "no-store",
					headers: {
						"Content-Type": "application/json",
						"X-ApplyPilot-CSRF": session.csrf,
						Accept: "application/json",
					},
					body: "{}",
					signal: AbortSignal.timeout(15000),
				});
				task = await response.json();
				if (!response.ok)
					throw new Error(task.error || "Support export could not start.");
				save(task.id);
			}
			while (task.state === "building") {
				if (disposed) return; // Artifact survives navigation; next click resumes polling.
				button.textContent =
					"Building ZIP: " +
					String(task.phase || "queued").replaceAll("_", " ") +
					"…";
				await new Promise((resolve) => setTimeout(resolve, 1000));
				const response = await fetch(
					"/api/operations/support-bundle/" + encodeURIComponent(task.id),
					{
						credentials: "same-origin",
						cache: "no-store",
						signal: AbortSignal.timeout(15000),
					},
				);
				const value = await response.json();
				if (!response.ok)
					throw new Error(
						value.error ||
							"Support export status unavailable. Retry to resume.",
					);
				task = value;
			}
			if (task.state !== "ready") {
				save(null);
				throw new Error(task.error || "Support export failed.");
			}
			const expected =
				"/api/operations/support-bundle/" + task.id + "/download";
			if (!(task.bytes > 22) || task.download_url !== expected)
				throw new Error("Invalid or empty support ZIP; download refused.");
			if (disposed) return;
			// Native download: no 180-second build/body timeout, full-file Blob, or
			// premature object-URL/file deletion. The retained link can be retried.
			const link = document.createElement("a");
			link.href = expected;
			link.download = "applypilot-support.zip";
			document.body.appendChild(link);
			link.click();
			link.remove();
			notify(
				"Validated ZIP ready (" +
					Math.ceil(task.bytes / 1024).toLocaleString() +
					" KB). Download requested; click again to retry the retained file. Review manifest.json before sharing.",
			);
		} catch (error) {
			if (!disposed)
				notify(
					error.message ||
						"Support ZIP failed. Retry to resume the saved export.",
					true,
				);
		} finally {
			exportingBundle = false;
			if (!disposed) {
				button.disabled = false;
				button.textContent = "Export / retry support ZIP";
			}
		}
	};
	function paintSelection() {
		$("#ops-bulk").hidden = !selected.size;
		$("#ops-selected").textContent = `${selected.size} selected`;
		$$("[data-bulk]").forEach((b) => (b.disabled = readOnly || mutation));
		const boxes = $$("[data-select]"),
			all = $("#ops-select-all");
		if (all) {
			all.checked = !!boxes.length && boxes.every((b) => b.checked);
			all.indeterminate = boxes.some((b) => b.checked) && !all.checked;
		}
	}
	function paintIncidents(force = false) {
		if (!data) return;
		if (!force && $("#ops-incidents").contains(document.activeElement)) return;
		const rows = matching();
		page = Math.min(
			page,
			Math.max(0, Math.ceil(rows.length / problemPageSize) - 1),
		);
		const visible = rows.slice(
				page * problemPageSize,
				(page + 1) * problemPageSize,
			),
			retained = data.incidents?.length || 0;
		$("#ops-scope").textContent =
			`${fmt(rows.length)} matching groups in ${fmt(retained)} loaded · select a title for evidence and reversible controls`;
		$("#ops-incidents").innerHTML = visible.length
			? `<div class="table-scroll"><table class="ops-table"><caption class="sr-only">Retained incident groups; rows are not independent root causes</caption><thead><tr><th class="ops-select-cell"><input id="ops-select-all" type="checkbox" aria-label="Select all groups on this page" ${readOnly ? "disabled" : ""}></th><th>Problem</th><th>Display / review</th><th>Records</th><th>Last seen</th></tr></thead><tbody>${visible.map((i) => `<tr><td><input type="checkbox" data-select="${esc(i.fingerprint)}" aria-label="Select ${esc(explain(i).title)}" ${selected.has(i.fingerprint) ? "checked" : ""} ${readOnly ? "disabled" : ""}></td><th scope="row"><button class="ops-row-title" data-inspect="${esc(i.fingerprint)}">${esc(explain(i).title)}</button><span class="ops-row-meta">${esc(human(i.stage))} / ${esc(human(i.component))}${i.display_severity ? " · original: " + esc(i.severity) : ""}</span></th><td>${tag(i.display_severity || i.severity)}<span class="ops-row-meta">${reviewed(i) ? "Reviewed" : "Needs review"}</span></td><td class="ops-number">${fmt(i.occurrences)}</td><td class="ops-last">${esc(when(i.last_at))}</td></tr>`).join("")}</tbody></table></div>`
			: '<p class="ops-empty">No groups match this view. This is not a health check; try All retained groups or inspect current runtime status.</p>';
		$("#ops-problem-prev").disabled = page === 0;
		$("#ops-problem-next").disabled =
			(page + 1) * problemPageSize >= rows.length;
		$("#ops-problem-page").textContent =
			`Page ${page + 1} of ${Math.max(1, Math.ceil(rows.length / problemPageSize))} · ${fmt(rows.length)} groups`;
		paintSelection();
	}
	function paintInspector() {
		if (!inspector) return;
		const i = inspector,
			e = explain(i),
			protectedIncident =
				["storage", "security", "send_unknown"].includes(i.error_kind) ||
				i.severity === "critical";
		$("#ops-inspector-title").textContent = e.title;
		$("#ops-inspector-summary").innerHTML =
			`<div class="ops-inspector-tags">${tag(i.display_severity || i.severity)}<strong>${reviewed(i) ? "Reviewed" : "Needs review"}</strong><span>Original severity: ${esc(i.severity)}</span></div><p>${esc(e.meaning)}</p><p class="fine-print">${fmt(i.occurrences)} recorded occurrences · first ${esc(when(i.first_at))} · last ${esc(when(i.last_at))}. This inspector is a snapshot. A later occurrence needs review again.</p>`;
		const review = $("#ops-review");
		review.textContent = reviewed(i)
			? "Mark not reviewed"
			: "Mark these occurrences reviewed";
		review.disabled = readOnly || mutation;
		$("#ops-severity").value = i.display_severity ? "warning" : "reset";
		$("#ops-severity").disabled = readOnly || mutation;
		$("#ops-severity").querySelector('[value="warning"]').disabled =
			protectedIncident;
		$("#ops-severity-save").disabled = readOnly || mutation;
		$("#ops-inspect-run").disabled = !i.last_run_id;
		const recovery = (data?.stage_health || []).find(
			(h) =>
				h.stage === "backup" &&
				h.outcome === "success" &&
				h.last_at > i.last_at,
		);
		$("#ops-inspector-next").innerHTML =
			`${e.action ? `<section class="ops-next"><h3>Next step</h3><p>${esc(e.action)}</p>${e.href ? `<a class="btn" href="${e.href}">${esc(e.link)}</a>` : ""}</section>` : ""}${i.stage === "backup" ? `<p class="notice">${recovery ? "A later backup sync reported success; it does not independently verify the earlier release. " : ""}<a href="#backups">Inspect backups & recovery →</a></p>` : ""}`;
		$("#ops-inspector-technical").innerHTML = technical(i);
	}
	$("#ops-incidents").onclick = (e) => {
		const button = e.target.closest("[data-inspect]");
		if (!button) return;
		const record = data.incidents.find(
			(i) => i.fingerprint === button.dataset.inspect,
		);
		if (!record) return;
		inspector = { ...record };
		returnFocus = button;
		$("#ops-action-status").textContent = "";
		paintInspector();
		$("#ops-inspector").showModal();
		$("#ops-close").focus();
	};
	$("#ops-incidents").onchange = (e) => {
		if (e.target.id === "ops-select-all") {
			$$("[data-select]").forEach((b) => {
				b.checked = e.target.checked;
				if (b.checked) selected.add(b.dataset.select);
				else selected.delete(b.dataset.select);
			});
		} else if (e.target.dataset.select) {
			if (e.target.checked) selected.add(e.target.dataset.select);
			else selected.delete(e.target.dataset.select);
		}
		paintSelection();
	};
	$("#ops-close").onclick = () => $("#ops-inspector").close();
	$("#ops-inspector").addEventListener("close", () => {
		const fp = inspector?.fingerprint;
		inspector = null;
		const replacement = fp
			? $$("[data-inspect]").find((b) => b.dataset.inspect === fp)
			: null;
		const target = returnFocus?.isConnected
			? returnFocus
			: replacement || $('[data-view="problems"]');
		target.focus({ preventScroll: true });
	});
	$("#ops-inspector-next").onclick = (e) => {
		if (e.target.closest("a")) $("#ops-inspector").close();
	};
	async function act(action, items) {
		if (readOnly || mutation || !items.length) return;
		mutation = true;
		paintSelection();
		if (inspector) paintInspector();
		$("#ops-action-status").textContent = "Saving presentation change…";
		try {
			await api("operations/incident", {
				method: "POST",
				body: {
					action,
					items: items.map((i) => ({
						fingerprint: i.fingerprint,
						last_at: i.last_at,
					})),
				},
			});
			if (disposed) return;
			for (const item of items) {
				const apply = (i) => {
					if (action === "acknowledge")
						i.acknowledged_at = Math.max(i.acknowledged_at || 0, item.last_at);
					else if (action === "unacknowledge") i.acknowledged_at = null;
					else i.display_severity = action === "warning" ? "warning" : null;
				};
				const row = data.incidents.find(
					(i) => i.fingerprint === item.fingerprint,
				);
				if (row) apply(row);
				if (inspector?.fingerprint === item.fingerprint) apply(inspector);
			}
			selected.clear();
			paintIncidents(true);
			const message =
				action === "reset"
					? "Original severity restored."
					: action === "warning"
						? "Display changed to warning. Choose Original severity to undo."
						: action === "unacknowledge"
							? "Marked not reviewed."
							: "Displayed occurrences marked reviewed. New occurrences still need review.";
			$("#ops-action-status").textContent = message;
			notify(message + " Execution is unchanged.");
		} catch (e) {
			if (!disposed) {
				$("#ops-action-status").textContent = e.message;
				notify(e.message, true);
			}
		} finally {
			mutation = false;
			if (!disposed) {
				paintSelection();
				if (inspector) paintInspector();
				load();
			}
		}
	}
	$("#ops-review").onclick = () =>
		act(reviewed(inspector) ? "unacknowledge" : "acknowledge", [
			{ ...inspector },
		]);
	$("#ops-severity-save").onclick = () =>
		act($("#ops-severity").value, [{ ...inspector }]);
	$("#ops-inspect-run").onclick = () => {
		filters = { ...defaults(), run_id: inspector.last_run_id, view: "all" };
		before = "";
		history = [];
		$("#ops-inspector").close();
		switchView("history");
		$('[data-view="history"]').focus();
		revision++;
		load();
	};
	$$("[data-bulk]").forEach(
		(b) =>
			(b.onclick = () =>
				act(
					b.dataset.bulk,
					data.incidents.filter((i) => selected.has(i.fingerprint)),
				)),
	);
	$("#ops-clear-selection").onclick = () => {
		selected.clear();
		paintIncidents(true);
	};
	const filterProblems = () => {
		page = 0;
		selected.clear();
		paintIncidents(true);
	};
	$("#ops-problem-filter").onsubmit = (e) => {
		e.preventDefault();
		filterProblems();
	};
	$("#ops-problem-filter").oninput = filterProblems;
	$("#ops-problem-prev").onclick = () => {
		page--;
		selected.clear();
		paintIncidents(true);
	};
	$("#ops-problem-next").onclick = () => {
		page++;
		selected.clear();
		paintIncidents(true);
	};
	$("#ops-problem-size").onchange = (e) => {
		problemPageSize = Number(e.target.value);
		rememberChoice("ap-operations-problem-page-size", problemPageSize);
		page = 0;
		selected.clear();
		paintIncidents(true);
	};
	let sharing = false;
	async function shareReport(copy = false) {
		if (sharing) return;
		sharing = true;
		$("#ops-copy-report").disabled = true;
		$("#ops-export-report").disabled = true;
		// Snapshot visible evidence before awaiting local diagnostics so live refresh
		// cannot silently change which groups the owner elected to share.
		const report = operationsReport(),
			text = copy ? problemReportText() : null;
		try {
			report.support_context = await api("operations/support-context");
			if (disposed) return;
			if (copy) {
				await copyText(
					text +
						"\n\n## Saved setup and backup diagnostics\n" +
						"Saved local results only; no checks were rerun. Raw credentials and provider output are excluded.\n" +
						JSON.stringify(report.support_context, null, 2),
				);
			} else {
				const stamp = new Date().toISOString().replace(/[:.]/g, "-");
				downloadJson(`applypilot-operations-report-${stamp}.json`, report);
			}
			notify(
				`${copy ? "Copied" : "Exported"} ${fmt(report.problem_groups.length)} matching problem groups plus saved setup and backup diagnostics. Review before sharing.`,
			);
		} catch (e) {
			if (!disposed)
				notify("The support report was not created: " + e.message, true);
		} finally {
			sharing = false;
			if (!disposed) {
				$("#ops-copy-report").disabled = !data?.available;
				$("#ops-export-report").disabled = !data?.available;
			}
		}
	}
	$("#ops-copy-report").onclick = () => shareReport(true);
	$("#ops-export-report").onclick = () => shareReport(false);
	function paint(value, events) {
		data = value;
		if (!data.available)
			showError(data.error || "The operations journal is unavailable.");
		$("#ops-stats").innerHTML =
			`<span><strong>${data.available ? fmt(data.unacknowledged) : "—"}</strong> unreviewed groups</span><span><strong>${data.available ? fmt(data.event_count) : "—"}</strong> retained events</span>`;
		$("#ops-copy-report").disabled = sharing || !data.available;
		$("#ops-export-report").disabled = sharing || !data.available;
		const processes = data.processes || [],
			stale = processes.filter((p) => p.state === "stale").length,
			dropped = processes.reduce((a, p) => a + Number(p.dropped || 0), 0),
			writes = processes.reduce((a, p) => a + Number(p.write_errors || 0), 0);
		$("#ops-recording-warning").hidden = !(stale || dropped || writes);
		$("#ops-recording-warning").textContent =
			`Recorder samples: ${fmt(stale)} stale processes, ${fmt(dropped)} dropped events and ${fmt(writes)} write failures. Evidence may be incomplete; see Monitoring.`;
		if (!$("#ops-filter").contains(document.activeElement)) {
			$("#ops-run").innerHTML =
				'<option value="">All retained runs</option>' +
				(data.runs || [])
					.map(
						(r) =>
							`<option value="${esc(r.id)}">${esc(r.role)} · ${esc(when(r.started_at))} · ${esc(r.id.slice(0, 8))}</option>`,
					)
					.join("");
			if (
				filters.run_id &&
				!(data.runs || []).some((r) => r.id === filters.run_id)
			)
				$("#ops-run").insertAdjacentHTML(
					"beforeend",
					`<option value="${esc(filters.run_id)}">${esc(filters.run_id)}</option>`,
				);
			for (const [k, v] of Object.entries(filters))
				$("#ops-filter").elements.namedItem(k).value = v;
		}
		if (
			!$("#ops-events").querySelector("details[open]") &&
			!$("#ops-events").contains(document.activeElement)
		) {
			$("#ops-events").innerHTML = card(
				"Recorded outcomes",
				"Newest first; every displayed outcome remains an independent journal record.",
				events.items.length
					? events.items
							.map((e) => {
								const x = explain(e);
								return `<details class="setup-details"><summary>${tag(e.severity)} ${tag(e.outcome)} <strong>${esc(x.title)}</strong> · ${esc(when(e.at))}</summary><p>${esc(x.meaning)}</p>${x.action ? `<p><strong>Next step:</strong> ${esc(x.action)}</p>` : ""}<p>${esc(e.stage)} / ${esc(e.component)} / ${esc(e.operation)} · ${Number(e.duration || 0).toFixed(2)} seconds</p>${technical(e)}</details>`;
							})
							.join("")
					: "<p>No matching outcomes on this page. This does not establish that the application is healthy.</p>",
			);
			displayedEvents = events;
			nextBefore = events.next_before;
			$("#ops-export").disabled = !data.available;
		}
		$("#ops-older").disabled = !nextBefore;
		$("#ops-newer").disabled = !history.length;
		$("#ops-page-note").textContent = before
			? "Historical page"
			: "Latest page · open evidence is not replaced by live updates";
		selected = new Set(
			[...selected].filter((fp) =>
				(data.incidents || []).some((i) => i.fingerprint === fp),
			),
		);
		paintIncidents();
		$("#ops-workers").innerHTML = card(
			"Recorder heartbeats",
			"Telemetry processes, not completed application workers.",
			processes
				.slice(0, 30)
				.map(
					(p) =>
						`<p>${tag(p.state)} ${esc(p.role)} · PID ${esc(p.pid)} · ${esc(when(p.heartbeat))}<br>${fmt(p.queue_depth)} queued · ${fmt(p.write_errors)} write failures</p>`,
				)
				.join("") || "<p>No recorder heartbeat reported.</p>",
		);
		$("#ops-active").innerHTML = card(
			"In-flight scopes",
			"Long-lived scopes alone do not establish useful progress.",
			(data.active || [])
				.slice(0, 40)
				.map(
					(a) =>
						`<p><strong>${esc(a.stage)} / ${esc(a.component)} / ${esc(a.operation)}</strong><br>Started ${esc(when(a.started_at))}</p>`,
				)
				.join("") || "<p>No instrumented scopes reported.</p>",
		);
		$("#ops-queues").innerHTML = card(
			"Last observed eligible queues",
			"Samples can become stale; these are not application totals.",
			(data.gauges || [])
				.map(
					(g) =>
						`<p>${esc(g.stage)}: <strong>${fmt(g.value)}</strong> · ${esc(when(g.updated_at))}</p>`,
				)
				.join("") || "<p>No queue sample reported.</p>",
		);
		if (
			!settingsTracker.dirty() &&
			!$("#ops-settings").contains(document.activeElement)
		) {
			for (const key of ["retention_days", "max_events"])
				if (data.settings?.[key] !== undefined)
					$("#ops-settings").elements[key].value = data.settings[key];
			settingsTracker.reset();
		}
	}
	async function load(automatic = false) {
		if (disposed || loading) return;
		if (automatic && (updatesPaused() || document.hidden)) {
			clearTimeout(timer);
			timer = setTimeout(() => load(true), 1500);
			return;
		}
		loading = true;
		clearTimeout(timer);
		const version = revision;
		try {
			const value = await api("operations");
			const events = value.available
				? await api(
						"operations/events?" +
							new URLSearchParams({
								...filters,
								before,
								limit: String(eventPageSize),
							}),
					)
				: { items: [], next_before: null };
			if (
				!disposed &&
				version === revision &&
				(!automatic || !updatesPaused())
			) {
				showError();
				paint(value, events);
			}
		} catch (e) {
			if (!disposed) showError(e.message + " Displayed evidence may be stale.");
		} finally {
			loading = false;
			if (!disposed)
				timer = setTimeout(
					() => load(true),
					version !== revision
						? 0
						: document.documentElement.dataset.liveTransport === "live"
							? 30000
							: 5000,
				);
		}
	}
	function changePage() {
		$$("#ops-events details").forEach((d) => (d.open = false));
		$("#ops-export").disabled = true;
		revision++;
		load();
	}
	$("#ops-filter").onsubmit = (e) => {
		e.preventDefault();
		filters = Object.fromEntries(new FormData(e.target));
		before = "";
		history = [];
		changePage();
	};
	$("#ops-reset").onclick = () => {
		filters = defaults();
		before = "";
		history = [];
		$("#ops-filter").reset();
		changePage();
	};
	$("#ops-older").onclick = () => {
		if (nextBefore) {
			history.push(before);
			before = String(nextBefore);
			changePage();
		}
	};
	$("#ops-newer").onclick = () => {
		before = history.pop() || "";
		changePage();
	};
	$("#ops-event-size").onchange = (e) => {
		eventPageSize = Number(e.target.value);
		rememberChoice("ap-operations-event-page-size", eventPageSize);
		before = "";
		history = [];
		changePage();
	};
	$("#ops-settings").onsubmit = async (e) => {
		e.preventDefault();
		if (readOnly || !settingsTracker.dirty() || !e.target.checkValidity())
			return;
		settingsTracker.busy(true);
		try {
			await api("operations/settings", {
				method: "POST",
				body: Object.fromEntries(
					[...new FormData(e.target)].map(([k, v]) => [k, Number(v)]),
				),
			});
			if (!disposed) {
				settingsTracker.reset();
				notify("Local telemetry retention saved.");
			}
		} catch (e) {
			if (!disposed) notify(e.message, true);
		} finally {
			if (!disposed) settingsTracker.busy(false);
		}
	};
	$("#ops-check").onclick = async () => {
		const button = $("#ops-check");
		button.disabled = true;
		try {
			const value = await api("operations/diagnostics");
			if (!disposed) {
				$("#ops-diagnostics").hidden = false;
				$("#ops-diagnostics").innerHTML = card(
					"Monitoring diagnostics",
					"Fixed local ports; no model requests or paid probes.",
					pre(value),
				);
			}
		} catch (e) {
			if (!disposed) notify(e.message, true);
		} finally {
			if (!disposed) button.disabled = false;
		}
	};
	$("#ops-export").onclick = () => {
		if (!displayedEvents) return;
		downloadJson("applypilot-operations-event-page.json", displayedEvents);
	};
	load();
}
