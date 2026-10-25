const esc = (value) =>
	String(value ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const count = (value) =>
	typeof value === "number" && Number.isFinite(value)
		? value.toLocaleString()
		: "—";
const words = (value) => String(value || "").replaceAll("_", " ");
const stageNames = {
	discover: "Discovery",
	discovery: "Discovery",
	enrich: "Enrichment",
	score: "Scoring",
	tailor: "Résumé tailoring",
	cover: "Cover letters",
	pdf: "Documents",
	brief: "Interview briefs",
	recovery: "Recovery",
	company_research: "Company research",
	network_research: "Network research",
	network_sync: "Mailbox and delivery",
	admission: "Assessment refresh",
	tracking: "Mailbox tracking",
	outreach: "Outreach delivery",
	unattended: "Networking maintenance",
	referrals: "Referral planning",
	learning: "Learning",
	backup: "Backups",
};
const stageStates = {
	running: "Working",
	working: "Working",
	waiting_provider: "Awaiting model",
	waiting_capacity: "Waiting for capacity",
	queued: "Queued",
	waiting: "Waiting",
	buffer_wait: "Buffer paused",
	buffer_paused: "Buffer paused",
	backoff: "Retry waiting",
	blocked: "Blocked",
	idle: "Idle",
	paused: "Paused",
	disabled: "Disabled",
	error: "Error",
	stopped: "Stopped",
};
const preparedBlockers = {
	automatic_recovery_pending: "awaiting recovery",
	provider_admission_wait: "awaiting provider eligibility",
	submission_identity_conflict: "submission conflicts",
	company_capacity: "company limits",
	target_circuit_open: "destination retry waits",
	referral_hold: "referral holds",
	retry_backoff: "retry waits",
	document_files: "document files need repair",
	allocation_or_snapshot_wait: "awaiting allocation or a fresh selection",
};
const when = (value) => {
	const date = new Date(typeof value === "number" ? value * 1000 : value);
	return value && !Number.isNaN(date.valueOf()) ? date.toLocaleString() : "";
};
const age = (value) =>
	typeof value === "number" && Number.isFinite(value)
		? `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s`
		: "";

export function schedulerLink(execution = {}) {
	try {
		const url = new URL(execution.dashboard_link);
		if (
			!["http:", "https:"].includes(url.protocol) ||
			!["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
		)
			return "";
		return `<a class="btn btn-sm" href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">Open scheduler dashboard</a>`;
	} catch {
		return "";
	}
}

export function runtimeHealth({
	current = false,
	execution = {},
	active,
	queued,
	waitingProvider,
	warnings = [],
	error = "",
	paused = false,
	runtimeState = "",
} = {}) {
	let title,
		detail,
		tone = "neutral";
	if (error) {
		title = "Report unavailable";
		detail = error + " Previously displayed activity may be stale.";
		tone = "warning";
	} else if (["paused", "stopped"].includes(runtimeState)) {
		title =
			runtimeState === "paused"
				? "Autopilot is paused"
				: "Autopilot is stopped";
		detail =
			"Use the Autopilot controls above to " +
			(runtimeState === "paused" ? "resume" : "start") +
			" work.";
		tone = "warning";
	} else if (!current) {
		title = "Current activity is unknown";
		detail = "Waiting for a fresh, matching runtime report.";
	} else if (paused) {
		title = "Display updates paused";
		detail = "Resume display updates to see current activity.";
	} else if (execution.stopping) {
		title = "Finishing active work";
		detail = "Autopilot is draining submitted tasks before stopping.";
	} else if (warnings.length || execution.warnings?.length) {
		title = "Work needs attention";
		tone = "warning";
		detail = [...new Set([...warnings, ...(execution.warnings || [])])].join(
			" ",
		);
	} else if (active > 0) {
		title = "Work is active";
		detail = `${count(active)} tasks have started${typeof waitingProvider === "number" ? `, ${count(waitingProvider)} reporting a model wait` : ""}. Active tasks can be waiting on resources; saved outcomes are reported separately.`;
	} else if (queued > 0) {
		title = "Work is queued";
		detail = `${count(queued)} tasks are waiting for their dependencies or an execution worker. Stage reasons appear below.`;
	} else {
		title =
			active === 0
				? "No active task reported"
				: "Execution counts are incomplete";
		detail =
			"Check application selection and stage queues for eligible work and recorded blockers.";
	}
	return `<section class="runtime-health runtime-health-${tone}" aria-label="Execution status"><div><h3>${esc(title)}</h3><p>${esc(detail)}</p>${current ? schedulerLink(execution) : ""}</div>${current && !error && !paused ? `<p class="runtime-health-counts"><strong>${count(active)}</strong> executing <span aria-hidden="true">·</span> <strong>${count(queued)}</strong> queued</p>` : ""}</section>`;
}

function stageTable(entries) {
	return `<div class="stage-queue-scroll" tabindex="0" role="region" aria-label="Stage queue details"><table class="stage-queue-table"><thead><tr><th scope="col">Stage</th><th scope="col">Status</th><th scope="col">Active</th><th scope="col">Queued</th><th scope="col">Output buffer</th><th scope="col">Tasks finished</th><th scope="col">Reason and last result</th></tr></thead><tbody>${entries
		.map(([name, stage]) => {
			const pending = stage.queued_tasks ?? stage.pending;
			const label =
				stageStates[stage.state] || words(stage.state) || "Not reported";
			const reason =
				(stage.buffer?.paused && (stage.active_tasks > 0 || pending > 0)
					? "Previously scheduled tasks can finish. "
					: "") + (stage.reason || "No reason recorded.");
			const notes = [];
			if (typeof stage.ready_backlog === "number")
				notes.push(
					`${count(stage.ready_backlog)} eligible${typeof stage.deferred_backlog === "number" ? ` · ${count(stage.deferred_backlog)} awaiting repair, retry or admission` : ""}`,
				);
			if (stage.repair_tasks > 0)
				notes.push(
					`${count(stage.repair_tasks)} existing jobs being repaired; bulk production remains buffer-controlled.`,
				);
			if (stage.oldest_queue_seconds > 0)
				notes.push(`Oldest scheduled wait: ${age(stage.oldest_queue_seconds)}`);
			if (stage.last_result_at)
				notes.push(
					`${stage.last_result_scope === "saved_workspace" ? "Saved workspace result" : "Last result"}: ${when(stage.last_result_at)}`,
				);
			if (stage.last_result_reason && stage.last_result_reason !== reason)
				notes.push(
					`Last ${words(stage.last_result) || "result"}: ${stage.last_result_reason}`,
				);
			if (stage.retry_at) notes.push(`Retry: ${when(stage.retry_at)}`);
			if (stage.last_verified_at)
				notes.push(
					`Last saved backup checkpoint: ${when(stage.last_verified_at)}`,
				);
			const buffers = Object.values(stage.output_buffers || {});
			if (!buffers.length && stage.buffer) buffers.push(stage.buffer);
			for (const buffer of buffers) {
				const blockers = Object.entries(buffer.prepared_blockers || {}).filter(
					([, value]) => typeof value === "number" && value > 0,
				);
				if (blockers.length)
					notes.push(
						`Prepared applications: ${blockers.map(([code, value]) => `${count(value)} ${preparedBlockers[code] || words(code)}`).join(" · ")}.`,
					);
			}
			const bufferText = buffers
				.map(
					(buffer) =>
						`${esc(buffer.label || buffer.name || "Output")}: ${buffer.mode === "automatic" ? "Automatic capacity" : `${count(buffer.occupancy)} / ${count(buffer.high)}`}<br><small>${typeof buffer.consumable_stock === "number" ? `${count(buffer.consumable_stock)} runnable · ${count(buffer.prepared_stock)} prepared` : `${count(buffer.stock)} saved`} · ${count(buffer.reserved)} scheduled${buffer.mode === "automatic" ? "" : ` · resume at ${count(buffer.low)}`}${buffer.route_blocked ? ` · ${count(buffer.route_blocked)} need an application route` : ""}</small>`,
				)
				.join("<br>");
			return `<tr><th scope="row">${esc(stageNames[name] || words(name))}</th><td><span class="stage-status">${esc(label)}</span></td><td>${count(stage.active_tasks)}</td><td>${count(pending)}</td><td>${bufferText || "—"}</td><td>${count(stage.completed_tasks)}${stage.failed_tasks ? `<br><small>${count(stage.failed_tasks)} failed</small>` : ""}</td><td>${esc(reason)}${notes.map((note) => `<br><small>${esc(note)}</small>`).join("")}</td></tr>`;
		})
		.join("")}</tbody></table></div>`;
}

export function stageQueues(stages = {}) {
	const entries = Object.entries(stages).filter(
		([name, stage]) =>
			stage &&
			typeof stage === "object" &&
			!["engine", "autopilot", "applications", "flow_control"].includes(name),
	);
	const pipeline = [
		"discover",
		"discovery",
		"enrich",
		"score",
		"tailor",
		"cover",
		"pdf",
		"brief",
	];
	const main = entries
		.filter(([name]) => pipeline.includes(name))
		.sort(([a], [b]) => pipeline.indexOf(a) - pipeline.indexOf(b));
	const other = entries.filter(([name]) => !pipeline.includes(name));
	return `<section class="stage-queues" aria-label="Stage queues"><header><h3>Stage queues</h3><p>Buffers count saved output plus scheduled work. At the limit, new work pauses until the resume target; scheduled tasks finish. Finished task counts include checks that produced no output.</p></header>${main.length ? stageTable(main) : '<p class="stage-queues-empty">No stage-level report received.</p>'}${other.length ? `<details class="stage-queue-more"><summary>Supporting services (${other.length})</summary>${stageTable(other)}</details>` : ""}</section>`;
}
