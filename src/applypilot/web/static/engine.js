import { statusView } from "./live-status.js";
import { recoveryView } from "./recovery-view.js";
import { STAGE_CATALOG } from "./stage_catalog.js";
import { updatesPaused } from "./ux.js";

/* Read-only engine visibility. No account checks, mutations or browser launches. */
const esc = (value) =>
	String(value ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const number = (value) =>
	typeof value === "number" && Number.isFinite(value)
		? value.toLocaleString()
		: "—";
const words = (value) => String(value || "Not reported").replaceAll("_", " ");
function stamp(value) {
	if (typeof value === "number") return value * 1000;
	if (!value) return NaN;
	return Date.parse(
		/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : value.replace(" ", "T") + "Z",
	);
}
const date = (value) =>
	Number.isFinite(stamp(value))
		? new Date(stamp(value)).toLocaleString(undefined, {
				month: "short",
				day: "numeric",
				hour: "2-digit",
				minute: "2-digit",
				second: "2-digit",
			})
		: "Not recorded";
const elapsed = (value) => {
	const seconds = Math.max(0, Math.floor((Date.now() - stamp(value)) / 1000));
	if (!Number.isFinite(seconds)) return "Not reported";
	return seconds < 60
		? `${seconds}s`
		: seconds < 3600
			? `${Math.floor(seconds / 60)}m ${seconds % 60}s`
			: `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
};
const SERVICES = STAGE_CATALOG.filter((row) =>
	[
		"company_research",
		"tracking",
		"referrals",
		"outreach",
		"backup",
		"unattended",
	].includes(row.id),
).map((row) => [row.id, row.label]);
const NAMES = Object.fromEntries(
	STAGE_CATALOG.map((row) => [row.id, row.label]),
);
const metric = (title, value, foot) =>
	`<div class="engine-metric"><span>${esc(title)}</span><strong>${number(value)}</strong><small>${foot}</small></div>`;
const pill = (label, tone = "quiet") =>
	`<span class="engine-state ${tone}">${esc(label)}</span>`;
function replace(el, markup) {
	if (!el || el._engineMarkup === markup) return;
	const selected = el.contains(document.activeElement)
		? document.activeElement?.dataset?.engineJob
		: null;
	el.innerHTML = markup;
	el._engineMarkup = markup;
	if (selected)
		[...el.querySelectorAll("[data-engine-job]")]
			.find((b) => b.dataset.engineJob === selected)
			?.focus({ preventScroll: true });
}
function jobLink(job, fallback = "Job title not available") {
	return job?.url
		? `<button class="job-link" data-engine-job="${esc(job.url)}">${esc(job.title || fallback)}</button>`
		: `<strong>${esc(job?.title || fallback)}</strong>`;
}

export function serviceReason(service) {
	if (!service || service.state === "unknown")
		return "Awaiting the first service report.";
	if (typeof service.reason === "string" && service.reason.trim())
		return service.reason;
	if (service.state === "running") return "Processing eligible work.";
	if (service.state === "checking")
		return "Checking this service’s eligible queue.";
	return "Service report received; no additional status detail.";
}
export function serviceCapacity(service) {
	if (service?.active_tasks != null)
		return `${number(service.active_tasks)} executing · ${number(service.queued_tasks ?? service.pending)} queued`;
	return service?.workers != null
		? `${number(service.workers)} reported workers`
		: "";
}

export function mountEngineOverview(
	root,
	{ api, onOpenJob, stageSummaryRoot = null } = {},
) {
	let alive = true,
		busy = false,
		timer = null,
		data = null,
		runtime = null,
		lastRuntime = null,
		queue = null,
		settings = null,
		error = "",
		liveStatus = null,
		liveView = null;
	root.innerHTML = `<section class="engine-overview" aria-label="Autopilot engine overview">
    <div class="engine-section-head"><div><h2>Engine overview</h2><p>Stage activity, queued work and blockers.</p></div><span data-engine-freshness class="fine-print" role="status">Loading engine report…</span></div>
    <p data-engine-error class="notice notice-warning" role="status" hidden></p>
    <div data-engine-metrics class="engine-metrics"></div>
    <section data-recovery class="card work-support"></section>
    <div class="engine-main-grid">
      <section class="card engine-stage-card"><div class="card-head"><div><h2>All concurrent stages & services</h2><p data-engine-cycle>Checking the current run…</p></div><a class="btn btn-sm" href="#operations">Inspect operations →</a></div>
      <div class="table-scroll"><table class="engine-stages"><caption class="sr-only">Stage status, observed in-flight units, eligible queue and retained outcomes since this runtime started</caption><thead><tr><th>Stage / state</th><th>In flight</th><th>Queue</th><th>Succeeded / issues<br><small>This runtime</small></th></tr></thead><tbody data-engine-stages></tbody></table></div>
      <p class="card-foot" data-engine-stage-note></p></section>
      <div class="engine-live-column"><section class="card engine-working-card"><div class="card-head"><div><h2>Working on now</h2><p>Current enrichment, scoring and other recorded work</p></div><span data-engine-active-count class="badge">—</span></div><div data-engine-current class="engine-current"></div></section>
    <section class="card engine-recent-card"><div class="card-head"><div><h2>Latest enriched & scored jobs</h2><p>Latest saved per-job timestamps, not imported state-transition timestamps</p></div><a class="btn btn-sm" href="#activity">Activity history →</a></div><div data-engine-recent class="engine-recent-grid"></div></section>
      </div>
    </div>
    <div id="queue-control"></div>
    <section class="card engine-services"><div class="card-head"><div><h2>Application workers & service detail</h2><p>Application workers, research, tracking, outreach and maintenance</p></div></div><div data-engine-workers class="engine-worker-strip"></div><div data-engine-services class="engine-service-grid"></div></section>
    <section class="card engine-source-card"><div class="card-head"><div><h2>Job sources</h2><p data-engine-source-note>Current discovery checkpoints and last recorded provider results</p></div><a class="btn btn-sm" href="#sources">Sources & workers →</a></div>
      <div data-engine-search class="engine-search"></div><div data-engine-source-now class="engine-source-now"></div>
      <div class="table-scroll"><table class="engine-sources"><caption class="sr-only">Task leases and latest saved health by job source; leases are not a health check</caption><thead><tr><th>Source</th><th>Leased / pending</th><th>Done / blocked</th><th>Last recorded result</th><th>Last checked</th></tr></thead><tbody data-engine-sources></tbody></table></div></section>

  </section>`;
	const refs = Object.fromEntries(
		[
			...root.querySelectorAll(
				"[data-engine-metrics],[data-engine-freshness],[data-engine-error],[data-engine-cycle],[data-engine-stages],[data-engine-stage-note],[data-engine-current],[data-engine-active-count],[data-engine-workers],[data-engine-services],[data-engine-search],[data-engine-source-note],[data-engine-source-now],[data-engine-sources],[data-engine-recent]",
			),
		].map((el) => [
			Object.keys(el.dataset).find((k) => k.startsWith("engine")),
			el,
		]),
	);
	const freshRuntime = () =>
		runtime?.running === true &&
		runtime?.stale === false &&
		Date.now() - stamp(runtime.at) < 16000 &&
		Date.now() - stamp(runtime.at) > -5000;
	const live = () =>
		!error &&
		freshRuntime() &&
		data?.journal?.current === true &&
		!data.journal.retained &&
		data.runtime_id === runtime.runtime_id &&
		data.journal.since === runtime.started_at &&
		Date.now() - stamp(data.journal.sample_at || data.at) < 15000 &&
		Date.now() - stamp(data.journal.sample_at || data.at) > -5000;
	const freshQueue = () =>
		queue?.available === true &&
		!queue.retained &&
		Date.now() - stamp(queue.at) < 45000 &&
		Date.now() - stamp(queue.at) > -5000;
	const sameRuntime = () =>
		data?.runtime_id ===
			(runtime?.runtime_id || lastRuntime?.runtime_id || data?.runtime_id) &&
		(!runtime?.started_at ||
			!data?.journal?.since ||
			data.journal.since === runtime.started_at);
	const stage = (key) => {
		if (liveView) {
			const row = liveView.stages[key];
			if (!row) return null;
			const saved = sameRuntime() ? data?.journal?.stages?.[key] : null;
			return {
				...saved,
				active: row.historical ? null : row.active_tasks,
				historical: row.historical,
				workerOnly: !saved,
				unit: "tasks",
			};
		}
		if (live()) return data.journal.stages?.[key];
		if (sameRuntime() && data?.journal?.stages?.[key])
			return { ...data.journal.stages[key], historical: true };
		if (key === "applications" && freshRuntime())
			return {
				active: (runtime.workers || []).filter((w) => w.status === "applying")
					.length,
				unit: "attempts",
				workerOnly: true,
			};
		return null;
	};
	function serviceFor(key) {
		if (liveView) return liveView.stages[key] || null;
		if (!sameRuntime()) return null; // Never transfer prior-run activity to a new run.
		const row = data?.scheduler?.stages?.[key];
		if (!row || row.state === "unknown") return null;
		const at = row.sample_at || data.scheduler.sample_at || data.at;
		const age = Date.now() - stamp(at);
		const old =
			error ||
			!freshRuntime() ||
			data.scheduler.retained ||
			row.retained ||
			!Number.isFinite(age) ||
			age < -5000 ||
			age >= 15000;
		return old ? { ...row, historical: true, sample_at: at } : row;
	}
	function queueFor(row) {
		const service = serviceFor(row.id);
		const age =
			Date.now() - stamp(service?.queue_checked_at ?? service?.checked_at);
		// Retain known stage counts with THEIR original timestamp. A temporary
		// refresh failure is not an empty queue; never substitute raw inventory.
		if (
			row.queue_key &&
			typeof service?.pending === "number" &&
			Number.isFinite(age) &&
			age >= -5000
		)
			return service.pending;
		return null;
	}
	function queueNote(row) {
		const service = serviceFor(row.id),
			at = service?.queue_checked_at;
		if (!at) return "";
		const age = Math.max(0, Math.floor((Date.now() - stamp(at)) / 1000));
		return `<small>${age > 15 ? "Retained sample · " : ""}${esc(date(at))}</small>`;
	}
	function stateFor(key, value) {
		const service = serviceFor(key);
		if (service?.state === "disabled") return pill("Disabled");
		if (service?.state === "not_selected") return pill("Not selected");
		if (!service && (!value || value.historical))
			return pill(
				error ? "Unavailable" : data ? "Awaiting first report" : "Loading…",
				error ? "waiting" : "quiet",
			);
		if (value?.active > 0 && !value.historical)
			return pill("Running", "running");
		if (
			key === "outreach" &&
			(settings?.env_enabled === false || settings?.enabled === false)
		)
			return pill("Off", "waiting");
		if (key === "outreach" && settings?.daily_send_limit === 0)
			return pill("Sending paused", "waiting");

		if (service?.state === "error") return pill("Worker error", "waiting");
		if (service?.state === "stopped") return pill("Stopped");
		if (service?.state === "checking")
			return pill("Checking eligible queue", "waiting");
		if (service?.state === "starting") return pill("Starting", "waiting");
		if (service?.state === "running") return pill("Service active", "running");
		if (service?.state === "queued") return pill("Queued", "waiting");
		if (service?.state === "scheduled")
			return pill("Waiting for next check", "waiting");
		if (service?.state === "paused")
			return pill("Configured budget reached", "waiting");
		if (service?.state === "no_progress")
			return pill("Waiting / last pass empty", "waiting");
		if (service?.state === "yielded") return pill("Requested pass limit");
		if (service?.state === "drained") return pill("Current work drained");
		if (service?.flow_mode === "repair")
			return pill("Focused evidence repair", "waiting");
		if (service?.flow_mode === "probe")
			return pill("Bounded replenishment probe", "waiting");
		if (service?.state === "waiting")
			return pill("Waiting for eligible work", "waiting");
		if (service?.state === "cooldown")
			return pill("Discovery refresh wait", "waiting");
		if (service?.state === "inventory_wait")
			return pill("Inventory unavailable", "waiting");
		if (service?.state === "capacity_wait")
			return pill("Waiting for capacity", "waiting");
		if (service?.state === "buffer_wait")
			return pill("Own output buffer stocked", "waiting");
		if (service?.state === "background_wait")
			return pill("Background budget wait", "waiting");
		if (service?.thread_alive === false)
			return pill("Worker stopped", "waiting");
		if (value?.stage_open) return pill("Stage open", "waiting");
		return pill("No reported work");
	}

	let ap4PaintKey = null;
	let renderedRuntime = null;

	function ap4StableRow(key, html, service, value, active, pending, hasQueue) {
		const previous = root.querySelector(
			'[data-engine-stage="' + CSS.escape(String(key)) + '"]',
		);
		if (!previous) return html;

		const holder = document.createElement("tbody");
		holder.innerHTML = html;
		const next = holder.firstElementChild;
		if (!next || next.cells.length !== previous.cells.length) {
			return previous.outerHTML;
		}

		// Keep queue and outcome samples, but never retain expired live activity.
		if (!Number.isFinite(active) && service && !service.historical) {
			next.cells[1].replaceWith(previous.cells[1].cloneNode(true));
		}
		if (hasQueue && !Number.isFinite(pending)) {
			next.cells[2].replaceWith(previous.cells[2].cloneNode(true));
		}
		if (!value || value.historical || value.workerOnly) {
			next.cells[3].replaceWith(previous.cells[3].cloneNode(true));
		}
		return next.outerHTML;
	}

	function renderLive() {
		if (!alive || !root.isConnected) return;
		liveView = liveStatus ? statusView(liveStatus) : null;
		const current = liveView ? liveView.current : live(),
			report = freshRuntime() ? runtime : lastRuntime,
			p = report?.engine || report?.pipeline || {};
		const paintKey = JSON.stringify([
			Object.values(liveStatus?.components || {}).map((record) => [
				record.component,
				record.revision,
				record.updated_at,
			]),
			liveView && [
				liveView.runtime.current,
				liveView.execution.current,
				liveView.preparation.current,
				liveView.applications.current,
			],
			data?.runtime_id,
			data?.at,
			runtime?.at,
			runtime?.runtime_id,
			data?.scheduler?.sample_at,
			data?.journal?.sample_at,
			queue?.at,
			current,
			freshRuntime(),
			error,
			settings?.env_enabled,
			settings?.enabled,
			settings?.daily_send_limit,
		]);
		if (paintKey === ap4PaintKey) return;
		ap4PaintKey = paintKey;

		const runtimeId = runtime?.runtime_id || data?.runtime_id;
		if (renderedRuntime && runtimeId && renderedRuntime !== runtimeId) {
			refs.engineStages.replaceChildren();
			refs.engineStages._engineMarkup = null;
		}
		renderedRuntime = runtimeId;
		// Expired evidence must actively remove live claims; keep saved totals in
		// renderSaved rather than freezing old Running labels and job cards.
		refs.engineFreshness.textContent = liveView
			? `Status sample · ${date(new Date(liveView.at).toISOString())}`
			: data
				? `Engine sample · ${date(data.at)}`
				: error
					? "Engine report unavailable"
					: "Waiting for engine data…";
		const notes = [error, ...(data?.warnings || [])].filter(Boolean);
		if (data && !current)
			notes.push(
				"Updates delayed or engine stopped. Last successful values are retained with their timestamps; old activity is not shown as live.",
			);
		if (freshRuntime()) {
			const run = data?.current_run;
			if (
				run?.available &&
				run.run_id === runtime.operations_run_id &&
				run.submitted_attempts === 0 &&
				(run.cancellations > 0 ||
					run.verification_holds > 0 ||
					Date.now() - Date.parse(runtime.started_at) >= 600000)
			)
				notes.push(
					`No saved submissions for this run as of ${date(run.captured_at)}; ${number(run.verification_holds)} verification holds and ${number(run.cancellations)} cancellations. Open Work for the recorded reasons.`,
				);
		}
		if (
			data?.journal?.available &&
			(data.journal.dropped || 0) + (data.journal.write_errors || 0) > 0
		)
			notes.push(
				`Recorder reports ${number(data.journal.dropped)} dropped events and ${number(data.journal.write_errors)} write errors. Counts may be incomplete.`,
			);
		const stale = (data?.journal?.processes || []).filter(
			(row) => row.state === "stale",
		).length;
		if (data?.journal?.truncated)
			notes.push(
				"Active job cards sample up to 200 operations across stages; stage counts aggregate all fresh operations.",
			);
		if (stale)
			notes.push(
				`${stale} recorder process(es) are stale; their old operations are not shown as live.`,
			);
		refs.engineError.hidden = !notes.length;
		refs.engineError.textContent = notes.join(" ");
		refs.engineCycle.textContent = freshRuntime()
			? `Concurrent independent services · ${words(p.pipeline_state)} · runtime started ${date(runtime.started_at)}`
			: data
				? "Last received report. Current activity is unverified."
				: error
					? "Engine report unavailable. Open Operations for the recorded errors."
					: "Waiting for the first engine report…";
		const catalog =
			Array.isArray(data?.stage_catalog) && data.stage_catalog.length
				? data.stage_catalog
				: STAGE_CATALOG;
		replace(
			refs.engineStages,
			catalog
				.map((row) => {
					const key = row.id,
						value = stage(key),
						currentService = serviceFor(key),
						service = currentService,
						q = queueFor(row);
					const outcomes = value?.outcomes || {},
						issues =
							value && !value.workerOnly
								? (outcomes.failed || 0) + (outcomes.partial || 0)
								: null;
					const reason =
						!service && error
							? "The engine report could not be read."
							: serviceReason(service);
					const active =
						key === "applications" && freshRuntime()
							? (runtime.workers || []).filter((w) => w.status === "applying")
									.length
							: value?.historical
								? null
								: value?.active;
					const capacity = serviceCapacity(service);
					const researchSource =
						key === "company_research" && service?.policy_source
							? `<small>Effective policy: ${esc(service.policy_source)} · enabled ${esc(service.enabled_source || "unknown")} · companies ${esc(service.companies_source || "unknown")}${service.legacy_environment_override_ignored ? " · legacy environment switch ignored" : ""}</small>`
							: "";
					return ap4StableRow(
						key,
						`<tr data-engine-stage="${esc(key)}"><th scope="row"><strong>${esc(row.label)}</strong>${stateFor(key, value)}<small class="engine-stage-reason">${esc(reason)}</small>${capacity ? `<small>${capacity} · ${service.thread_alive === true ? "service thread alive" : service.thread_alive === false ? "service thread stopped" : "thread not reported"}</small>` : ""}${researchSource}</th><td>${number(active)}<small>${esc(row.unit)}</small></td><td>${row.queue_key ? number(q) : "—"}${row.queue_key ? queueNote(row) : ""}</td><td>${number(value && !value.workerOnly ? outcomes.success || 0 : null)} / <span class="${issues ? "engine-issues" : ""}">${number(issues)}</span></td></tr>`,
						service,
						value,
						active,
						q,
						!!row.queue_key,
					);
				})
				.join(""),
		);
		if (stageSummaryRoot) {
			replace(
				stageSummaryRoot,
				`<div class="card-head"><div><h2>All stages & services</h2><p>Concurrent availability and observed work—not a sequence of engine-wide steps.</p></div><a class="btn btn-sm" href="#overview?panel=engine">Full engine detail →</a></div><div class="overview-stage-grid">${catalog
					.map((row) => {
						const value = stage(row.id),
							service = serviceFor(row.id);
						return `<article data-overview-stage="${esc(row.id)}"><strong>${esc(row.label)}</strong>${stateFor(row.id, value)}<small>${esc(serviceReason(service))}</small></article>`;
					})
					.join("")}</div>`,
			);
		}
		refs.engineStageNote.textContent = current
			? "In flight = observed operations, not batch size. Queue = stage-reported eligible candidates; delayed counts carry their last-check time; — means not reported. Buffer totals use a separate snapshot. Stages share execution workers and follow their task dependencies. Succeeded / issues = retained operation outcomes since this runtime started; retries count separately, not unique jobs. No reported work is not a health guarantee."
			: `Last received stage values · ${date(data?.journal?.sample_at || data?.at)}. Live activity is awaiting a fresh report.`;
		const admission = data?.inventory?.scoring_admission;
		if (admission?.available) {
			const reasons = (admission.reasons || [])
				.map((row) => `${words(row.reason)}: ${number(row.jobs)}`)
				.join("; ");
			refs.engineStageNote.textContent += ` Scoring policy: ${admission.policy_label || "current admission policy"}. Raw scoring candidates: ${number(admission.raw_candidates)}. ${reasons}. Saved totals are not eligible queue sizes.`;
		}
		const active = liveView
			? liveView.workers
					.filter((row) => row.active && row.fresh)
					.map((row) => ({
						key: row.stage,
						stage: row.stage,
						started_at: row.started_at,
						operation: row.operation,
						job: {
							url: row.job_url,
							title: row.job_title,
							company: row.company,
							site: row.job_source,
						},
					}))
			: current
				? data?.journal?.active || []
				: [];
		// Give each active stage a card before filling spare slots. A large source
		// batch must not hide the smaller scoring/document/application services.
		const groups = new Map();
		for (const row of active) {
			const key = row.key || row.stage;
			if (!groups.has(key)) groups.set(key, []);
			groups.get(key).push(row);
		}
		const cards = [];
		while (
			cards.length < Math.max(12, groups.size) &&
			[...groups.values()].some((rows) => rows.length)
		) {
			for (const rows of groups.values())
				if (rows.length && cards.length < Math.max(12, groups.size))
					cards.push(rows.shift());
		}
		refs.engineActiveCount.textContent = current
			? `${active.length}${!liveView && data?.journal?.truncated ? "+" : ""} observed`
			: error
				? "Unavailable"
				: data
					? "Updates delayed"
					: "Loading…";
		replace(
			refs.engineCurrent,
			active.length
				? cards
						.map(
							(a) =>
								`<article class="engine-work-item"><div>${pill(NAMES[a.key] || words(a.stage), current ? "running" : "quiet")}<time>${current ? "Running " + elapsed(a.started_at) : "Last observed " + date(data?.journal?.sample_at || data?.at)}</time></div>${jobLink(a.job, a.source?.source_key ? a.source.source_key + " · " + (a.source.query || words(a.source.kind)) : a.task_id ? "Source task " + a.task_id.slice(0, 10) : `${NAMES[a.key] || words(a.stage)} · ${words(a.operation)}`)}<p>${esc([a.job?.company, a.job?.site, a.source?.location].filter(Boolean).join(" · ") || "This operation does not report a job title.")}</p></article>`,
						)
						.join("") +
						(active.length > cards.length
							? '<p class="engine-empty">A representative set of active operations is shown across all active stages. <a href="#workers">Open Workers for every reported execution slot.</a></p>'
							: "")
				: !current
					? '<p class="engine-empty">Live activity is awaiting an update. Last successful stage totals are retained above.</p>'
					: '<p class="engine-empty">No in-flight job or page operations were reported in this sample. See stage and buffer status; the parent heartbeat alone does not prove progress.</p>',
		);
		const workers = freshRuntime() ? runtime.workers || [] : [];
		replace(
			refs.engineWorkers,
			workers.length
				? workers
						.map(
							(w) =>
								`<article class="engine-worker"><div><strong>Worker ${number(w.worker_id)}</strong>${pill(words(w.status), w.status === "applying" ? "running" : "quiet")}</div><p>${esc(w.job_title || "No job reported")}${w.company ? " · " + esc(w.company) : ""}</p><small>Fit ${number(w.score)} · ${number(w.jobs_applied)} applied / ${number(w.jobs_failed)} failed reported by worker</small></article>`,
						)
						.join("")
				: '<p class="engine-empty">No application workers reported. Preparation can continue independently.</p>',
		);
		replace(
			refs.engineServices,
			SERVICES.map(([key, label]) => {
				const value = stage(key),
					out = value?.outcomes || {};
				return `<article><div><strong>${esc(label)}</strong>${stateFor(key, value)}</div><small>${number(value?.active)} in flight · ${number(value && !value.workerOnly ? out.success || 0 : null)} successful ${esc(value?.unit || "passes")} this runtime${key === "outreach" ? " · not sent-message totals" : ""}</small></article>`;
			}).join(""),
		);
		const search = p.last_search
			? esc(p.last_search)
			: "No search text reported in this runtime";
		const score =
			p.last_score == null
				? "No score reported in this runtime"
				: `${number(p.last_score)} / 10 · ${esc(p.last_scored_job || "Job not reported")}`;
		const sourceData = data?.sources,
			tasks = sourceData?.task_counts_available ? sourceData.tasks || [] : null;
		const sum = (key) =>
			tasks
				? tasks.reduce((total, t) => total + (Number(t[key]) || 0), 0)
				: null;
		replace(
			refs.engineSearch,
			`<div><span>Source task checkpoints</span><strong>${number(sum("done"))} / ${number(sum("total"))}</strong><small>Done / total · saved search context</small></div><div><span>Leased / pending tasks</span><strong>${number(sum("leased"))} / ${number(sum("pending"))}</strong><small>At ${esc(date(sourceData?.at))} · leases are not health checks</small></div><div class="engine-search-text"><span>Last reported search</span><p>${search}</p><span>Latest score reported this runtime</span><p>${score}</p>${freshRuntime() && p.discovery_total > 0 ? `<small>CLI search progress: ${number(p.discovery_done)} / ${number(p.discovery_total)} · ${number(p.new_jobs)} new · ${number(p.duplicates)} duplicates</small>` : ""}</div>`,
		);
	}
	function renderSaved() {
		if (!alive || !root.isConnected) return;
		const inventory = data?.inventory,
			metrics = inventory?.metrics || {};
		const entries = [
			["enriched", "Enriched"],
			["scored", "Scored"],
			[
				"qualified",
				data?.min_score == null
					? "Fit threshold unknown"
					: `Fit ≥ ${number(data.min_score)} / 10`,
			],
			["tailored", "Résumés recorded"],
			["cover", "Cover letters recorded"],
			["brief", "Interview briefs recorded"],
		];
		const run = data?.current_run || {},
			runNote = `Saved current-run records · ${esc(date(run.captured_at))}${run.available ? "" : "<br>" + esc(run.reason || "Run totals unavailable")}`;
		const runMetrics = [
			["submitted_attempts", "Submissions · this run"],
			["verification_holds", "Verification holds · this run"],
			["cancellations", "Cancellations · this run"],
		]
			.map(([key, label]) =>
				metric(label, run.available ? run[key] : null, runNote),
			)
			.join("");
		replace(
			refs.engineMetrics,
			runMetrics +
				entries
					.map(([key, label]) =>
						metric(
							label,
							inventory?.available ? metrics[key]?.total : null,
							`${number(inventory?.available ? metrics[key]?.today : null)} updated ${inventory?.retained ? "on " + esc(inventory.day || "last check") : "today"} · UTC<br>Stored jobs · all time${inventory?.retained ? "<br>Last successful check: " + esc(date(inventory.at)) : ""}`,
						),
					)
					.join(""),
		);
		replace(
			root.querySelector("[data-recovery]"),
			recoveryView(data?.recovery),
		);
		const sources = data?.sources || {},
			health = new Map((sources.health || []).map((r) => [r.source_name, r]));
		const byName = new Map(
			(sources.tasks || []).map((t) => [
				t.source_key,
				{ task: t, health: health.get(t.source_key) },
			]),
		);
		for (const [name, h] of health) {
			if (!byName.has(name)) byName.set(name, { health: h });
		}
		refs.engineSourceNote.textContent = `${sources.available ? (sources.retained ? "Last successful checkpoint check: " : "Checkpoints checked ") + date(sources.at) : "Source checkpoints unavailable"}. Task totals cover the saved search context; health is the last recorded run, not a live check.${sources.truncated ? " Showing bounded source results; checkpoint totals may be partial." : ""}`;
		const tasks = sources.current_tasks || [];
		replace(
			refs.engineSourceNow,
			tasks.length
				? `<p><strong>Leased source tasks at the last check</strong> · a lease is not proof of an active request</p><div class="engine-source-tags">${tasks.map((t) => `<span>${esc(t.source_key)}${t.query ? " · " + esc(t.query) : ""}${t.location ? " · " + esc(t.location) : ""} <small>${esc(words(t.kind))}</small></span>`).join("")}</div>`
				: "<p>No unexpired source task leases at the last check. Discovery may be waiting on the buffer, cooldown or source configuration.</p>",
		);
		replace(
			refs.engineSources,
			byName.size
				? [...byName]
						.map(
							([name, { task: t, health: h }]) =>
								`<tr><th scope="row">${esc(name)}${t && t.provider !== name ? `<small>${esc(t.provider)}</small>` : ""}</th><td>${number(t?.leased)} / ${number(t?.pending)}${t?.expired ? `<small class="engine-issues">${number(t.expired)} expired leases</small>` : ""}</td><td>${number(t?.done)} / ${number(t?.blocked)}</td><td>${h ? pill(words(h.status), h.consecutive_failures ? "waiting" : "quiet") + "<small>" + number(h.jobs_found) + " found / " + number(h.jobs_stored) + " stored · " + number(h.consecutive_failures) + " consecutive failures</small>" : "No saved health result"}</td><td>${esc(date(h?.last_run_at || t?.last_at))}${h ? "<small>Last success: " + esc(date(h.last_success_at)) + "</small>" : ""}</td></tr>`,
						)
						.join("")
				: '<tr><td colspan="5">No readable source checkpoints yet. This does not mean every provider is disabled.</td></tr>',
		);
		replace(
			refs.engineRecent,
			[
				["recent_enriched", "Enriched", "detail_scraped_at"],
				["recent_scored", "Scored", "scored_at"],
			]
				.map(
					([key, label, field]) =>
						`<section><h3>${label}</h3>${(inventory?.[key] || []).map((j) => `<article class="engine-recent-item"><div>${jobLink(j)}${key === "recent_scored" ? `<strong class="engine-score">${number(j.fit_score)} / 10</strong>` : ""}</div><p>${esc(j.company || "Company not recorded")} · ${esc(j.site || "Source not recorded")}</p><time>${esc(date(j[field]))}</time></article>`).join("") || '<p class="engine-empty">No readable ' + label.toLowerCase() + " job timestamps.</p>"}</section>`,
				)
				.join(""),
		);
	}
	function updateSnapshot(value, force = false) {
		if (!alive || (!force && updatesPaused()) || !value) return;
		if (data && stamp(value.at) < stamp(data.at)) return;
		value = { ...value };
		// Keep recorded values across partial reads without renewing their evidence.
		for (const key of ["inventory", "sources", "journal", "scheduler"]) {
			const good = ["scheduler", "journal"].includes(key)
				? "current"
				: "available";
			const sameRun = value.runtime_id === data?.runtime_id;
			if (
				value[key]?.[good] !== true &&
				data?.[key]?.[good] === true &&
				(["inventory", "sources"].includes(key) || sameRun) &&
				(key !== "inventory" || value.min_score === data.min_score)
			)
				value[key] = {
					...data[key],
					retained: true,
					sample_at: data[key].sample_at || data.at,
				};
			else if (value[key]?.[good] === true)
				value[key] = {
					...value[key],
					retained: false,
					sample_at: value[key].at || value.at,
				};
		}
		if (
			value.runtime_id === data?.runtime_id &&
			value.scheduler?.current &&
			data?.scheduler?.stages
		) {
			const rows = { ...value.scheduler.stages };
			for (const [key, old] of Object.entries(data.scheduler.stages)) {
				if (
					(!rows[key] || rows[key].state === "unknown") &&
					old.state !== "unknown"
				)
					rows[key] = {
						...old,
						retained: true,
						sample_at: old.sample_at || data.scheduler.sample_at || data.at,
					};
			}
			value.scheduler = { ...value.scheduler, stages: rows };
		}
		data = value;
		error = "";
		renderSaved();
		renderLive();
	}
	async function refresh(force = false) {
		clearTimeout(timer);
		if (!alive || busy || document.hidden) return;
		if (updatesPaused() && !force) {
			timer = setTimeout(refresh, 1000);
			return;
		}
		busy = true;
		try {
			const value = await api("engine");
			if (!alive || (updatesPaused() && !force)) return;
			updateSnapshot(value, force);
		} catch (e) {
			if (!alive) return;
			error =
				"Engine report could not update. Saved values are from the last successful check. " +
				String(e.message || "");
			renderLive();
		} finally {
			busy = false;
			if (alive && !document.hidden) timer = setTimeout(refresh, 15000);
		}
	}
	function visibility() {
		clearTimeout(timer);
		if (!document.hidden) refresh();
	}
	function click(e) {
		const button = e.target.closest("[data-engine-job]");
		if (button && root.contains(button)) onOpenJob?.(button.dataset.engineJob);
	}
	root.addEventListener("click", click);
	document.addEventListener("visibilitychange", visibility);
	window.addEventListener("applypilot:invalidate", visibility);
	const expiry = setInterval(() => {
		if (!document.hidden && !updatesPaused()) renderLive();
	}, 1000);
	renderSaved();
	renderLive();
	refresh();
	return {
		updateStatus(value) {
			liveStatus = value;
			renderLive();
		},
		updateRuntime(value) {
			runtime = value;
			if (value?.running && !value.stale) lastRuntime = value;
			renderLive();
		},
		updateQueue(value) {
			queue = value;
			renderLive();
		},
		updateSettings(value) {
			settings = value;
			renderLive();
		},
		refresh: () => refresh(true),
		dispose() {
			alive = false;
			clearTimeout(timer);
			clearInterval(expiry);
			root.removeEventListener("click", click);
			document.removeEventListener("visibilitychange", visibility);
			window.removeEventListener("applypilot:invalidate", visibility);
		},
	};
}
