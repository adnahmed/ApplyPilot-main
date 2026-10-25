import { statusView } from "./live-status.js";
import { runtimeHealth, schedulerLink, stageQueues } from "./runtime-health.js";
import { confirmAction, updatesPaused } from "./ux.js";

const esc = (v) =>
	String(v ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const names = {
	applications: "Applications",
	enrich: "Enrichment",
	score: "Scoring",
	tailor: "Résumé tailoring",
	cover: "Cover letters",
	pdf: "Document generation",
	brief: "Interview briefs",
	discovery: "Discovery",
	tracking: "Mailbox tracking",
	unattended: "Networking",
	referrals: "Referral planning",
	outreach: "Outreach delivery",
	backup: "Backups",
	company_research: "Company research",
};
const phases = {
	working: "Working",
	waiting_provider: "Awaiting model response",
	waiting_capacity: "Waiting for context inference capacity",
	dask_running: "Starting application task",
	queued: "Queued by Dask",
	navigating: "Opening job page",
	reading_page: "Reading page",
	saving: "Saving result",
	checking_inbox: "Checking inbox",
	between_jobs: "Waiting for next task",
	no_selectable_job: "No selectable job",
	selecting: "Checking next job",
	browser_unavailable: "Browser unavailable",
	linkedin_cooldown: "LinkedIn cooldown",
};
const states = {
	running: "Working",
	waiting_provider: "Awaiting model",
	waiting_capacity: "Waiting for capacity",
	idle: "Idle",
	waiting: "Waiting",
	blocked: "Blocked",
	unverified: "Not reporting",
	stopped: "Ended",
};
const issues = {
	model_output_incomplete: "Model returned no usable output",
	model_invalid_json: "Model returned invalid JSON",
	model_schema_error: "Model response has invalid fields",
	extraction_empty: "No usable job details extracted",
	provider_concurrency_limit: "Provider concurrency limit",
	inbox_backlog: "Inbox processing backlog",
	provider: "Model provider failed",
	schema: "Response format failed",
	network: "Network failure",
	auth: "Provider authentication/access failure",
	timeout: "Operation timed out",
	storage: "Database/storage read failed",
	policy: "A permission or delivery guard blocked work",
	send_unknown: "Delivery outcome is uncertain",
	dependency: "Required component unavailable",
	configuration: "Configuration needs attention",
	unknown:
		"This report has no detailed diagnosis; inspect the originating operation",
	document_latex_missing_field: "LaTeX template is missing a required field",
	document_renderer_fallback: "LaTeX failed; Chromium fallback attempted",
	model_bridge_timeout: "Local model-call bridge timed out",
	extraction_empty_page: "Page had no usable text for extraction",
	source_http_404: "Job URL returned HTTP 404",
	source_http_410: "Job URL returned HTTP 410",
	source_dns: "Job hostname could not be resolved",
	gmail_transport_access:
		"Gmail subprocess reported a file/program access failure",
	sqlite_interrupted: "Dashboard database read was interrupted",
	document_validation: "Generated document failed validation",
	rate_limit: "Provider rate limit reached",
	cancelled: "Operation cancelled",
	security: "Security check blocked the operation",
};
export const issueText = (row) =>
	row.last_issue?.summary ||
	issues[row.last_issue?.code] ||
	`${names[row.stage] || row.stage || "Worker"} / ${row.operation || "operation"} reported a warning; inspect the run record`;
const when = (s) => {
	const at = typeof s === "number" ? s : Date.parse(s || "") / 1000;
	return Number.isFinite(at) && at > 0
		? new Date(at * 1000).toLocaleString()
		: "Not reported";
};
const number = (v) =>
	typeof v === "number" && Number.isFinite(v) ? v.toLocaleString() : "—";
const duration = (s, now = Date.now() / 1000) => {
	if (typeof s !== "number" || !Number.isFinite(s) || s <= 0) return "—";
	const v = Math.max(0, Math.floor(now - s));
	return v < 60
		? `${v}s`
		: v < 3600
			? `${Math.floor(v / 60)}m ${v % 60}s`
			: `${Math.floor(v / 3600)}h ${Math.floor((v % 3600) / 60)}m`;
};
const stamp = (value) =>
	typeof value === "number" ? value : Date.parse(value || "") / 1000;
export function effectiveRows(data, now = Date.now() / 1000) {
	const beat = stamp(data?.runtime_at),
		parentCurrent =
			data?.runtime_current === true &&
			Number.isFinite(beat) &&
			now - beat <= 15 &&
			now - beat >= -5;
	return (data?.rows || []).map((row) => {
		const sample = stamp(row.sample_at),
			fresh =
				parentCurrent &&
				row.fresh === true &&
				Number.isFinite(sample) &&
				now - sample <= 20 &&
				now - sample >= -5;
		return fresh || row.state === "stopped"
			? { ...row }
			: {
					...row,
					state: "unverified",
					active: false,
					fresh: false,
					reason: "Report expired; current activity is unknown.",
				};
	});
}
export function filterRows(
	rows,
	{ kind = "all", state = "current", q = "" } = {},
) {
	const query = q.toLocaleLowerCase();
	return rows.filter(
		(r) =>
			(kind === "all" || r.kind === kind) &&
			(state === "all" ||
				(state === "current"
					? r.state !== "stopped"
					: state === "issues"
						? ["blocked", "unverified"].includes(r.state) ||
							Boolean(r.last_issue)
						: state === r.state)) &&
			(!query ||
				[
					r.worker,
					names[r.stage],
					r.job_title,
					r.company,
					r.last_job_title,
					r.reason,
					r.last_issue ? issueText(r) : "",
				]
					.join(" ")
					.toLocaleLowerCase()
					.includes(query)),
	);
}
function remembered() {
	try {
		const v = Number(localStorage.getItem("ap-workers-page-size"));
		return [10, 25, 50, 100].includes(v) ? v : 25;
	} catch {
		return 25;
	}
}
const phase = (r) =>
	r.model_call_state === "deferred" && r.state === "waiting_provider"
		? "Waiting for provider eligibility"
		: phases[r.phase] || states[r.state] || "Status not recorded";
const label = (r) =>
	r.kind === "application"
		? r.worker
		: `${names[r.stage] || r.stage} · ${String(r.key).slice(-5)}`;
function operationSummary(row) {
	const partial = row.partial_operations;
	return typeof partial === "number"
		? `${number(row.completed_operations)} succeeded / ${number(Math.max(0, (row.failed_operations || 0) - partial))} failed / ${number(partial)} incomplete`
		: `${number(row.completed_operations)} succeeded / ${number(row.failed_operations)} failed or incomplete`;
}
const badge = (state) =>
	`<span class="worker-state worker-state-${esc(Object.hasOwn(states, state) ? state : "unverified")}">${esc(states[state] || "Unknown")}</span>`;
function saveFile(
	value,
	name = `applypilot-workers-${new Date().toISOString().replace(/[:.]/g, "-")}.json`,
) {
	const url = URL.createObjectURL(
			new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
		),
		a = document.createElement("a");
	a.href = url;
	a.download = name;
	a.click();
	setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function completeTrace(api, traceId, source, segment) {
	let before = null,
		text = "",
		available = false,
		size = 0;
	const seen = new Set();
	while (true) {
		const query = new URLSearchParams({
			id: traceId,
			source,
			segment: String(segment),
		});
		if (before !== null) query.set("before", String(before));
		const part = await api("workers/trace?" + query);
		if (!part.available) return available ? { available, text, size } : null;
		available = true;
		size = part.size || size;
		text = String(part.text || "") + text;
		const next = part.next_before;
		if (next == null || next === 0) return { available: true, text, size };
		if (seen.has(next))
			throw new Error(
				`Trace pagination stopped making progress for ${source}.`,
			);
		seen.add(next);
		before = next;
	}
}
async function applicationTraceBundle(api, rows, links = []) {
	const grouped = new Map();
	for (const row of rows || []) {
		if (
			row.kind !== "application" ||
			!/^[a-f0-9]{32}$/.test(row.trace_id || "")
		)
			continue;
		const item = grouped.get(row.trace_id) || {
			trace_id: row.trace_id,
			workers: [],
			sources: {},
		};
		item.workers.push({
			worker: row.worker,
			worker_id: row.worker_id ?? null,
			job_title: row.job_title || row.last_job_title || "",
			company: row.company || "",
			state: row.state,
			phase: row.phase,
		});
		grouped.set(row.trace_id, item);
	}
	for (const link of links || []) {
		if (!/^[a-f0-9]{32}$/.test(link.trace_id || "") || !link.attempt_id)
			continue;
		const item = grouped.get(link.trace_id) || {
			trace_id: link.trace_id,
			workers: [],
			sources: {},
		};
		item.attempt_ids = [
			...new Set([...(item.attempt_ids || []), link.attempt_id]),
		];
		grouped.set(link.trace_id, item);
	}
	for (const item of grouped.values())
		for (const source of ["agent", "aleph", "browser", "learning", "rlm"]) {
			const segments = [];
			for (const segment of [2, 1, 0]) {
				const value = await completeTrace(api, item.trace_id, source, segment);
				if (value) segments.push({ segment, ...value });
			}
			item.sources[source] = { available: segments.length > 0, segments };
		}
	return [...grouped.values()];
}
async function retainedWorkRun(api, runId) {
	if (!runId) return null;
	let task = await api("work/exports", {
		method: "POST",
		body: { run_id: runId },
	});
	while (task.state === "building") {
		await pause(500);
		task = await api("work/exports/" + encodeURIComponent(task.id));
	}
	if (task.state !== "ready" || !task.download_url)
		throw new Error(task.error || "Run export did not complete.");
	const response = await fetch(task.download_url, {
		credentials: "same-origin",
		cache: "no-store",
	});
	if (!response.ok)
		throw new Error(`Run export download failed (${response.status}).`);
	return await response.json();
}
async function copy(value) {
	if (navigator.clipboard?.writeText) {
		await navigator.clipboard.writeText(value);
		return;
	}
	const prior = document.activeElement,
		t = document.createElement("textarea");
	t.value = value;
	t.setAttribute("readonly", "");
	t.style.cssText = "position:fixed;left:-9999px";
	document.body.append(t);
	t.select();
	const ok = document.execCommand?.("copy");
	t.remove();
	prior?.focus?.();
	if (!ok) throw new Error("Clipboard unavailable. Use Export workers report.");
}

function selectionDetails(row) {
	const s = row.selection;
	if (!s) return "";
	const heading = `<h3>Why no application was selected</h3><p>Diagnostic snapshot: ${esc(when(s.captured_at))}${s.stale ? " — older than three minutes; not current" : ""}. ${esc(s.scope)}</p>`;
	if (!s.available)
		return (
			heading +
			`<p>Diagnosis unavailable: ${esc(s.reason)}. Missing evidence is not zero candidates.</p>`
		);
	const assumptions = (s.optimistic_assumptions || [])
		.map((v) => `${esc(v.code)}: ${number(v.count)}`)
		.join("; ");
	const allocation = s.allocation || {};
	return (
		heading +
		`<p>${number(s.cohort_count)} jobs in this score/target cohort; ${number(s.passing_common_checks)} pass common checks; ${number(s.allocated_preview)} selected by the later allocation preview. Final reservation and browser execution remain separate.</p><p>Allocation: exploration share ${esc(allocation.exploration_share ?? "not reported")}; idle-capacity fill ${allocation.fill_idle_capacity === true ? "enabled" : allocation.fill_idle_capacity === false ? "disabled" : "not reported"}; unused preferred-slot transfer ${allocation.reallocate_unused_to_preferred === true ? "enabled" : allocation.reallocate_unused_to_preferred === false ? "disabled" : "not reported"}.</p>${s.optimistic_assumption_jobs ? `<p><strong>${number(s.optimistic_assumption_jobs)} cohort jobs carry recorded optimistic assumptions.</strong> ${assumptions || "Assumption categories were not reported."} These are provenance, not application permission.</p>` : ""}<table><caption>First failing checks (each job counted once)</caption><thead><tr><th scope="col">Check</th><th scope="col">Jobs</th></tr></thead><tbody>${(s.first_blockers || []).map((b) => `<tr><td>${esc(b.label)}</td><td>${number(b.count)}</td></tr>`).join("")}</tbody></table><details><summary>Example jobs and failed checks${s.samples_truncated ? " (sample limited)" : ""}</summary>${(s.samples || []).map((v) => `<p><strong>${esc(v.job_title || v.job_id)}</strong> · ${esc(v.company)}<br>${esc(v.label)}</p>`).join("")}</details>`
	);
}

export function mountWorkers(
	root,
	{ api, notify = () => {}, readOnly = false },
) {
	let data = null,
		savedData = null,
		liveStatus = null,
		error = "",
		alive = true,
		busy = false,
		timer = null,
		lastAttempt = 0,
		page = 0,
		pageSize = remembered(),
		opener = null;
	const filters = { kind: "all", state: "current", q: "" };
	let trace = null,
		traceTimer = null,
		traceRevision = 0,
		bundleBusy = false;
	root.innerHTML = `<section class="workers-view" aria-labelledby="workers-title">
    <header class="workers-heading"><div><h2 id="workers-title">Execution</h2><p>See what is running, what is waiting, and why.</p></div><div class="workers-actions"><button type="button" class="btn" data-copy disabled>Copy workers report</button><button type="button" class="btn" data-export disabled>Export workers report</button><button type="button" class="btn" data-bundle disabled>Export run + traces</button></div></header>
    <details class="card"><summary>Temporal durable execution</summary>
      <p>Native orchestration is separate from the legacy Start/Stop controls. Refresh observes the server; it does not start work.</p>
      <button type="button" class="btn" data-temporal-refresh>Capture Temporal state</button>
      <pre data-temporal-status>Loading last explicit capture…</pre></details>
    <div data-health role="status" aria-live="polite">Waiting for a worker report…</div>
    <div class="workers-metrics" data-metrics></div>
    <div data-stages></div>
    <div class="workers-health workers-warning" data-bottlenecks role="status" hidden></div>
    <div class="workers-filters"><label>Worker group<select data-kind><option value="all">All groups</option><option value="application">Applications</option><option value="preparation">Preparation</option><option value="service">Background services</option></select></label><label>Show<select data-state aria-label="Show"><option value="current">Current & not reporting</option><option value="all">All reported slots</option><option value="running">Working</option><option value="waiting_provider">Awaiting model</option><option value="idle">Idle</option><option value="issues">Issues & blockers</option><option value="stopped">Ended slots</option></select></label><label class="workers-search">Search workers or jobs<input type="search" data-search placeholder="Job title, stage, or wait reason" maxlength="200"></label></div>
    <section class="card workers-list" aria-label="Worker report"><p class="workers-caption" data-caption></p><div class="workers-table-scroll"><table class="workers-table"><caption class="sr-only">Actual worker execution slots and their last reported task</caption><thead><tr><th scope="col">Worker / stage</th><th scope="col">Current task or wait reason</th><th scope="col">State / elapsed</th><th scope="col">Reported outcomes</th></tr></thead><tbody data-rows></tbody></table></div></section>
    <div class="workers-pagination"><label>Rows per page<select data-size>${[10, 25, 50, 100].map((n) => `<option value="${n}" ${n === pageSize ? "selected" : ""}>${n}</option>`).join("")}</select></label><span data-page></span><button class="btn" type="button" data-prev>Previous</button><button class="btn" type="button" data-next>Next</button></div>
    <details class="card" data-learning-panel><summary>DSPy / GEPA learning and context routing</summary><div data-learning-status>Loading learning status…</div>
      <div data-learning-controls>
        <p><button type="button" class="btn" data-learning-mode="full"${readOnly ? " disabled" : ""}>Enable full learning</button>
        <button type="button" class="btn" data-learning-mode="capture"${readOnly ? " disabled" : ""}>Pause optimization</button>
        <button type="button" class="btn" data-learning-mode="defaults"${readOnly ? " disabled" : ""}>Use environment defaults</button></p>
        <p><a class="btn" href="/api/learning/export-policy" download="applypilot-policies.json">Export policies only</a></p>
        <p><label>Policy JSON file <input type="file" data-policy-file accept=".json,application/json"${readOnly ? " disabled" : ""}></label>
        <label><input type="checkbox" data-policy-replace${readOnly ? " disabled" : ""}> Replace conflicting task policies</label>
        <button type="button" class="btn" data-policy-import disabled>Import trusted policies</button></p>
        <p>Policy-only files exclude jobs, corpus, credentials and browser state. Instructions themselves may contain private facts: keep exports private. Imports retain schema/model/routing restrictions and do not perform a new evaluation.</p>
        <p>GitHub backups use encrypted policy checkpoints and full encrypted learning snapshots. <a href="#backups">Configure or inspect backups →</a></p>
        <p data-policy-result role="status" aria-live="polite"></p>
      </div></details>
    <details class="card"><summary>Execution scheduler</summary><div data-allocation></div></details>
    <p class="workers-footnote">Only reported execution slots appear—not every configured pool slot. Awaiting a model means the call has not returned; it does not prove the provider is making progress. <a href="#operations">Inspect operations</a> · <a href="#overview?panel=engine">Inspect work buffer</a></p>
    <dialog class="worker-inspector" aria-labelledby="worker-detail-title"><header><h2 id="worker-detail-title">Worker details</h2><button class="btn" type="button" data-close>Close</button></header><div data-details></div></dialog>
  </section>`;
	const $ = (selector) => root.querySelector(selector),
		dialog = $("dialog");
	function replace(el, html) {
		if (el._markup !== html) {
			el.innerHTML = html;
			el._markup = html;
		}
	}
    async function temporalStatus(live = false) {
        const button = $("[data-temporal-refresh]");
        button.disabled = true;
        try {
            const value = await api(live ? "temporal/live" : "temporal/status");
            if (alive) $("[data-temporal-status]").textContent = JSON.stringify(value, null, 2);
        } catch (error) {
            if (alive) $("[data-temporal-status]").textContent = "Temporal observation unavailable: " + error.message;
        } finally {
            if (alive) button.disabled = false;
        }
    }
    $("[data-temporal-refresh]").addEventListener("click", () => temporalStatus(true));
    temporalStatus();
	let learningFetchedAt = 0,
		learningBusy = false;
	async function loadLearning() {
		if (!alive || Date.now() - learningFetchedAt < 15000) return;
		learningFetchedAt = Date.now();
		try {
			const value = await api("learning/status");
			if (!alive) return;
			const cfg = value.settings || {},
				rows = value.corpus || [],
				rounds = value.rounds || [];
			const count = rows.reduce((n, r) => n + Number(r.examples || 0), 0);
			const revisions = (value.revisions || []).filter(
				(r) => r.state === "active",
			);
			const state = cfg.enabled
				? "Automatic optimization enabled"
				: "Capture only — optimization disabled";
			const scheduler = value.scheduler || {};
			const schedulerAge =
				Number(scheduler.checked_at) > 0
					? Math.max(
							0,
							Math.floor(Date.now() / 1000 - Number(scheduler.checked_at)),
						)
					: null;

			const renderRound = (r) => {
				let report = {};
				try {
					report = JSON.parse(r.report || "{}");
				} catch {}
				const end = Number(r.finished) || Date.now() / 1000;
				const elapsed =
					Number(r.started) > 0
						? Math.max(0, Math.floor(end - Number(r.started)))
						: null;
				const detail = [
					report.reason_code,
					report.error_type,
					elapsed == null ? "" : `${elapsed}s elapsed`,
				]
					.filter(Boolean)
					.join(" · ");
				const qualityScore = (score) => score == null ? "n/a" : Number(score).toFixed(3);
				const dimensions = Object.entries(report.quality_dimensions || {})
					.map(([name, scores]) => `${name}: ${qualityScore(scores.baseline)} → ${qualityScore(scores.candidate)} (${Number(scores.paired_examples)} paired cases; assessable ${Number(scores.baseline_assessed)} → ${Number(scores.candidate_assessed)})`)
					.join("\n");
				return `<tr><td>${esc(r.task)}</td><td>${esc(report.optimizer || "gepa")}</td><td>${esc(r.state)}${detail ? `<br><small>${esc(detail)}</small>` : ""}</td><td>${esc(report.evaluation_kind || "Not yet evaluated")}</td><td>${report.baseline_holdout_score == null ? "—" : Number(report.baseline_holdout_score).toFixed(3)} → ${report.candidate_holdout_score == null ? "—" : Number(report.candidate_holdout_score).toFixed(3)}${dimensions ? `<details><summary>Quality dimensions</summary><pre>${esc(dimensions)}</pre></details>` : ""}</td><td>${esc(report.revision || r.id)}</td></tr>`;
			};
			$("[data-learning-status]").innerHTML =
				`<p><strong>${esc(state)}</strong>. ${count} unconsumed examples; ${revisions.length} deployed revisions. Automatic promotion ${cfg.auto_promote ? "enabled" : "disabled"}; model-judged/replay-proxy promotion ${cfg.allow_judged_promotion ? "enabled (can be wrong)" : "disabled"}.</p>
        <p><strong>Scheduler:</strong> ${esc(scheduler.state || "not observed")}${scheduler.reason_code ? ` · ${esc(scheduler.reason_code)}` : ""}${schedulerAge == null ? "" : ` · last scheduler update ${schedulerAge}s ago`}. Enabled is a setting, not proof of execution; a scheduler update is not model progress.</p>
        <p>RLM context routing: <strong>${value.inference?.rlm_enabled ? "enabled" : "disabled by configuration"}</strong>. DSPy-enabled tasks: ${Number(value.inference?.dspy_enabled_tasks?.length || 0)}.</p>
        <p>GEPA is the default optimizer. ${value.optimizers?.challenger_enabled ? `MIPROv2 alternates with GEPA for ${esc((value.optimizers.challenger_tasks || []).join(", "))}, challenging the active policy under the same round limits.` : "The MIPROv2 challenger is disabled."}</p>
        <p>Behavior improvements are evaluated, not guaranteed. Browser replay measures the next action, not live submission success. Unknown or browser-reported completions are not verified-success labels. Completed episodes with guarded proof: ${Number(value.episodes?.verified || 0)}.</p>
        <table><caption>Recent optimization rounds</caption><thead><tr><th>Task</th><th>Optimizer</th><th>State</th><th>Evaluation basis</th><th>Holdout baseline → candidate</th><th>Revision / trace</th></tr></thead><tbody>${rounds.length ? rounds.map(renderRound).join("") : '<tr><td colspan="6">No rounds yet. A round needs compatible data in separate training, validation and fresh holdout job groups.</td></tr>'}</tbody></table>
        <p>Full learning is the default: optimization plus automatic promotion, including judged/proxy evidence. Private captured evidence reaches the configured model gateway and adds model usage. Autopilot supervises the learner; no separate enable/watch command is needed. Saved owner overrides: ${esc((value.saved_override_keys || []).join(", ") || "none")}.</p>
        <details><summary>Per-task corpus, deployed revisions and observed outcomes</summary><pre>${esc(JSON.stringify({ corpus: rows, active_revisions: revisions, episode_outcomes: value.episodes?.by_revision || [] }, null, 2))}</pre></details>`;
		} catch (e) {
			if (alive)
				$("[data-learning-status]").textContent =
					"Learning status unavailable: " + String(e.message || e);
		}
	}
	loadLearning();
	function accept(value) {
		savedData = value;
		error = "";
		render();
		loadLearning();
	}
	function currentData() {
		if (!liveStatus) return savedData;
		const live = statusView(liveStatus),
			same = savedData?.runtime_id === liveStatus.runtime_id;
		const previous = new Map(
			(same ? savedData.rows || [] : []).map((row) => [row.key, row]),
		);
		const rows = live.workers.map((row) => {
			const old = previous.get(row.key);
			return old?.trace_id && old.trace_id === row.trace_id
				? { ...old, ...row }
				: row;
		});
		const reported = new Set(rows.map((row) => row.key));
		for (const row of previous.values())
			if (!reported.has(row.key))
				rows.push({
					...row,
					fresh: false,
					active: false,
					state: row.state === "stopped" ? "stopped" : "unverified",
					reason: "Retained diagnostic sample; no current producer report.",
				});
		const applications = live.workers.filter(
			(row) => row.kind === "application",
		);
		return {
			at: new Date(live.at).toISOString(),
			runtime_id: liveStatus.runtime_id,
			run_id: live.runtime.payload.operations_run_id,
			runtime_at: new Date(live.at).toISOString(),
			runtime_current: live.current,
			rows,
			warnings: live.execution.current
				? live.execution.payload.warnings || []
				: [],
			resources: {
				stages: live.stages,
				execution: live.execution.current ? live.execution.payload : {},
				preparation: {
					...live.preparation.payload,
					available: live.preparation.current,
				},
				application: {
					...live.applications.payload,
					selection_fresh: live.applications.selectionCurrent,
					reported_worker_slots: live.runtime.current
						? applications.length
						: null,
					active_tasks: live.execution.current
						? (live.execution.payload.stages?.applications?.active_tasks ?? 0)
						: live.runtime.current
							? applications.filter((row) => row.active).length
							: null,
					reported_applied: live.runtime.current
						? applications.reduce(
								(total, row) => total + (row.reported_applied || 0),
								0,
							)
						: null,
					reported_failed: live.runtime.current
						? applications.reduce(
								(total, row) => total + (row.reported_failed || 0),
								0,
							)
						: null,
				},
			},
		};
	}
	function report() {
		return {
			...data,
			exported_at: new Date().toISOString(),
			rows: effectiveRows(data),
			scope: "All reported workers in this sample, not only the filtered page",
			filters: { ...filters },
			page_size: pageSize,
		};
	}
	async function exportBundle() {
		if (bundleBusy || !data) return;
		const button = $("[data-bundle]");
		bundleBusy = true;
		button.disabled = true;
		button.textContent = "Collecting traces…";
		try {
			const workers = report(),
				runId = workers.run_id || "";
			button.textContent = "Collecting run…";
			const work = await retainedWorkRun(api, runId);
			button.textContent = "Collecting traces…";
			const traces = await applicationTraceBundle(
				api,
				workers.rows,
				work?.application_trace_links?.links || [],
			);
			const supportContext =
				work?.support_context ||
				(await api("operations/support-context").catch((error) => ({
					available: false,
					source: "operations/support-context",
					reason:
						error.message ||
						"Saved setup and backup diagnostics could not be read.",
				})));
			const bundle = {
				format: "applypilot_run_debug_bundle",
				format_version: 1,
				exported_at: new Date().toISOString(),
				run_id: runId || null,
				privacy:
					"Private diagnostic export. May contain applicant details and page observations. Keep it local unless intentionally sharing it.",
				scope:
					"Retained selected-run evidence plus application traces linked by persisted episodes or the worker report.",
				workers,
				work,
				support_context: supportContext,
				learning_status: await api("learning/status").catch(() => ({
					available: false,
				})),
				application_traces: traces,
				trace_notes: {
					sources: {
						agent: "Claude Code lifecycle plus named tool requests/results",
						aleph: "Historical Aleph diagnostics",
						browser: "BrowserOS bridge and adapter diagnostics",
						learning:
							"Observation processing, recursive analysis, DSPy routing, captured examples and GEPA events",
						rlm: "RLM inputs, cell code, stdout/stderr, model subcalls, wait stacks, validation and container lifecycle; credentials redacted, large payloads explicitly truncated",
					},
					rotation:
						"All retained .2, .1 and current trace segments were requested; absent segments are omitted.",
				},
			};
			const stamp = new Date().toISOString().replace(/[:.]/g, "-");
			saveFile(
				bundle,
				`applypilot-run-debug-${(runId || "current").slice(0, 16)}-${stamp}.json`,
			);
			notify(
				`Run debug export saved with ${traces.length} application trace${traces.length === 1 ? "" : "s"}.`,
			);
		} catch (error) {
			notify(error.message, true);
		} finally {
			bundleBusy = false;
			button.textContent = "Export run + traces";
			button.disabled = !data;
		}
	}
	function render() {
		if (!alive) return;
		data = currentData();
		const rows = effectiveRows(data),
			prep = data?.resources?.preparation || {},
			apps = data?.resources?.application || {},
			execution = data?.resources?.execution || {};
		const age = Date.now() / 1000 - stamp(data?.runtime_at);
		const current =
			data?.runtime_current === true &&
			Number.isFinite(age) &&
			age >= -5 &&
			age <= 15;
		const active = current ? execution.active_tasks : null;
		replace(
			$("[data-health]"),
			runtimeHealth({
				current,
				execution,
				active,
				queued: execution.queued_tasks,
				waitingProvider: rows.filter(
					(row) => row.active && row.fresh && row.state === "waiting_provider",
				).length,
				error: liveStatus && current ? "" : error,
				warnings: data?.warnings || [],
				paused: updatesPaused(),
			}),
		);
		const metric = (name, value, note = "") =>
			`<div><span>${name}</span><strong>${value}</strong>${note ? `<small>${esc(note)}</small>` : ""}</div>`;
		replace(
			$("[data-metrics]"),
			[
				metric(
					"Tasks executing",
					number(current ? execution.active_tasks : null),
					"Actual scheduler execution",
				),
				metric(
					"Tasks queued",
					number(current ? execution.queued_tasks : null),
					"Waiting for dependencies or a worker",
				),
				metric(
					"Application workers busy",
					current
						? `${number(apps.active_tasks)} / ${number(apps.effective_workers)}`
						: "—",
					"Executing applications / configured application capacity",
				),
				metric(
					"Shared worker limit",
					current ? number(execution.configured_workers ?? execution.workers) : "—",
					"Configured ceiling for pipeline and supporting work",
				),
				metric(
					"Context inference sessions",
					current
						? `${number(execution.rlm_active_slots)} / ${number(execution.capacity?.rlm_limit)}`
						: "—",
					"Shared across this workspace; waiting tasks resume as capacity opens",
				),
			].join(""),
		);
		const stagePanel = $("[data-stages]"),
			expanded = stagePanel.querySelector("details")?.open;
		replace(stagePanel, stageQueues(data?.resources?.stages || prep.stages));
		if (expanded && stagePanel.querySelector("details"))
			stagePanel.querySelector("details").open = true;
		replace(
			$("[data-allocation]"),
			`<dl class="workers-capacity-details">${[
				["Dask workers", number(execution.dask_workers)],
				[
					"Shared execution threads",
					number(execution.capacity?.thread_limit || execution.workers),
				],
				["Executing threads", number(execution.executing_threads)],
				[
					"Seceded coordinators/resource waits",
					number(execution.seceded_tasks),
				],
				["Native scheduler queued", number(execution.scheduler_queued)],
				["Native no-worker tasks", number(execution.scheduler_no_worker)],
				["Available task admission slots", number(execution.admission_available_slots)],
				["PostgreSQL running connection limit", number(execution.database_capacity?.running_max_connections)],
				["PostgreSQL configured connection limit", number(execution.database_capacity?.configured_max_connections)],
				["Database connection budget", number(execution.database_capacity?.server_capacity)],
				["Inference slots in use", number(execution.rlm_active_slots)],
				["Inference slots available", number(execution.rlm_available_slots)],
				["Configured browser slots", number(execution.browser_workers)],
				["Available browser slots", number(execution.browser_available_slots)],
				["Preparation task checks finished", number(prep.completed)],
				["Worker-reported submissions", number(apps.reported_applied)],
				["Worker-reported failures", number(apps.reported_failed)],
			]
				.map(
					([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`,
				)
				.join(
					"",
				)}</dl><p>Ordinary stages use equal explicit Dask priority. Dependencies, runnable state and real resource capacity determine execution; saved application records determine business outcomes.</p>${schedulerLink(execution)}`,
		);
		const bottlenecks = [];
		if (current) {
			if (execution.database_capacity?.restart_required)
				bottlenecks.push("PostgreSQL has a capacity change waiting for an orderly restart; the running connection limit is still in effect.");
			const idle = rows.filter(
				(r) =>
					r.kind === "application" &&
					r.fresh &&
					r.phase === "no_selectable_job",
			);
			if (idle.length)
				bottlenecks.push(
					`${idle.length} application worker(s) have no selectable job. Open an idle worker for its selection blockers.`,
				);
			const stages = new Map();
			for (const row of rows) {
				if (row.kind !== "preparation" || !row.fresh) continue;
				const total = stages.get(row.stage) || {
					completed_operations: 0,
					failed_operations: 0,
					partial_operations: 0,
				};
				total.completed_operations += row.completed_operations || 0;
				total.failed_operations += row.failed_operations || 0;
				total.partial_operations =
					typeof row.partial_operations === "number" &&
					total.partial_operations !== null
						? total.partial_operations + row.partial_operations
						: null;
				stages.set(row.stage, total);
			}
			for (const [key, total] of stages)
				if (total.failed_operations > total.completed_operations)
					bottlenecks.push(
						`${names[key] || key}: ${operationSummary(total)} operations in reported workers.`,
					);
		}
		$("[data-bottlenecks]").hidden = !bottlenecks.length;
		$("[data-bottlenecks]").textContent = bottlenecks.join(" ");
		const selected = filterRows(rows, filters);
		const totalPages = Math.max(1, Math.ceil(selected.length / pageSize));
		page = Math.min(page, totalPages - 1);
		const shown = selected.slice(page * pageSize, (page + 1) * pageSize);
		$("[data-caption]").textContent =
			`${selected.length} matching execution slots${data?.truncated ? " (report truncated)" : ""} · sample ${data ? new Date(data.at).toLocaleTimeString() : "not received"} · select a worker for evidence`;
		// Keep the modal snapshot and its opener stable. Outside the modal, retain
		// keyboard focus by worker key while still expiring stale row badges.
		const focusedKey = $("[data-rows]").contains(document.activeElement)
			? document.activeElement.dataset.worker
			: null;
		if (!dialog.open)
			replace(
				$("[data-rows]"),
				shown.length
					? shown
							.map((r) => {
								const active = r.active && r.fresh,
									task = active
										? r.job_title || phase(r)
										: r.reason || "No current task reported";
								const issue = r.last_issue
									? `<small class="worker-issue">${r.last_issue.severity === "warning" ? "Warning" : "Last issue"}: ${esc(issueText(r))}</small>`
									: "";
								return `<tr><th scope="row"><button class="worker-open" type="button" data-worker="${esc(r.key)}">${esc(label(r))}</button><small>${esc(names[r.stage] || r.stage)}</small></th><td><strong>${esc(task)}</strong>${active && r.company ? `<small>${esc(r.company)}</small>` : ""}${active ? `<small>${esc(phase(r))}${r.model_task ? " · " + esc(r.model_task) : ""}${r.model_wait_since ? ` · Model wait <span data-worker-elapsed="${esc(r.model_wait_since)}"></span>` : ""}</small>${r.form_progress_at ? `<small>Last form progress ${esc(when(r.form_progress_at))}</small>` : ""}` : ""}${issue}</td><td>${badge(r.state)}<small>${r.state === "stopped" ? "Execution ended" : r.state === "unverified" ? "Last sample " + when(r.sample_at) : `${active ? "Task" : "State"} <span data-worker-elapsed="${esc(r.started_at)}"></span>`}</small></td><td>${r.kind === "application" ? `${number(r.reported_applied)} applied / ${number(r.reported_failed)} failed<small>worker-reported this run</small>` : `${operationSummary(r)}${r.warning_events ? ` · ${number(r.warning_events)} warnings` : ""}<small>operation outcomes, not submissions</small>`}</td></tr>`;
							})
							.join("")
					: `<tr><td colspan="4" class="workers-empty">${data ? "No execution slots match these filters. Missing reports are not proof of an empty queue." : "Loading worker reports without querying job inventory…"}</td></tr>`,
			);
		// Advancing a clock must not destroy the table, selection or focused button.
		if (!dialog.open)
			for (const node of root.querySelectorAll("[data-worker-elapsed]")) {
				const value = duration(Number(node.dataset.workerElapsed));
				if (node.textContent !== value) node.textContent = value;
			}
		if (focusedKey && !dialog.open) {
			const button = [...root.querySelectorAll("[data-worker]")].find(
				(b) => b.dataset.worker === focusedKey,
			);
			if (button) button.focus({ preventScroll: true });
			else $("[data-search]").focus({ preventScroll: true });
		}
		$("[data-page]").textContent = `Page ${page + 1} of ${totalPages}`;
		$("[data-prev]").disabled = page === 0;
		$("[data-next]").disabled = page >= totalPages - 1;
		$("[data-copy]").disabled = !data;
		$("[data-export]").disabled = !data;
		$("[data-bundle]").disabled = !data || bundleBusy;
	}
	function inspect(key, button) {
		const row = effectiveRows(data).find((r) => r.key === key);
		if (!row) return;
		opener = button;
		$("[data-details]").innerHTML =
			`<p class="workers-footnote">Snapshot from ${esc(when(row.sample_at))}. Later changes do not overwrite this inspector.</p><div class="worker-inspector-title">${badge(row.state)}<strong>${esc(label(row))}</strong></div><dl>${[
				["Stage", names[row.stage] || row.stage],
				["Last completed selection check", when(row.selection_checked_at)],
				[
					"Selection polls / consecutive empty",
					row.selection_polls == null
						? "Not recorded"
						: `${number(row.selection_polls)} / ${number(row.selection_empty_polls)}`,
				],
				["Selected browser runtime", row.selected_runtime || "Not recorded"],
				["Worker activity log", "Workspace / logs / worker-activity"],
				[
					"Current task",
					row.active && row.fresh
						? row.job_title || phase(row)
						: "No current task reported",
				],
				["Wait / phase", row.reason || phase(row)],
				["Task started", when(row.started_at)],
				["Phase started", when(row.phase_at)],
				["Lifecycle checkpoint", when(row.progress_at)],
				["Last form progress", when(row.form_progress_at)],
				["Model wait began", when(row.model_wait_since)],
				["Last model activity", when(row.model_activity_at)],
				["Last browser transport", when(row.transport_at)],
				["Latest issue", row.last_issue ? issueText(row) : "None reported"],
				[
					"Issue origin",
					row.last_issue?.origin ||
						[row.last_issue?.component, row.last_issue?.operation]
							.filter(Boolean)
							.join(" / ") ||
						"Not recorded",
				],
				["Exception type", row.last_issue?.exception_type || "Not recorded"],
				["Issue recorded", when(row.last_issue?.at)],
				[
					"Model call ID",
					row.model_call_id || row.last_issue?.llm_call_id || "Not recorded",
				],
				["Model call started", when(row.model_call_started_at)],
				["Model call state", row.model_call_state || "Not recorded"],
				[
					"Local model response log",
					row.model_call_id || row.last_issue?.llm_call_id
						? "Workspace / logs / llm-responses (private, not included in this export)"
						: "No response-log reference recorded",
				],
				["Previous task", row.last_job_title || "Not reported"],
				["Previous outcome", row.last_outcome || "Not reported"],
				[
					"Reported outcomes",
					row.kind === "application"
						? `${number(row.reported_applied)} applied / ${number(row.reported_failed)} failed (worker-reported)`
						: `${operationSummary(row)}; ${number(row.warning_events || 0)} warnings`,
				],
			]
				.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
				.join(
					"",
				)}</dl>${selectionDetails(row)}<p><a class="btn" href="#operations?run_id=${encodeURIComponent(data?.run_id || "")}">Open run history</a></p><details><summary>Exact worker snapshot</summary><pre>${esc(JSON.stringify(row, null, 2))}</pre></details>`;
		if (row.trace_id) {
			trace = {
				id: row.trace_id,
				source: "agent",
				segment: 0,
				before: null,
				nextBefore: null,
			};
			const panel = document.createElement("section");
			panel.className = "card";
			panel.innerHTML = `<h3>Application trace</h3><p>Private diagnostic previews: may contain applicant or page data. Not included in workers/operations exports. Structured events omit Gmail bodies, image bytes and thinking blocks; native diagnostic text can still be sensitive. Times are UTC.</p>
        <p><label>Source <select data-trace-source><option value="agent">Claude tool calls and results</option><option value="aleph">Historical Aleph logs</option><option value="browser">Browser bridge (agent vs adapter)</option><option value="learning">Observation processing + DSPy / GEPA</option><option value="rlm">RLM internals</option></select></label>
        <label>File <select data-trace-segment><option value="0">Latest</option><option value="1">Previous rotated file</option><option value="2">Older rotated file</option></select></label></p>
        <p><button type="button" class="btn" data-trace-refresh>Latest output</button> <button type="button" class="btn" data-trace-older disabled>Earlier in file</button> <button type="button" class="btn" data-trace-copy>Copy displayed trace</button> <label><input type="checkbox" data-trace-follow checked> Follow while open</label></p>
        <p>Select RLM internals for executed code, model requests and responses, stdout/stderr, validation errors, wait stacks and container cleanup. Match rlm_id, container, cell and rlm_call_id to follow one inference. Select learning for character counts, DSPy routes, example IDs and policy versions. Global optimizer rounds and held-out scores appear in the learning panel.</p>
        <p data-trace-status role="status">Reading trace…</p><pre data-trace-body tabindex="0" style="max-height:28rem;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere"></pre>`;
			$("[data-details]").append(panel);
		} else trace = null;
		dialog.showModal();
		$("[data-close]").focus();
		if (trace) loadTrace();
	}

	function scheduleTrace() {
		clearTimeout(traceTimer);
		if (
			alive &&
			dialog.open &&
			trace &&
			!document.hidden &&
			$("[data-trace-follow]")?.checked
		)
			traceTimer = setTimeout(() => loadTrace(), 2500);
	}
	async function loadTrace() {
		clearTimeout(traceTimer);
		if (!trace || !dialog.open || !alive || document.hidden) return;
		const revision = ++traceRevision,
			selected = { ...trace };
		const params = new URLSearchParams({
			id: selected.id,
			source: selected.source,
			segment: String(selected.segment),
		});
		if (selected.before !== null) params.set("before", String(selected.before));
		try {
			const value = await api("workers/trace?" + params);
			if (!alive || !dialog.open || revision !== traceRevision) return;
			const body = $("[data-trace-body]"),
				following = $("[data-trace-follow]").checked;
			if (body.textContent !== value.text) {
				body.textContent = value.text;
				if (following) body.scrollTop = body.scrollHeight;
			}
			trace.nextBefore = value.next_before;
			$("[data-trace-older]").disabled = value.next_before === null;
			$("[data-trace-status]").textContent = value.available
				? `Trace ${selected.id} · ${selected.source} · bytes ${value.start}–${value.end} of ${value.size}. Log views and rotated retention are not model-context limits.`
				: value.text;
		} catch (e) {
			if (alive && dialog.open && revision === traceRevision)
				$("[data-trace-status]").textContent =
					"Trace unavailable: " + String(e.message || e);
		} finally {
			if (revision === traceRevision) scheduleTrace();
		}
	}
	function closed() {
		clearTimeout(traceTimer);
		++traceRevision;
		trace = null;
		if (opener?.isConnected) opener.focus({ preventScroll: true });
		else $("[data-search]").focus({ preventScroll: true });
		opener = null;
	}
	function schedule() {
		clearTimeout(timer);
		if (!alive || document.hidden || updatesPaused()) return;
		timer = setTimeout(refresh, 15000);
	}
	async function refresh(force = false) {
		clearTimeout(timer);
		if (!alive || busy || document.hidden || (!force && updatesPaused()))
			return;
		const gap = Date.now() - lastAttempt;
		if (!force && gap < 1500) {
			timer = setTimeout(() => refresh(), 1500 - gap);
			return;
		}
		busy = true;
		lastAttempt = Date.now();
		try {
			const value = await api("workers");
			if (alive) accept(value);
		} catch (e) {
			if (alive) {
				error =
					"Workers could not update. Previous samples expire rather than stay falsely live. " +
					String(e.message || "");
				render();
			}
		} finally {
			busy = false;
			schedule();
		}
	}
	async function learningAction(button) {
		if (readOnly || learningBusy) return;
		learningBusy = true;
		const controls = $("[data-learning-controls]");
		for (const field of controls.querySelectorAll("button,input"))
			field.disabled = true;
		try {
			let result;
			if (button.dataset.learningMode) {
				const mode = button.dataset.learningMode;
				const message =
					mode === "capture"
						? "Pause optimization? Local evidence capture and existing deployed policies remain active. The learner cancels its current round safely."
						: "Use " +
							(mode === "full"
								? "full automatic learning"
								: "the configured environment defaults") +
							"? This may send private captured evidence to your configured model gateway, add model usage and deploy model-judged/next-action-proxy candidates. These evaluations can be wrong.";
				if (
					!(await confirmAction(message, {
						title: "Learning policy",
						accept: "Apply setting",
					}))
				)
					return;
				result = await api("learning/settings", {
					method: "POST",
					body: { mode, confirm: true },
				});
				if (alive)
					$("[data-policy-result]").textContent =
						"Learning settings saved. Autopilot supervises the learner; current rounds stop when optimization is paused.";
			} else {
				const file = $("[data-policy-file]").files[0];
				if (!file) throw new Error("Select a policy JSON file.");
				if (file.size > 5000000)
					throw new Error("Policy JSON must be no larger than 5 MB.");
				let bundle;
				try {
					bundle = JSON.parse(await file.text());
				} catch {
					throw new Error("The file is not valid JSON.");
				}
				if (
					!(await confirmAction(
						"Trust and import these instructions and recorded deployment choices? They may change application behavior. This is not a new evaluation. Jobs, captured examples and credentials will not be replaced.",
						{ title: "Import policies only", accept: "Trust and import" },
					))
				)
					return;
				result = await api("learning/import-policy", {
					method: "POST",
					body: {
						bundle,
						trust: true,
						replace: $("[data-policy-replace]").checked,
					},
				});
				if (alive)
					$("[data-policy-result]").textContent =
						`Imported ${result.imported} revisions and ${result.artifacts} legacy policies; ${result.active} marked active. Compatibility checks still apply.`;
			}
			learningFetchedAt = 0;
			await loadLearning();
		} catch (error) {
			if (alive) {
				$("[data-policy-result]").textContent = error.message;
				notify(error.message, true);
			}
		} finally {
			learningBusy = false;
			if (alive) {
				for (const field of controls.querySelectorAll("button,input"))
					field.disabled = readOnly;
				$("[data-policy-import]").disabled =
					readOnly || !$("[data-policy-file]").files.length;
			}
		}
	}
	async function click(e) {
		const b = e.target.closest("button");
		if (!b) return;
		if (
			b.hasAttribute("data-learning-mode") ||
			b.hasAttribute("data-policy-import")
		) {
			await learningAction(b);
			return;
		}
		if (b.hasAttribute("data-prev")) {
			page = Math.max(0, page - 1);
			render();
		}
		if (b.hasAttribute("data-next")) {
			page++;
			render();
		}
		if (b.dataset.worker) inspect(b.dataset.worker, b);
		if (b.hasAttribute("data-close")) dialog.close();
		if (b.hasAttribute("data-trace-refresh") && trace) {
			trace.before = null;
			trace.segment = 0;
			$("[data-trace-segment]").value = "0";
			loadTrace();
		}
		if (
			b.hasAttribute("data-trace-older") &&
			trace &&
			trace.nextBefore !== null
		) {
			$("[data-trace-follow]").checked = false;
			trace.before = trace.nextBefore;
			loadTrace();
		}
		if (b.hasAttribute("data-trace-copy")) {
			try {
				await copy($("[data-trace-body]").textContent);
				notify("Displayed private trace copied. Redact before sharing.");
			} catch (e) {
				notify(e.message, true);
			}
		}
		try {
			if (b.hasAttribute("data-copy") && data) {
				await copy(JSON.stringify(report(), null, 2));
				notify("Worker report copied. It includes all reported slots.");
			}
			if (b.hasAttribute("data-export") && data) saveFile(report());
			if (b.hasAttribute("data-bundle") && data) await exportBundle();
		} catch (e) {
			notify(e.message, true);
		}
	}
	function change(e) {
		if (e.target.matches("[data-policy-file]")) {
			$("[data-policy-import]").disabled =
				readOnly || learningBusy || !e.target.files.length;
			return;
		}
		if (e.target.matches("[data-policy-replace]")) return;
		if (
			trace &&
			e.target.matches(
				"[data-trace-source],[data-trace-segment],[data-trace-follow]",
			)
		) {
			trace.source = $("[data-trace-source]").value;
			trace.segment = Number($("[data-trace-segment]").value);
			trace.before = null;
			if (trace.segment) $("[data-trace-follow]").checked = false;
			loadTrace();
			return;
		}
		if (e.target.matches("[data-kind]")) filters.kind = e.target.value;
		if (e.target.matches("[data-state]")) filters.state = e.target.value;
		if (e.target.matches("[data-size]")) {
			pageSize = Number(e.target.value);
			try {
				localStorage.setItem("ap-workers-page-size", String(pageSize));
			} catch {}
		}
		page = 0;
		render();
	}
	function input(e) {
		if (e.target.matches("[data-search]")) {
			filters.q = e.target.value;
			page = 0;
			render();
		}
	}
	function visibility() {
		clearTimeout(timer);
		clearTimeout(traceTimer);
		if (!document.hidden) {
			scheduleTrace();
			refresh();
		}
	}
	function invalidation() {
		refresh();
	}
	root.addEventListener("click", click);
	root.addEventListener("change", change);
	root.addEventListener("input", input);
	dialog.addEventListener("close", closed);
	document.addEventListener("visibilitychange", visibility);
	window.addEventListener("applypilot:invalidate", invalidation);
	const tick = setInterval(() => {
		if (!document.hidden && !updatesPaused()) render();
	}, 1000);
	render();
	refresh();
	return {
		refresh,
		updateStatus(value) {
			liveStatus = value;
			if (alive) render();
		},
		dispose() {
			alive = false;
			clearTimeout(traceTimer);
			++traceRevision;
			clearTimeout(timer);
			clearInterval(tick);
			if (dialog.open) dialog.close();
			root.removeEventListener("click", click);
			root.removeEventListener("change", change);
			root.removeEventListener("input", input);
			dialog.removeEventListener("close", closed);
			document.removeEventListener("visibilitychange", visibility);
			window.removeEventListener("applypilot:invalidate", invalidation);
		},
	};
}
