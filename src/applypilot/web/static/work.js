import { statusView } from "./live-status.js";
import { recoveryView } from "./recovery-view.js";
import { runtimeHealth, stageQueues } from "./runtime-health.js";
import { updatesPaused } from "./ux.js";

const esc = (value) =>
	String(value ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const count = (value) =>
	typeof value === "number" ? value.toLocaleString() : "—";
const words = (value) => String(value || "").replaceAll("_", " ");
const when = (value) => {
	const d =
		typeof value === "number" ? new Date(value * 1000) : new Date(value);
	return value && !isNaN(d) ? d.toLocaleString() : "Not recorded";
};
const labels = {
	success: "Completed",
	submitted: "Submitted",
	sent: "Delivered",
	manual_sent: "Delivery recorded",
	ats_referral: "Secured",
	completed: "Completed",
	failed: "Failed",
	partial: "Incomplete",
	verification_required: "Verification needed",
	verification_quarantined: "Verification needed",
	cancelled: "Cancelled",
	interrupted: "Interrupted",
	timed_out: "Timed out",
	deferred: "Waiting",
	started: "Started",
};
const good = (value) =>
	[
		"success",
		"submitted",
		"sent",
		"manual_sent",
		"ats_referral",
		"completed",
	].includes(value);
const bad = (value) =>
	[
		"failed",
		"partial",
		"verification_required",
		"verification_quarantined",
		"timed_out",
	].includes(value);
const badge = (value) =>
	`<span class="work-result ${good(value) ? "work-good" : bad(value) ? "work-bad" : "work-neutral"}">${esc(labels[value] || words(value) || "Not recorded")}</span>`;
const json = (value) =>
	`<pre class="work-evidence">${esc(JSON.stringify(value, null, 2))}</pre>`;

export function mountWork(root, { api, onOpenJob, notify }) {
	let disposed = false,
		busy = false,
		exportBusy = false,
		timer = null,
		data = null,
		liveStatus = null,
		runtimeState = "",
		before = "",
		history = [],
		revision = 0;
	const $ = (selector) => root.querySelector(selector);
	root.innerHTML = `<div class="work-view">
    <div data-runtime-health role="status" aria-live="polite"></div>
    <section class="card work-now"><div class="card-head"><div><h2>Now</h2><p data-now-stamp>Waiting for a fresh work report…</p></div><a class="btn btn-sm" href="#workers">All workers →</a></div><div data-now></div></section>
    <div class="work-outcomes" data-outcomes aria-label="Confirmed outcomes"></div>
    <p class="work-definition">All-time outcomes use saved records across runs. Current-run totals use exact saved run links. Completed preparation does not count as a submission.</p>
    <p class="work-definition" data-run-note></p>
    <section class="card work-support" data-recovery></section>
    <p class="notice notice-warning" data-error role="status" hidden></p>
    <section class="card work-recent"><div class="card-head"><div><h2>Recent work</h2><p>What ran, what happened, and the recorded reason.</p></div><div class="work-export"><label>Run<select data-export-run aria-label="Run to export" disabled><option>Loading runs…</option></select></label><button class="btn btn-sm" type="button" data-export disabled>Export JSON</button><small data-export-status></small><a data-export-download hidden>Download again</a><span class="work-page-note" data-page-note></span></div></div>
      <div data-rows class="work-rows" aria-live="polite">Loading work…</div>
      <div class="work-pagination"><button class="btn" data-newer>← Newer</button><span data-page-count></span><button class="btn" data-older>Older →</button></div>
    </section>
    <p class="work-definition">One work attempt per row. Requests, retries and warnings stay with their attempt. Historical evidence can be incomplete.</p>
  </div>`;
	function renderNow() {
		if (!data && !liveStatus) return;
		const disclosures = new Set(
			[...$("[data-now]").querySelectorAll("details[open]")].map(
				(el) => el.querySelector(":scope > summary")?.textContent,
			),
		);
		const live = liveStatus ? statusView(liveStatus) : null;
		const saved = data?.now || {};
		const now = live
			? {
					current: live.current,
					at: new Date(live.at).toISOString(),
					runtime_state: runtimeState || live.runtime.payload.phase,
					scheduler: {
						current: live.current,
						stages: live.stages,
						execution: live.execution.current ? live.execution.payload : {},
					},
					applications: {
						...live.applications.payload,
						fresh: live.applications.selectionCurrent,
					},
					active: live.workers.filter((worker) => worker.active),
					waiting_provider: live.workers.filter(
						(worker) => worker.active && worker.state === "waiting_provider",
					).length,
					blockers:
						data?.runtime_id === liveStatus.runtime_id ? saved.blockers : [],
					warnings: live.execution.current
						? live.execution.payload.warnings || []
						: [],
				}
			: saved;
		const scheduler = now.scheduler || {},
			application = now.applications || {},
			at = Date.parse(now.at || ""),
			fresh =
				now.current &&
				Number.isFinite(at) &&
				Date.now() - at >= -5000 &&
				Date.now() - at < 20000;
		const selectionFresh =
			application.fresh && application.selection_available === true;
		const applicationReason = !selectionFresh
			? (application.active_tasks > 0
					? `${count(application.active_tasks)} application tasks are active; ready queue count is awaiting its next check.`
					: "Waiting for a current application selection check.") +
				(application.checked_at
					? ` Last checked ${when(application.checked_at)}.`
					: "")
			: application.reason === "dask_scheduler"
				? "Applications run as execution slots become available."
				: application.reason;
		$("[data-now-stamp]").textContent = fresh
			? `Current report · ${when(now.at)}`
			: ["paused", "stopped"].includes(now.runtime_state)
				? `Autopilot ${now.runtime_state} · recorded outcomes remain below`
				: `Current work is unknown · last received ${when(now.at)}`;
		const stages = scheduler.stages || {},
			execution = scheduler.execution || {};
		$("[data-runtime-health]").innerHTML = runtimeHealth({
			current: fresh && scheduler.current,
			execution,
			active: execution.active_tasks,
			queued: execution.queued_tasks,
			waitingProvider: now.waiting_provider,
			warnings: now.warnings || [],
			paused: updatesPaused(),
			runtimeState: now.runtime_state,
		});
		const active = fresh ? now.active || [] : [];
		const queued = selectionFresh && fresh ? application.selectable_jobs : null;
		const referral = stages.referrals || {};
		const activeRows = active
			.slice(0, 8)
			.map(
				(w) =>
					`<div class="work-live-row"><span class="work-live-dot"></span><div><strong>${esc(w.job_title || w.model_task || words(w.stage) || "Work in progress")}</strong><small>${esc([w.company, words(w.phase || w.state)].filter(Boolean).join(" · "))}</small></div><span>${esc(words(w.stage))}</span></div>`,
			)
			.join("");
		const blockers = (now.blockers || [])
			.map(
				(b) =>
					`<li><span>${esc(b.label || b.code)}</span><strong>${count(b.count)}</strong></li>`,
			)
			.join("");
		$("[data-now]").innerHTML =
			`<div class="work-demand"><article><span>Runnable applications</span><strong>${count(queued)}</strong><small>${esc(applicationReason || "Waiting for the common selector")}</small></article><article><span>Referral tasks queued</span><strong>${count(fresh && scheduler.current ? referral.queued_tasks : null)}</strong><small>${esc(referral.reason || "Waiting for a service report")}</small></article><article><span>Active work</span><strong>${count(fresh && scheduler.current ? execution.active_tasks : null)}</strong><small>Tasks reported by the execution scheduler</small></article></div>
      <div class="work-current-grid"><div class="work-current-list">${activeRows || `<p class="work-empty">${fresh ? "No active task in this report." : "Waiting for a fresh matching runtime report."}</p>`}</div><div class="work-blockers"><h3>What is blocking applications</h3>${blockers ? `<ul>${blockers}</ul>` : `<p>${esc(applicationReason || "No current selector diagnosis recorded.")}</p>`}<p class="work-definition">${count(data?.saved_ready_labels)} jobs retain a ready label. Runnable demand above comes from the application selector.</p></div></div>
      ${stageQueues(stages)}
      <details class="work-support"><summary>Execution workers</summary><p>${fresh && scheduler.current ? `${count(execution.workers)} shared workers · ${count(execution.browser_available_slots)} browser slots available of ${count(execution.browser_workers)} configured.` : "Worker counts await a fresh scheduler report."}</p><details><summary>Recorded scheduler report</summary>${json(execution)}</details><details><summary>Recorded application selection</summary><p>Saved selection values keep their original check time; an expired count is not current demand.</p>${json(application)}</details></details>`;
		for (const el of $("[data-now]").querySelectorAll("details"))
			if (disclosures.has(el.querySelector(":scope > summary")?.textContent))
				el.open = true;
	}
	function render() {
		const totals = data.outcomes || {},
			run = data.current_run || {};
		const recovering = new Set(
			(data.recovery?.items || []).map((item) => item.job_url),
		);
		const rowBadge = (row) =>
			recovering.has(row.job_url) && !row.saved_outcome
				? `<span class="work-result work-neutral">${["verification_required", "verification_quarantined", "side_effect_intent"].includes(row.outcome) ? "Checking submission" : "Automatic retry"}</span>`
				: badge(row.outcome);
		$("[data-outcomes]").innerHTML = [
			[
				"Applications confirmed · all time",
				totals.applications,
				"Distinct jobs with a saved submitted attempt",
				"applications",
			],
			[
				"Outreach delivered · all time",
				totals.outreach,
				"Messages recorded sent; promises are separate",
				"outreach",
			],
			[
				"Referrals secured · all time",
				totals.referrals,
				"Explicitly confirmed ATS referrals",
				"referrals",
			],
			[
				"Application attempts · this run",
				run.available ? run.attempts : null,
				"Saved attempts, including reservations and failures",
				"applications",
			],
			[
				"Submissions confirmed · this run",
				run.available ? run.submitted_attempts : null,
				`${count(run.applications)} distinct jobs with a saved submitted attempt`,
				"applications",
			],
		]
			.map(
				([title, value, note, key]) =>
					`<article class="work-outcome work-outcome-${key}"><span>${title}</span><strong>${count(value)}</strong><p>${note}</p></article>`,
			)
			.join("");
		$("[data-run-note]").textContent = [
			run.run_id ? `Run ${run.run_id}.` : "No run identity recorded.",
			`Checked ${when(run.captured_at)}.`,
			run.available
				? `${count(run.verification_holds)} verification holds · ${count(run.cancellations)} cancellations.`
				: "",
			run.reason || run.scope,
		]
			.filter(Boolean)
			.join(" ");
		$("[data-recovery]").innerHTML = recoveryView(data.recovery);
		renderNow();
		const open = new Map(
			[...root.querySelectorAll("[data-work-id]")]
				.filter((el) => el.open)
				.map((el) => [el.dataset.workId, el]),
		);
		const fragment = document.createElement("div");
		fragment.innerHTML =
			(data.items || [])
				.map(
					(row) =>
						`<details class="work-row" data-work-id="${esc(row.id)}"><summary><div class="work-row-main"><div class="work-row-title"><strong>${esc(row.target || row.action)}</strong>${rowBadge(row)}</div><p>${esc([row.company, row.target ? row.action : ""].filter(Boolean).join(" · "))}</p><p class="work-message ${bad(row.outcome) ? "work-error-text" : ""}">${esc(row.message || "Original result was not recorded.")}</p><small>${esc(row.origin || row.source)}${row.model ? " · " + esc(row.model) : ""}</small></div><time>${esc(when(row.at))}</time><span class="work-chevron" aria-hidden="true">⌄</span></summary><div class="work-detail" data-detail><p>Loading recorded evidence…</p></div></details>`,
				)
				.join("") ||
			'<p class="work-empty">No retained work records on this page.</p>';
		for (const el of fragment.querySelectorAll("[data-work-id]"))
			if (open.has(el.dataset.workId))
				el.replaceWith(open.get(el.dataset.workId));
		$("[data-rows]").replaceChildren(...fragment.childNodes);
		$("[data-newer]").disabled = !history.length;
		$("[data-older]").disabled = !data.next_before;
		$("[data-page-note]").textContent = before
			? "Earlier records"
			: "Newest first";
		$("[data-page-count]").textContent =
			`Page ${history.length + 1} · ${(data.items || []).length} records`;
		const warning = (data.warnings || []).join(" ");
		$("[data-error]").hidden = !warning;
		$("[data-error]").textContent = warning;
	}
	async function load() {
		clearTimeout(timer);
		if (disposed || busy) return;
		if (document.hidden || updatesPaused()) {
			timer = setTimeout(load, 5000);
			return;
		}
		busy = true;
		const current = revision;
		try {
			const value = await api(
				"work?" +
					new URLSearchParams({ limit: "25", ...(before ? { before } : {}) }),
			);
			if (!disposed && current === revision) {
				data = value;
				render();
			}
		} catch (error) {
			if (!disposed) {
				$("[data-error]").hidden = false;
				$("[data-error]").textContent =
					error.message + " Previously displayed records may be stale.";
			}
		} finally {
			busy = false;
			if (!disposed) timer = setTimeout(load, current === revision ? 10000 : 0);
		}
	}
	async function loadRuns() {
		const select = $("[data-export-run]"),
			button = $("[data-export]"),
			status = $("[data-export-status]");
		try {
			const value = await api("work/runs");
			if (disposed) return;
			select.innerHTML =
				(value.items || [])
					.map(
						(run) =>
							`<option value="${esc(run.id)}"${run.id === value.default_run_id ? " selected" : ""}>${esc(when(run.started_at))} · ${esc(words(run.role))} · ${esc(run.id.slice(0, 8))} · ${count(run.event_count)} events</option>`,
					)
					.join("") || '<option value="">No retained runs</option>';
			select.disabled = !(value.items || []).length;
			button.disabled = select.disabled;
			status.textContent = select.disabled
				? "Start autopilot to create a run."
				: "Exports all retained evidence and labels historical gaps.";
		} catch (error) {
			if (!disposed) {
				select.innerHTML = '<option value="">Runs unavailable</option>';
				status.textContent = error.message;
			}
		}
	}
	async function exportRun() {
		const select = $("[data-export-run]"),
			button = $("[data-export]"),
			status = $("[data-export-status]"),
			retry = $("[data-export-download]");
		if (exportBusy || !select.value) return;
		exportBusy = true;
		select.disabled = true;
		button.disabled = true;
		button.textContent = "Preparing…";
		retry.hidden = true;
		retry.removeAttribute("href");
		try {
			let task = await api("work/exports", {
				method: "POST",
				body: { run_id: select.value },
			});
			while (!disposed && task.state === "building") {
				status.textContent = `Building export · ${words(task.phase)}`;
				await new Promise((resolve) => setTimeout(resolve, 750));
				task = await api("work/exports/" + encodeURIComponent(task.id));
			}
			if (disposed) return;
			if (task.state !== "ready" || !task.download_url)
				throw new Error(task.error || "The work export did not complete.");
			status.textContent = task.coverage_complete
				? "Complete retained coverage · download ready for 30 minutes."
				: `Download ready with ${count(task.coverage_warnings)} coverage warning${task.coverage_warnings === 1 ? "" : "s"} recorded in the file.`;
			retry.href = task.download_url;
			retry.hidden = false;
			const link = document.createElement("a");
			link.href = task.download_url;
			link.download = "";
			document.body.append(link);
			link.click();
			link.remove();
			notify(
				task.coverage_complete
					? "Selected run export is ready with complete retained coverage."
					: "Selected run export is ready and labels its incomplete historical coverage.",
			);
		} catch (error) {
			if (!disposed) {
				status.textContent = error.message;
				notify(error.message, true);
			}
		} finally {
			if (!disposed) {
				exportBusy = false;
				select.disabled = !select.options.length || !select.value;
				button.disabled = select.disabled;
				button.textContent = "Export JSON";
			}
		}
	}
	root.addEventListener(
		"toggle",
		async (event) => {
			const row = event.target;
			if (!row.matches("[data-work-id]")) return;
			if (!row.open) {
				delete row.dataset.loaded;
				return;
			}
			if (row.dataset.loaded) return;
			row.dataset.loaded = "loading";
			try {
				const detail = await api(
					"work/" + encodeURIComponent(row.dataset.workId),
				);
				if (disposed || !row.isConnected) return;
				const job = detail.item?.job_url;
				row.querySelector("[data-detail]").innerHTML =
					`${job ? `<button class="btn btn-sm" data-job-url="${esc(job)}">Open job</button>` : ""}<p>${esc(detail.note || detail.message || "Saved work record")}</p>${(detail.events || []).map((e) => `<section class="work-event"><strong>${esc(e.component)} / ${esc(e.operation)}</strong> ${badge(e.outcome)}<small>${esc(when(e.at))}</small><p>${esc(e.message)}</p>${(e.details?.exception_chain || []).map((c) => `<p class="work-error-text"><strong>${esc(c.type)}</strong>: ${esc(c.message)}</p>`).join("")}<details><summary>Recorded details and correlation</summary>${json(e)}</details></section>`).join("")}${!detail.events?.length ? json(detail.item || detail.record || {}) : ""}`;
				if (detail.item?.committed_result)
					row
						.querySelector("[data-detail]")
						.insertAdjacentHTML(
							"afterbegin",
							`<p><strong>Saved result:</strong> ${esc(detail.item.committed_result.to_state)} · ${esc(when(detail.item.committed_result.at))}</p>`,
						);
				if (detail.validation_report)
					row
						.querySelector("[data-detail]")
						.insertAdjacentHTML(
							"beforeend",
							`<details><summary>Original document validation report</summary>${json(detail.validation_report)}</details>`,
						);
				row
					.querySelector("[data-detail]")
					.insertAdjacentHTML(
						"beforeend",
						`<p class="work-definition">Evidence snapshot · ${esc(new Date().toLocaleString())}. Close and reopen to refresh.${detail.truncated ? " Showing the latest 200 retained events." : ""}</p>`,
					);
				row.dataset.loaded = "true";
			} catch (error) {
				delete row.dataset.loaded;
				if (row.isConnected)
					row.querySelector("[data-detail]").textContent = error.message;
			}
		},
		true,
	);
	root.onclick = (event) => {
		const button = event.target.closest("[data-job-url]");
		if (button) onOpenJob(button.dataset.jobUrl);
	};
	$("[data-older]").onclick = () => {
		if (!data?.next_before) return;
		history.push(before);
		before = data.next_before;
		revision++;
		load();
	};
	$("[data-newer]").onclick = () => {
		if (!history.length) return;
		before = history.pop();
		revision++;
		load();
	};
	$("[data-export]").onclick = exportRun;
	loadRuns();
	const expiry = setInterval(() => {
		if (!disposed && !updatesPaused()) renderNow();
	}, 5000);
	return {
		refresh: load,
		updateStatus(value) {
			liveStatus = value;
			if (!disposed) renderNow();
		},
		updateRuntime(value) {
			runtimeState = value?.control?.state || "";
			if (!disposed) renderNow();
		},
		dispose() {
			disposed = true;
			clearTimeout(timer);
			clearInterval(expiry);
		},
	};
}
