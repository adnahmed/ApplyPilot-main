import {
	backlogFields,
	fillBacklog,
	readBacklog,
	validBacklog,
} from "./backlog_settings.js";
import { panelSwitch } from "./panel_switch.js";

let pagePanels = null;

import { mountEngineOverview } from "./engine.js";
import { connectLive } from "./live.js";
import { runtimeStatus } from "./live-status.js";
import { mountNavigation } from "./navigation.js";
import { mountOutreachControls } from "./outreach_controls.js";
/* ApplyPilot's local control room. No analytics, CDN calls, or account secrets.
 * All database strings are escaped; writes use the owner's authenticated session
 * and the server's existing transaction guards. Polling pauses in hidden tabs.
 */
import { confirmAction, trackForm, updatesPaused } from "./ux.js";
import { mountWorkerSettings } from "./worker_settings.js";
import { mountWorkers } from "./workers.js";

let workersView = null;
let workerSettings = null;

import { mountWork } from "./work.js";

let workView = null;

import { chartTable, lineChart } from "./charts.js";
import { mountMemory, unmountMemory } from "./memory.js";
import { mountOperations, unmountOperations } from "./operations.js";
import { mountProviders } from "./providers.js";
import { mountSetup, unmountSetup } from "./setup.js";

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (value) =>
	String(value ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const n = (value) => Number(value || 0).toLocaleString();
const human = (value) =>
	String(value || "Not recorded")
		.replaceAll("_", " ")
		.replace(/\b\w/g, (x) => x.toUpperCase());
const dateValue = (v) =>
	v
		? new Date(/[zZ]|[+-]\d\d:\d\d$/.test(v) ? v : v.replace(" ", "T") + "Z")
		: null;
const dateText = (v) => {
	const d = dateValue(v);
	return d && !isNaN(d)
		? d.toLocaleString(undefined, {
				month: "short",
				day: "numeric",
				hour: "2-digit",
				minute: "2-digit",
			})
		: "Not recorded";
};
const fullDate = (v) => {
	const d = dateValue(v);
	return d && !isNaN(d) ? d.toLocaleString() : "Not recorded";
};
const bytes = (b) =>
	b >= 1048576
		? `${(b / 1048576).toFixed(1)} MB`
		: `${(b / 1024).toFixed(1)} KB`;
const paths = {
	grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
	briefcase:
		'<rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12a22 22 0 0 0 18 0M10 13h4"/>',
	send: '<path d="m21 3-6.5 18-4-7.5L3 9.5 21 3ZM10.5 13.5 21 3"/>',
	activity: '<path d="M2 12h5l3-8 4 16 3-8h5"/>',
	layers: '<path d="m12 3 10 5-10 5L2 8l10-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
	database:
		'<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 4 18 4 18 0V5M3 12c0 4 18 4 18 0"/>',
	settings:
		'<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
	lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3"/>',
	moon: '<path d="M20.8 13.2A9 9 0 0 1 10.8 3.2 9 9 0 1 0 20.8 13.2Z"/>',
	menu: '<path d="M3 6h18M3 12h18M3 18h18"/>',
	search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
	refresh:
		'<path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/>',
	close: '<path d="m6 6 12 12M18 6 6 18"/>',
	check: '<path d="m5 12 4 4L19 6"/>',
	arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
	alert: '<path d="m12 3 10 18H2L12 3Zm0 6v5m0 3v.1"/>',
	spark:
		'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z"/>',
	download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
	external: '<path d="M14 3h7v7m0-7L10 14M10 3H3v18h18v-7"/>',
	clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
	people:
		'<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 5"/>',
	pause: '<path d="M8 5v14M16 5v14"/>',
	file: '<path d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8m-8 4h6"/>',
};
function icon(name) {
	return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.activity}</svg>`;
}
function icons(root = document) {
	$$("[data-icon]", root).forEach((e) => {
		e.innerHTML = icon(e.dataset.icon);
		e.removeAttribute("data-icon");
	});
}
function href(value) {
	try {
		const u = new URL(value);
		return ["http:", "https:"].includes(u.protocol) &&
			!u.username &&
			!u.password
			? u.href
			: null;
	} catch {
		return null;
	}
}
function external(url, label) {
	const safe = href(url);
	return safe
		? `<a class="btn btn-sm" href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(label)} ${icon("external")}</a>`
		: "";
}
function tone(state) {
	if (/fail|reject|quarantin|broken|uncertain/.test(state || "")) return "red";
	if (/human|manual|verif|pending|draft|degrad|approved/.test(state || ""))
		return "orange";
	if (/applied|sent|ready|ok|offer|hired/.test(state || "")) return "green";
	return "blue";
}
function badge(v) {
	return `<span class="badge ${tone(v)}">${esc(human(v))}</span>`;
}
function empty(title, note, name = "search") {
	return `<div class="empty-state"><div class="empty-icon">${icon(name)}</div><h3>${esc(title)}</h3><p>${esc(note)}</p></div>`;
}
function stat(title, value, foot, name, highlight = false) {
	return `<div class="stat-card${highlight ? " highlight" : ""}"><div class="stat-top">${esc(title)}${icon(name)}</div><div class="stat-value">${esc(value)}</div><div class="stat-foot">${foot}</div></div>`;
}
function setHTML(id, html) {
	const el = typeof id === "string" ? $(id) : id;
	if (!el || el._lastMarkup === html) return;
	const active = document.activeElement;
	const focus = el.contains(active)
		? ["job", "outreach", "cancel", "page"]
				.map((k) => [k, active.dataset?.[k]])
				.find(([, v]) => v !== undefined)
		: null;
	const detailKey = (d) =>
		d.id || d.querySelector(":scope > summary")?.textContent.trim();
	const details = new Map($$("details", el).map((d) => [detailKey(d), d.open]));
	el.innerHTML = html;
	el._lastMarkup = html;
	$$("details", el).forEach((d) => {
		if (details.has(detailKey(d))) d.open = details.get(detailKey(d));
	});
	if (focus)
		$$(`[data-${focus[0]}]`, el)
			.find((e) => e.dataset[focus[0]] === focus[1])
			?.focus({ preventScroll: true });
}
function text(id, value) {
	const el = $(id);
	if (el) el.textContent = value;
}
function options(values, selected, first) {
	return (
		`<option value="">${esc(first)}</option>` +
		values
			.map(
				(v) =>
					`<option value="${esc(v)}"${v === selected ? " selected" : ""}>${esc(human(v))}</option>`,
			)
			.join("")
	);
}
function localGet(k, fallback = null) {
	try {
		return JSON.parse(localStorage.getItem(k)) ?? fallback;
	} catch {
		return fallback;
	}
}
function localSet(k, v) {
	try {
		localStorage.setItem(k, JSON.stringify(v));
	} catch {
		/* Private browser mode may disable persistence. */
	}
}
function persistFilters() {
	localSet("ap-job-filters", jobFilters);
	renderActiveFilters();
}
function query(obj) {
	return new URLSearchParams(
		Object.entries(obj).filter(
			([, v]) => v !== "" && v !== undefined && v !== null,
		),
	).toString();
}
const defaultJobs = {
	q: "",
	view: "all",
	state: "",
	source: "",
	min_score: "",
	max_score: "",
	company: "",
	from: "",
	to: "",
	sort: "newest",
	page: 1,
	page_size: 25,
};
let jobFilters = { ...defaultJobs };
const saved = localGet("ap-job-filters", {});
for (const k of Object.keys(defaultJobs))
	if (["string", "number"].includes(typeof saved[k])) jobFilters[k] = saved[k];
const outFilters = { q: "", state: "", channel: "", page: 1 };
const actFilters = { q: "", kind: "all" };
let csrf = null,
	route = "",
	generation = 0,
	busy = false,
	needsRefresh = false,
	timer,
	toastTimer,
	settingsDirty = false;
let engineOverview = null;
let workspaceBrief = null,
	navigationUI = null;
let liveConnection = null,
	liveStatus = null,
	runtimeControlReport = null;
let operationsSummary = null,
	summary = null,
	settings = null,
	runtime = null,
	jobsResponse = null,
	overviewRecent = null,
	lastOutreach = null,
	seriesVisible = ["discovered", "submitted", "outreach"];
let providersView = null;
const chartDays = [7, 30, 90, 365].includes(localGet("ap-chart-days", 30))
	? localGet("ap-chart-days", 30)
	: 30;
let queuePendingTask = null,
	queueSaveNotice = "";
let queueState = null,
	outreachControls = null,
	queueForm = null,
	runtimeTimer = null,
	runtimeBusy = false,
	controlError = "",
	navigationVersion = 0,
	sessionRefresh = null;
let readOnly = false,
	detailReturn = null,
	controlBusy = false,
	lastRouteHash = location.hash,
	runtimeExpiry;
const titles = {
	capacity: [
		"Workers & buffers",
		"WORKLOAD SETTINGS",
		"Change worker capacity and preparation buffer limits.",
	],
	providers: [
		"AI providers",
		"PROVIDER & CREDENTIAL HEALTH",
		"Find unique errors, check individual credentials, and choose providers for future requests.",
	],
	workers: [
		"Workers",
		"EXECUTION, NOT JUST HEARTBEATS",
		"See the current task, observed wait, and last checkpoint for each reporting worker.",
	],
	operations: [
		"Operations",
		"RUN HEALTH & DIAGNOSTICS",
		"Failures, warnings and progress across your actual workers. Separate from job history.",
	],
	backups: [
		"Backups & recovery",
		"YOUR WORK, KEPT SAFE",
		"Readable Git history. Encrypted database snapshots. A way back.",
	],
	diary: [
		"Impact diary",
		"REMEMBER THE WORK",
		"Write what happened. Keep the original. Turn it into grounded evidence.",
	],
	setup: [
		"Set up your workspace",
		"LET’S GET YOU READY",
		"Bring your experience. Set your preferences. Stay in control.",
	],
	connections: [
		"Connections",
		"YOUR EXISTING TOOLS, CONNECTED",
		"Model gateway, mailbox, browser, and source settings.",
	],
	evidence: [
		"My material",
		"YOUR EXISTING WORKSPACE",
		"Files, public work and impact history in one place.",
	],
	overview: [
		"Work & outcomes",
		"YOUR JOB SEARCH, IN FOCUS",
		"Applications and referrals first. See what ran, what succeeded, and what needs fixing.",
	],
	profile: [
		"My profile",
		"YOUR FACTS, YOUR CONTROL",
		"Edit what changed, review its effect, then apply it.",
	],
	searches: [
		"Search preferences",
		"WHERE TO LOOK NEXT",
		"Adjust your search without repeating setup.",
	],
	outcomes: [
		"Results",
		"PROGRESS THAT MATTERS",
		"Recorded responses, interviews and offers—separate from system diagnostics.",
	],
	jobs: [
		"Find your next opportunity",
		"YOUR JOBS",
		"Explore your matches, review the details, and follow every application.",
	],
	outreach: [
		"Outreach",
		"OUTREACH",
		"Messages, replies and scheduled requests.",
	],
	activity: [
		"Activity",
		"WORK & ACCOUNT HISTORY",
		"Job progress, account setup and message states. Current queue counts are on Overview.",
	],
	sources: [
		"Sources",
		"BEHIND THE SCENES",
		"See what is working, what is waiting, and where jobs come from.",
	],
	system: [
		"Database & diagnostics",
		"THE WHOLE PICTURE",
		"Real records. Clear definitions. No inflated success numbers.",
	],
	settings: [
		"Settings",
		"WORKSPACE",
		"Accounts, search preferences and outreach limits.",
	],
};

async function api(
	path,
	{ method = "GET", body, signal, tokenRetried = false } = {},
) {
	const headers = { Accept: "application/json" };
	if (method !== "GET") {
		headers["Content-Type"] = "application/json";
		if (csrf) headers["X-ApplyPilot-CSRF"] = csrf;
	}
	let r;
	try {
		r = await fetch(`/api/${path}`, {
			method,
			headers,
			credentials: "same-origin",
			cache: "no-store",
			body: body === undefined ? undefined : JSON.stringify(body),
			signal: signal || AbortSignal.timeout(20000),
		});
	} catch (e) {
		throw new Error(
			method === "GET"
				? "Could not reach the dashboard. Retrying automatically."
				: "The request was interrupted. Its outcome is not yet confirmed; check current status before retrying.",
		);
	}
	let data;
	try {
		data = await r.json();
	} catch {
		throw new Error(
			"The dashboard returned an unreadable response. Refresh the current status.",
		);
	}
	if (r.status === 401) {
		showAuth();
		throw new Error(
			"Your local session expired. Reopen the private dashboard link.",
		);
	}
	// Only this structured rejection is guaranteed to precede the action. Never
	// automatically replay writes after a timeout, network error or generic 403.
	if (
		r.status === 403 &&
		data.code === "csrf_expired" &&
		method !== "GET" &&
		!tokenRetried
	) {
		if (!sessionRefresh)
			sessionRefresh = api("session")
				.then((value) => {
					if (!value.csrf) throw new Error("Unlock the dashboard again.");
					csrf = value.csrf;
					readOnly = value.read_only;
				})
				.finally(() => {
					sessionRefresh = null;
				});
		await sessionRefresh;
		return api(path, { method, body, signal, tokenRetried: true });
	}
	if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
	return data;
}
async function pollRuntime(force = false) {
	clearTimeout(runtimeTimer);
	if (!csrf || runtimeBusy || document.hidden || (updatesPaused() && !force))
		return;
	runtimeBusy = true;
	try {
		const requestedStatus = liveStatus;
		const reads = [
			api("runtime").then((value) => {
				if (csrf && (force || !updatesPaused()) && !document.hidden)
					acceptRuntime(value);
			}),
		];
		if (force || !liveConnection?.healthy)
			reads.push(
				api("status").then((value) => {
					if (
						csrf &&
						liveStatus === requestedStatus &&
						(force || !updatesPaused()) &&
						!document.hidden
					)
						acceptStatus(value);
				}),
			);
		const results = await Promise.allSettled(reads);
		if (results[0].status === "rejected" && csrf)
			text(
				"#autopilot-control-note",
				"Runtime controls are temporarily unavailable. The last report will expire; no second runtime is started.",
			);
	} finally {
		runtimeBusy = false;
		if (csrf && !document.hidden && !updatesPaused())
			runtimeTimer = setTimeout(pollRuntime, 3000);
	}
}

function notify(msg, error = false) {
	clearTimeout(toastTimer);
	const el = $("#toast");
	el.classList.toggle("error", error);
	text("#toast-message", msg);
	el.hidden = false;
	// Recovery information remains available until dismissed. Routine feedback is quiet.
	if (!error)
		toastTimer = setTimeout(() => {
			if (!el.matches(":hover,:focus-within")) el.hidden = true;
		}, 7000);
}
$("#dismiss-toast").addEventListener("click", () => {
	$("#toast").hidden = true;
});
function showAuth() {
	workerSettings?.dispose();
	workerSettings = null;
	workView?.dispose();
	workView = null;
	workersView?.dispose();
	workersView = null;
	liveConnection?.close();
	liveConnection = null;
	liveStatus = null;
	runtimeControlReport = null;
	engineOverview?.dispose();
	engineOverview = null;
	csrf = null;
	$("#auth-screen").hidden = false;
	$("#app-shell").hidden = true;
	clearTimeout(timer);
	clearTimeout(runtimeTimer);
	clearTimeout(runtimeExpiry);
	if ($("#detail-dialog").open) $("#detail-dialog").close();
}
async function chooseInitialView() {
	try {
		const b = await api("setup/brief");
		workspaceBrief = b;
		if (b.needs_setup && ["", "#overview"].includes(location.hash))
			history.replaceState(null, "", location.pathname + "#setup");
	} catch {
		/* A setup error must not hide the existing dashboard. */
	}
}
function startLiveConnection() {
	liveConnection?.close();
	liveConnection = connectLive({
		onStatus: acceptStatus,
		onDataChanged: () => {
			if (csrf) {
				refresh();
				engineOverview?.refresh();
			}
		},
		onSessionExpired: showAuth,
		onState: (value) => {
			const el = document.querySelector("#stream-status");
			if (el)
				el.textContent =
					value === "live"
						? "Live stream"
						: value === "paused"
							? "Display paused"
							: value === "off"
								? "Disconnected"
								: "Reconnecting · refresh fallback";
		},
	});
}
async function unlock(token) {
	const value = await api("session", { method: "POST", body: { token } });
	csrf = value.csrf;
	readOnly = value.read_only;
	$("#auth-screen").hidden = true;
	$("#app-shell").hidden = false;
	$("#unlock-token").value = "";
	text("#auth-error", "");
	startLiveConnection();
	pollRuntime();
	await chooseInitialView();
	navigate(true);
}
function banner(message) {
	const el = $("#connection-alert");
	el.hidden = !message;
	el.textContent = message || "";
}
function acceptStatus(value) {
	if (
		!csrf ||
		!value?.runtime_id ||
		value.available === false ||
		typeof value.components !== "object"
	)
		return;
	if (liveStatus?.runtime_id === value.runtime_id) {
		const components = { ...liveStatus.components, ...value.components };
		for (const [name, record] of Object.entries(liveStatus.components)) {
			if (record.updated_at > components[name].updated_at)
				components[name] = record;
		}
		value = { ...value, components };
	}
	liveStatus = value;
	displayRuntime(runtimeStatus(value, runtimeControlReport?.control));
	workView?.updateStatus(value);
	workersView?.updateStatus(value);
	engineOverview?.updateStatus(value);
}
function acceptRuntime(value) {
	runtimeControlReport = value;
	displayRuntime(
		liveStatus?.components?.runtime
			? runtimeStatus(liveStatus, value.control)
			: value,
	);
}
function displayRuntime(value) {
	clearTimeout(runtimeExpiry);
	runtime = value;
	runtimeUI();
	if (runtime?.running && !runtime?.stale) {
		// A lost API connection must not leave an indefinitely green cached state.
		const remaining = Math.max(0, 16 - Number(runtime.age_seconds || 0));
		runtimeExpiry = setTimeout(() => {
			if (runtime) {
				runtime = { ...runtime, running: false, stale: true };
				runtimeUI();
			}
		}, remaining * 1000);
	}
}
function runtimeUI() {
	const running = Boolean(runtime?.running && !runtime?.stale);
	$("#sidebar-dot").classList.toggle("running", running);
	const control = runtime?.control || {};
	const phase =
		control.state === "running" && !running
			? "unresponsive"
			: control.state || "checking";
	const names = {
		running: "Autopilot is reporting",
		paused: "Autopilot is paused",
		pausing: "Pausing safely",
		starting: "Starting autopilot",
		stopping: "Stopping autopilot",
		stopped: "Autopilot is stopped",
		unresponsive: "Autopilot status is unverified",
		checking: "Checking autopilot",
		error: "Autopilot needs attention",
		external: "Workspace owned by another runtime",
	};
	const message =
		phase === "unresponsive" && control.state === "running"
			? "The last runtime heartbeat has expired. Current activity is unknown; do not start another copy. Pause or Stop remains available."
			: control.message ||
				runtime?.reason ||
				"Checking current runtime ownership…";
	text("#sidebar-state", names[phase] || human(phase));
	text("#sidebar-note", message);
	text("#autopilot-control-title", names[phase] || human(phase));
	const drain = control.drain || runtime?.drain,
		elapsed = control.pause_elapsed_seconds;
	const pauseNote = ["pausing", "stopping"].includes(phase)
		? `${message}${drain?.waiting_for ? " Waiting for " + drain.waiting_for + "." : ""}${Number.isFinite(elapsed) ? " Requested " + elapsed + "s ago." : ""}`
		: message;
	text("#autopilot-control-note", controlError || pauseNote);
	text(
		"#autopilot-work",
		running
			? `Preparation: ${human(runtime.pipeline?.pipeline_state || "waiting")} · ${runtime.workers?.filter((w) => w.status === "applying").length || 0} workers applying`
			: "",
	);
	text("#autopilot-control-state", human(phase));
	text("#autopilot-summary-state", human(phase));
	const disclosure = $("#autopilot-disclosure");
	if (
		disclosure.dataset.state !== phase &&
		["paused", "stopped", "error", "unresponsive"].includes(phase)
	)
		disclosure.open = true;
	disclosure.dataset.state = phase;
	$("#read-only-note").hidden = !readOnly;
	const toggle = $("#autopilot-toggle"),
		pausable =
			(["running", "starting", "unresponsive"].includes(phase) &&
				control.can_pause) ||
			(control.maintenance && control.auto_resume);
	toggle.dataset.action = pausable
		? "pause"
		: phase === "paused" || phase === "error"
			? "resume"
			: "start";
	toggle.innerHTML =
		control.maintenance && control.auto_resume
			? "Ⅱ Keep autopilot paused"
			: phase === "stopping"
				? "Stopping…"
				: phase === "pausing"
					? "Ⅱ Pausing…"
					: pausable
						? "Ⅱ Pause autopilot"
						: phase === "paused"
							? "▶ Resume autopilot"
							: phase === "error"
								? "▶ Retry autopilot"
								: "▶ Start autopilot";
	toggle.disabled =
		readOnly ||
		controlBusy ||
		["stopping", "checking", "external"].includes(phase) ||
		(phase === "pausing" && !control.auto_resume) ||
		(!pausable && !control.can_start);
	toggle.title = readOnly
		? "Automation controls are disabled in this read-only workspace"
		: toggle.disabled
			? "Review the status explanation before starting"
			: "";
	toggle.setAttribute(
		"aria-busy",
		String(controlBusy || phase === "pausing" || phase === "stopping"),
	);
	const stop = $("#autopilot-stop");
	stop.hidden = !(control.managed || control.can_stop);
	stop.disabled = readOnly || controlBusy || !control.can_stop;
	text(
		"#autopilot-lifetime",
		control.lifetime === "dashboard"
			? "Started here: closing this dashboard server stops its controller after work drains. Closing a browser tab does not."
			: control.lifetime === "independent"
				? "Independent controller: closing this viewer does not stop it. Use Stop controller to exit it."
				: "",
	);
	const recoveryLink = $("#autopilot-setup-link");
	recoveryLink.hidden = !["stopped", "error"].includes(phase);
	recoveryLink.href = phase === "error" ? "#operations" : "#setup?step=launch";
	recoveryLink.textContent =
		phase === "error" ? "View error details" : "Review setup";
	if (route === "sources") renderRuntimeDetails();
	workView?.updateRuntime(runtime);
	engineOverview?.updateRuntime(runtime);
	if (summary) text("#nav-jobs", n(summary.totals.jobs));
	const count = summary?.totals.pending_requests || 0;
	text("#nav-pending", n(count));
	$("#nav-pending").hidden = !count;
	const alert = $("#outreach-alert");
	alert.hidden = !count || route === "outreach";
	if (count)
		setHTML(
			alert,
			`<strong>${n(count)} initial outreach ${count === 1 ? "request is" : "requests are"} awaiting dispatch.</strong> <a href="#outreach">Preview or cancel in Outreach →</a> Applications continue in parallel.`,
		);
}

function online(success) {
	const paused = updatesPaused();
	$("#live-label").classList.toggle("is-live", success && !paused);
	$("#live-label").setAttribute("aria-pressed", String(paused));
	$("#live-label").setAttribute(
		"aria-label",
		paused
			? "Updates paused — resume automatic display updates"
			: success
				? "Live updates — pause automatic display updates"
				: "Updates interrupted — pause automatic display updates",
	);
	setHTML(
		"#live-label",
		`<span class="status-dot"></span>${paused ? "Updates paused" : success ? "Live updates" : "Updates interrupted"}`,
	);
	if (success)
		text(
			"#last-updated",
			`Updated ${new Date().toLocaleTimeString()} · daily totals in UTC`,
		);
}
function setUpdatesPaused(paused) {
	document.documentElement.dataset.updatesPaused = String(paused);
	clearTimeout(timer);
	clearTimeout(runtimeTimer);
	$("#updates-notice").hidden = !paused;
	online(Boolean(summary));
	window.dispatchEvent(
		new CustomEvent("applypilot:updates", { detail: { paused } }),
	);
	if (!paused) {
		pollRuntime();
		requestRefresh();
		engineOverview?.refresh();
	}
}
$("#live-label").addEventListener("click", () =>
	setUpdatesPaused(!updatesPaused()),
);
$("#resume-updates").addEventListener("click", () => setUpdatesPaused(false));

function shell() {
	providersView?.dispose();
	providersView = null;
	workView?.dispose();
	workView = null;
	workersView?.dispose();
	workersView = null;
	pagePanels = null;
	const [title, eyebrow, subtitle] = titles[route];
	text("#page-title", title);
	text(
		"#breadcrumb-page",
		{ overview: "Work & outcomes", jobs: "Jobs", setup: "Setup guide" }[
			route
		] || title,
	);
	text("#page-eyebrow", eyebrow);
	text("#page-subtitle", subtitle);
	document.title = `${title} · ApplyPilot`;
	$$("[data-view]").forEach((a) => {
		a.classList.toggle("active", a.dataset.view === route);
		if (a.dataset.view === route) a.setAttribute("aria-current", "page");
		else a.removeAttribute("aria-current");
	});
	navigationUI?.close(false);
	document.body.dataset.view = route;
	for (const group of $$(".nav-group"))
		if (group.querySelector("[aria-current=page]")) group.open = true;
	engineOverview?.dispose();
	engineOverview = null;
	workerSettings?.dispose();
	workerSettings = null;
	outreachControls?.dispose();
	outreachControls = null;
	queueForm?.destroy();
	queueForm = null;
	settingsDirty = false;
	if (route === "capacity") {
		setHTML(
			"#page",
			'<div class="setup-stack"><div id="worker-settings"></div><div id="queue-control"></div></div>',
		);
		workerSettings = mountWorkerSettings($("#worker-settings"), {
			api,
			notify,
			readOnly,
			onChange: (dirty) => (settingsDirty = dirty),
		});
		renderQueue();
		return;
	}
	if (route === "workers") {
		$("#page")._lastMarkup = null;
		workersView = mountWorkers($("#page"), { api, notify, readOnly });
		if (liveStatus) workersView.updateStatus(liveStatus);
		return;
	}
	if (route === "providers") {
		$("#page")._lastMarkup = null;
		providersView = mountProviders($("#page"), { api, notify, readOnly });
		return;
	}
	if (route === "operations") {
		$("#page")._lastMarkup = null;
		mountOperations($("#page"), { api, notify, readOnly });
		return;
	}
	if (["backups", "diary"].includes(route)) {
		$("#page")._lastMarkup = null;
		mountMemory($("#page"), { api, notify, view: route, readOnly });
		return;
	}
	if (
		["setup", "connections", "evidence", "profile", "searches"].includes(route)
	) {
		$("#page")._lastMarkup = null;
		mountSetup($("#page"), { api, notify, view: route, readOnly });
		return;
	}
	if (route === "overview") {
		setHTML(
			"#page",
			`<div id="work-main"></div><details class="comfort-disclosure engine-disclosure" id="overview-engine-details"><summary>Engine detail & diagnostics <span>Queues, sources, allocation and recorder evidence</span></summary><p><a class="btn btn-sm" href="#operations">Operations & exports</a> <a class="btn btn-sm" href="#activity">Earlier account & job history</a> <a class="btn btn-sm" href="#system">Database diagnostics</a></p><div id="overview-engine"></div></details>`,
		);
		workView = mountWork($("#work-main"), { api, onOpenJob: openJob, notify });
		workView.updateRuntime(runtime);
		if (liveStatus) workView.updateStatus(liveStatus);
		$("#overview-engine-details").addEventListener("toggle", () => {
			if (!$("#overview-engine-details")?.open || engineOverview) return;
			engineOverview = mountEngineOverview($("#overview-engine"), {
				api,
				onOpenJob: openJob,
			});
			if (liveStatus) engineOverview.updateStatus(liveStatus);
			engineOverview.updateRuntime(runtime);
			engineOverview.updateQueue(queueState);
			engineOverview.updateSettings(settings);
			api("settings")
				.then((value) => {
					settings = value;
					engineOverview?.updateSettings(value);
				})
				.catch(() => {});
			api("queue")
				.then((value) => {
					queueState = value;
					engineOverview?.updateQueue(value);
				})
				.catch(() => {});
		});
		return;
	} else if (route === "jobs") {
		setHTML(
			"#page",
			`<div class="list-toolbar"><div class="pills" role="group" aria-label="Jobs view">${[
				["all", "All jobs"],
				["ready", "Ready"],
				["submitted", "Submitted"],
				["attention", "Needs attention"],
			]
				.map(
					([v, l]) =>
						`<button data-job-view="${v}" class="${jobFilters.view === v ? "selected" : ""}" aria-pressed="${jobFilters.view === v}">${l}</button>`,
				)
				.join(
					"",
				)}</div><button id="export-jobs" class="btn" aria-label="Export CSV" title="Export filtered jobs as CSV">${icon("download")}<span>Export CSV</span></button></div><section class="card"><form id="job-filters"><div class="toolbar job-filter-bar"><label class="filter-field filter-search"><span>Search opportunities</span><span class="search-field">${icon("search")}<input name="q" value="${esc(jobFilters.q)}" maxlength="200" placeholder="Job title, company or location"></span></label><button class="btn mobile-filter-button" type="button" id="toggle-job-filters" aria-expanded="false" aria-controls="job-filter-options job-advanced-filters">${icon("settings")}Filters</button><div class="filter-options" id="job-filter-options"><label class="filter-field"><span>Status</span><select name="state" id="state-filter">${options([], jobFilters.state, "All states")}</select></label><label class="filter-field"><span>Source</span><select name="source" id="source-filter">${options([], jobFilters.source, "All sources")}</select></label><label class="filter-field"><span>Minimum fit</span><select name="min_score" aria-label="Minimum fit score">${options(
				Array.from({ length: 11 }, (_, i) => String(i)),
				String(jobFilters.min_score),
				"Any score",
			)}</select></label><button type="button" class="btn btn-quiet" id="reset-filters">Clear filters</button></div></div><details class="advanced" id="job-advanced-filters"><summary>More filters & sorting</summary><div class="advanced-fields"><label>Company<input name="company" value="${esc(jobFilters.company)}" maxlength="200" placeholder="Company contains…"></label><label>Discovered from (UTC)<input type="date" name="from" value="${esc(jobFilters.from)}"></label><label>Discovered to (UTC)<input type="date" name="to" value="${esc(jobFilters.to)}"></label><label>Maximum fit score<select name="max_score">${options(
				Array.from({ length: 11 }, (_, i) => String(i)),
				String(jobFilters.max_score),
				"Any score",
			)}</select></label><label>Sort by<select name="sort">${[
				["newest", "Newest first"],
				["oldest", "Oldest first"],
				["score", "Highest fit"],
				["company", "Company"],
				["title", "Job title"],
				["applied", "Latest submission"],
			]
				.map(
					([v, l]) =>
						`<option value="${v}"${jobFilters.sort === v ? " selected" : ""}>${l}</option>`,
				)
				.join(
					"",
				)}</select></label></div></details></form><div id="active-job-filters" class="active-filters" hidden></div><div class="filter-caption" id="jobs-caption" role="status" aria-live="polite" aria-atomic="true">Loading records…</div><div id="jobs-list"></div><div id="jobs-pagination"></div></section><p class="page-note">Ready means the recorded state is ready_to_apply; active claims, eligibility, and ordinary safety controls can still affect dispatch. CSV exports honor these filters, up to 10,000 rows.</p>`,
		);
		const form = $("#job-filters");
		let debounce;
		$("#toggle-job-filters").addEventListener("click", () => {
			const button = $("#toggle-job-filters");
			button.setAttribute(
				"aria-expanded",
				String(button.getAttribute("aria-expanded") !== "true"),
			);
			syncJobFilterLayout();
		});
		syncJobFilterLayout();
		const apply = () => {
			for (const [k, v] of new FormData(form)) jobFilters[k] = v;
			jobFilters.page = 1;
			persistFilters();
			requestRefresh();
		};
		form.addEventListener("submit", (e) => {
			e.preventDefault();
			clearTimeout(debounce);
			apply();
		});
		form.addEventListener("input", (e) => {
			if (e.target.tagName === "INPUT" && e.target.type !== "date") {
				clearTimeout(debounce);
				debounce = setTimeout(apply, 300);
			}
		});
		form.addEventListener("change", () => {
			clearTimeout(debounce);
			apply();
		});
		$$("[data-job-view]").forEach((b) =>
			b.addEventListener("click", () => {
				jobFilters.view = b.dataset.jobView;
				jobFilters.page = 1;
				persistFilters();
				syncJobViews();
				requestRefresh();
			}),
		);
		$("#reset-filters").addEventListener("click", () => clearAllJobFilters());
		$("#export-jobs").addEventListener("click", exportJobs);
	} else if (route === "outreach") {
		setHTML(
			"#page",
			`<div id="outreach-controls"></div><div id="outreach-notice"></div><div class="stats-grid" id="outreach-stats"></div><div class="section-heading"><div><h2>Scheduled requests</h2><p>Preview or cancel before the sending window closes.</p></div></div><div id="pending-requests"></div><section class="card"><div class="card-head"><div><h2>Messages & requests</h2><p>Initial outreach, replies, and follow-ups across channels</p></div><button class="btn btn-sm" type="button" id="open-outreach-controls">Outreach settings</button></div><form class="toolbar" id="outreach-filters"><label class="search-field">${icon("search")}<input name="q" maxlength="200" value="${esc(outFilters.q)}" aria-label="Search outreach" placeholder="Search person, company, job…"></label><select name="state" aria-label="Outreach status">${options(["draft", "approved", "sending", "sent", "uncertain", "cancelled", "failed", "deferred"], outFilters.state, "All statuses")}</select><select name="channel" aria-label="Outreach channel">${options(["email", "linkedin", "x", "manual"], outFilters.channel, "All channels")}</select></form><div id="outreach-list"></div><div id="outreach-pagination"></div></section>`,
		);
		const page = $("#page"),
			controls = $("#outreach-controls");
		const requestNodes = [...page.children].filter((n) => n !== controls);
		pagePanels = panelSwitch(
			page,
			[
				{ key: "requests", label: "Requests & messages", nodes: requestNodes },
				{ key: "controls", label: "Outreach settings", nodes: [controls] },
			],
			{
				initial:
					new URLSearchParams(location.hash.split("?")[1] || "").get("panel") ||
					"requests",
				label: "Outreach sections",
			},
		);
		$("#open-outreach-controls").onclick = () =>
			pagePanels.show("controls", { focus: true });
		bindFilters("#outreach-filters", outFilters);
		outreachControls = mountOutreachControls($("#outreach-controls"), {
			api,
			notify,
			readOnly,
			onChange: (value) => (settingsDirty = value),
		});
	} else if (route === "activity") {
		setHTML(
			"#page",
			`<section class="card"><div class="card-head"><div><h2>Recent activity</h2><p>Latest 100 matching records, including the latest 12 saved account/setup tasks</p></div>${icon("activity")}</div><form class="toolbar" id="activity-filters"><label class="search-field">${icon("search")}<input name="q" maxlength="200" value="${esc(actFilters.q)}" aria-label="Search activity" placeholder="Search job, company or account task…"></label><select name="kind" aria-label="Activity type">${[
				["all", "Work & account activity"],
				["pipeline", "Job progress"],
				["setup", "Account & setup"],
				["outcome", "Recorded outcomes"],
				["outreach", "Message statuses"],
				["snapshot", "Imported state snapshots"],
				["recheck", "Repeated state checks"],
			]
				.map(
					([v, l]) =>
						`<option value="${v}"${actFilters.kind === v ? " selected" : ""}>${l}</option>`,
				)
				.join(
					"",
				)}</select></form><p id="activity-error" class="notice notice-warning" role="alert" hidden></p><p id="activity-updated" class="fine-print">Loading activity…</p><div id="activity-list"></div></section><p class="page-note">Activity records what happened; Overview counts currently eligible work. “Enriched” means details saved, not a fit approval or an application. Imported snapshots and repeated state checks have separate filters. Message rows show each message’s latest state, not every change. Dates use your browser’s local timezone.</p>`,
		);
		bindFilters("#activity-filters", actFilters);
	} else if (route === "sources") {
		setHTML(
			"#page",
			`<div id="runtime-banner"></div><section class="card section-space"><div class="card-head"><div><h2>Current worker report</h2><p>Only a fresh, matching runtime heartbeat appears here</p></div><span id="worker-count" class="badge">—</span></div><div id="workers"></div></section><div class="two-col equal-col"><section class="card"><div class="card-head"><div><h2>Current preparation report</h2><p>Unknown until this runtime reports; zero is not a substitute for missing data</p></div></div><div class="metric-list" id="cycle-metrics"></div><div id="historical-runtime"></div></section><section class="card"><div class="card-head"><div><h2>Stored jobs by source</h2><p>Database totals · all time</p></div></div><div id="source-totals" class="card-content"></div></section></div><section class="card"><div class="card-head"><div><h2>Source health</h2><p>Last recorded discovery run per provider; not lifetime totals</p></div></div><div id="source-health"></div></section>`,
		);
	} else if (route === "outcomes") {
		setHTML(
			"#page",
			`<div id="results-summary" class="stats-grid"></div><section class="card"><div class="card-head"><div><h2>Recorded results</h2><p>All time · events and distinct jobs, kept separate</p></div><a class="btn btn-sm" href="#activity">View history ${icon("arrow")}</a></div><div id="results-list"></div></section><p class="page-note">One job can have several results. These records do not prove that outreach caused an interview or offer.</p>`,
		);
	} else if (route === "system") {
		setHTML(
			"#page",
			`<div class="stats-grid" id="database-stats"></div><div class="two-col equal-col"><section class="card"><div class="card-head"><div><h2>Database inventory</h2><p>Selected operational tables · all time</p></div></div><div class="metric-list" id="database-counts"></div></section><section class="card"><div class="card-head"><div><h2>Recorded outcomes</h2><p>Unvoided events and distinct jobs · all time</p></div></div><div id="outcome-totals"></div></section></div><div class="two-col equal-col"><section class="card"><div class="card-head"><div><h2>Open incidents</h2><p>Latest 50 unresolved records; workers handle routine retries</p></div></div><div id="incidents"></div></section><section class="card"><div class="card-head"><div><h2>Background task checkpoints</h2><p>Persisted schedules, retries, and last successful completion</p></div></div><div id="task-checkpoints"></div></section></div>`,
		);
	} else if (route === "settings") {
		setHTML(
			"#page",
			`<div class="settings-layout"><section class="card"><div class="card-head"><h2>Workspace</h2></div><nav class="settings-nav" aria-label="Workspace settings"><a href="#profile"><strong>Profile & material</strong><span>Résumé, experience and supporting work</span></a><a href="#searches"><strong>Search preferences</strong><span>Roles, markets, sources and throughput</span></a><a href="#connections"><strong>Connections</strong><span>Email, social accounts and integrations</span></a><a href="#capacity"><strong>Workers & buffers</strong><span>Edit worker and preparation limits</span></a><a href="#backups"><strong>Backups & recovery</strong><span>Snapshots and restore options</span></a></nav><div class="card-content"><button class="btn btn-quiet" id="lock-workspace" type="button">Lock this browser session</button></div></section><div id="outreach-controls"></div></div>`,
		);
		outreachControls = mountOutreachControls($("#outreach-controls"), {
			api,
			notify,
			readOnly,
			onChange: (value) => (settingsDirty = value),
		});
		$("#lock-workspace").addEventListener("click", async () => {
			try {
				await api("logout", { method: "POST", body: {} });
				showAuth();
			} catch (e) {
				notify(e.message, true);
			}
		});
	}
}
function bindFilters(selector, model) {
	const form = $(selector);
	let debounce;
	const apply = () => {
		for (const [k, v] of new FormData(form)) model[k] = v;
		if ("page" in model) model.page = 1;
		requestRefresh();
	};
	form.addEventListener("submit", (e) => {
		e.preventDefault();
		clearTimeout(debounce);
		apply();
	});
	form.addEventListener("input", (e) => {
		if (e.target.tagName === "INPUT") {
			clearTimeout(debounce);
			debounce = setTimeout(apply, 300);
		}
	});
	form.addEventListener("change", () => {
		clearTimeout(debounce);
		apply();
	});
}

function syncJobFilterLayout() {
	const button = $("#toggle-job-filters"),
		options = $("#job-filter-options"),
		advanced = $("#job-advanced-filters");
	if (!button) return;
	const mobile = matchMedia("(max-width:760px)").matches,
		expanded = button.getAttribute("aria-expanded") === "true";
	button.hidden = !mobile;
	options.hidden = mobile && !expanded;
	advanced.hidden = mobile && !expanded;
}
matchMedia("(max-width:760px)").addEventListener("change", syncJobFilterLayout);
function clearAllJobFilters() {
	jobFilters = { ...defaultJobs };
	persistFilters();
	const form = $("#job-filters");
	if (form)
		for (const [key, value] of Object.entries(jobFilters)) {
			if (form.elements[key]) form.elements[key].value = value;
		}
	syncJobViews();
	requestRefresh();
	form?.elements.q.focus();
}
function syncJobViews() {
	$$("[data-job-view]").forEach((b) => {
		const selected = b.dataset.jobView === jobFilters.view;
		b.classList.toggle("selected", selected);
		b.setAttribute("aria-pressed", String(selected));
	});
}
function renderActiveFilters() {
	const root = $("#active-job-filters");
	if (!root) return;
	const names = {
		q: "Search",
		state: "Status",
		source: "Source",
		min_score: "Min fit",
		max_score: "Max fit",
		company: "Company",
		from: "From",
		to: "To",
	};
	const active = Object.entries(names).filter(
		([key]) => jobFilters[key] !== "" && jobFilters[key] != null,
	);
	root.hidden = !active.length;
	setHTML(
		root,
		active
			.map(
				([key, label]) =>
					`<button type="button" class="filter-chip" data-clear-filter="${key}" aria-label="Remove ${esc(label.toLowerCase())} filter: ${esc(jobFilters[key])}">${esc(label)}: ${esc(key === "state" ? human(jobFilters[key]) : jobFilters[key])} ${icon("close")}</button>`,
			)
			.join(""),
	);
}

function jobRows(items, compact = false) {
	if (!items.length) {
		const filtered =
			route === "jobs" &&
			(jobFilters.view !== "all" ||
				[
					"q",
					"state",
					"source",
					"min_score",
					"max_score",
					"company",
					"from",
					"to",
				].some((k) => jobFilters[k] !== ""));
		return `<div class="empty-state"><div class="empty-icon">${icon("briefcase")}</div><h3>${filtered ? "No jobs match these filters" : "Your next opportunity starts here"}</h3><p>${filtered ? "Try a broader search or clear your filters. Your saved jobs have not been removed." : "Finish your profile and search preferences, then start Autopilot to discover jobs. Nothing is submitted by opening this page."}</p>${filtered ? '<button class="btn btn-primary" data-clear-all-filters>Clear all filters</button>' : '<a class="btn btn-primary" href="#setup">Review setup ' + icon("arrow") + "</a>"}</div>`;
	}
	return `<div class="table-scroll jobs-scroll" tabindex="0" role="region" aria-label="${compact ? "Recent opportunities" : "Job search results"}"><table class="jobs-table ${compact ? "compact-jobs" : ""}" role="table"><caption class="sr-only">Saved jobs with fit assessments and application status</caption><thead role="rowgroup"><tr role="row"><th scope="col">Opportunity</th>${compact ? "" : '<th scope="col">Location</th><th scope="col">Source</th>'}<th scope="col">Fit</th><th scope="col">Status</th>${compact ? "" : '<th scope="col">Discovered</th>'}</tr></thead><tbody role="rowgroup">${items.map((j) => `<tr role="row"><td role="cell" class="opportunity-cell"><div class="company-cell"><span class="company-avatar" aria-hidden="true">${esc((j.company || "?").slice(0, 1).toUpperCase())}</span><div><button class="job-link" data-job="${esc(j.url)}" aria-label="View ${esc(j.title || "Untitled job")} at ${esc(j.company || "unknown company")}">${esc(j.title || "Untitled job")}</button><span class="cell-secondary">${esc(j.company || "Company not recorded")}</span></div></div></td>${compact ? "" : `<td role="cell" class="location-cell" data-label="Location"><span class="cell-location">${esc(j.location || "Not recorded")}</span></td><td role="cell" class="source-cell" data-label="Source">${esc(j.site || "Unknown")}</td>`}<td role="cell" class="fit-cell" data-label="Fit">${j.fit_score == null ? '<span class="muted">Not scored</span>' : `<span class="score ${Number(j.fit_score) >= 8 ? "high" : ""}">${esc(j.fit_score)}<small>/10</small></span>`}</td><td role="cell" class="state-cell" data-label="Status">${badge(j.state)}</td>${compact ? "" : `<td role="cell" class="discovered-cell" data-label="Discovered"><time title="${esc(fullDate(j.discovered_at))}">${esc(dateText(j.discovered_at))}</time></td>`}</tr>`).join("")}</tbody></table></div>`;
}

function pagination(data, id, model, allowSize = false) {
	const pages = Math.max(1, Math.ceil(data.total / data.page_size));
	model.page = data.page;
	setHTML(
		id,
		`<div class="table-footer"><span>${data.total ? `${n((data.page - 1) * data.page_size + 1)}–${n(Math.min(data.page * data.page_size, data.total))} of ${n(data.total)}` : "No matches"}</span><div class="pagination">${allowSize ? `<select aria-label="Jobs per page" data-page-size>${[25, 50, 100].map((v) => `<option value="${v}"${v === Number(model.page_size) ? " selected" : ""}>${v} / page</option>`).join("")}</select>` : ""}<button class="btn btn-sm" data-page="${data.page - 1}"${data.page <= 1 ? " disabled" : ""}>Previous</button><span>${n(data.page)} / ${n(pages)}</span><button class="btn btn-sm" data-page="${data.page + 1}"${data.page >= pages ? " disabled" : ""}>Next</button></div></div>`,
	);
	$$("[data-page]", $(id)).forEach((b) =>
		b.addEventListener("click", () => {
			model.page = Number(b.dataset.page);
			if (allowSize) persistFilters();
			requestRefresh();
		}),
	);
	$("[data-page-size]", $(id))?.addEventListener("change", (e) => {
		model.page_size = Number(e.target.value);
		model.page = 1;
		persistFilters();
		requestRefresh();
	});
}
function bars(items) {
	const max = Math.max(1, ...items.map((r) => r.value));
	return items
		.map(
			(r) =>
				`<div class="bar-row"><div class="bar-row-top"><span>${esc(r.label)}</span><strong>${n(r.value)}</strong></div><div class="meter"><span style="width:${(100 * r.value) / max}%"></span></div></div>`,
		)
		.join("");
}
function metrics(items) {
	return items
		.map(
			([label, value]) =>
				`<div class="metric-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`,
		)
		.join("");
}

function acceptQueue(value) {
	if (value?.available === true) {
		queueState = value;
		return;
	}
	if (queueState?.limits) {
		if (value?.limits)
			queueState = {
				...queueState,
				limits: value.limits,
				flow_settings: value.flow_settings,
				revision: value.revision,
			};
	} else queueState = value;
}
function renderQueue() {
	const root = $("#queue-control");
	if (!root) return;
	if (!queueState?.limits) {
		if ($("#queue-status")) {
			text("#queue-status", "Status unavailable");
			text(
				"#queue-reason",
				queueState?.reason || "Checking the fresh-work buffer…",
			);
			text(
				"#queue-freshness",
				"Displayed counts are from the last successful check, not verified current work.",
			);
			queueForm?.busy(true);
		} else
			setHTML(
				root,
				`<div class="notice">${esc(queueState?.reason || "Checking the fresh-work buffer…")}</div>`,
			);
		return;
	}
	const q = queueState,
		limits = q.limits;
	if (!$("#queue-status")) {
		setHTML(
			root,
			`<section class="card buffer-card"><div class="card-head"><div><h2>Work buffer</h2><p id="queue-reason"></p></div><span class="badge" id="queue-status"></span></div><div class="card-content"><div id="queue-metrics" class="buffer-metrics"></div><p id="queue-freshness" class="fine-print"></p><details ${route === "capacity" ? "open" : ""}><summary>Buffer & freshness settings</summary><form id="queue-form" class="setup-stack">${backlogFields(q.flow_settings)}<div class="setup-fields"><label class="setup-field">Recheck source after this many days<input name="revalidate_after_days" type="number" min="1" max="90" required></label></div><p class="fine-print">Automatic preparation uses available execution capacity. Manual watermarks are optional. Source revalidation controls freshness, not total opportunity volume.</p><div class="form-actions"><button class="btn btn-primary" type="submit" disabled>Save buffer settings</button><span id="queue-save-status" role="status"></span></div></form></details></div></section>`,
		);
		$("#queue-metrics").insertAdjacentHTML(
			"afterend",
			'<div id="pipeline-buffers"></div>',
		);
		const form = $("#queue-form");
		for (const [key, value] of Object.entries(limits))
			form.elements[key].value = value;
		fillBacklog(form, q.flow_settings);
		form.dataset.revision = q.revision;
		queueForm = trackForm(form, {
			readOnly,
			valid: () => form.checkValidity() && validBacklog(form),
			onChange: (dirty) =>
				text(
					"#queue-save-status",
					queuePendingTask
						? "Applying safely…"
						: queueSaveNotice ||
								(dirty ? "Unsaved changes" : "No unsaved changes"),
				),
		});
		form.addEventListener("input", () => {
			queueSaveNotice = "";
			queueForm?.refresh();
		});
		form.onsubmit = async (e) => {
			e.preventDefault();
			if (readOnly || queuePendingTask || !queueForm.dirty()) return;
			const buffer = Object.fromEntries(
				Object.keys(limits).map((key) => [
					key,
					Number(form.elements[key].value),
				]),
			);
			const tracker = queueForm;
			tracker.busy(true);
			queueSaveNotice = "";
			try {
				const result = await api("queue/settings", {
					method: "POST",
					body: {
						revision: form.dataset.revision,
						buffer,
						...(readBacklog(form) ? { flow_control: readBacklog(form) } : {}),
					},
				});
				if (result.task_id) {
					queuePendingTask = { id: result.task_id, buffer };
					notify(
						"Buffer change accepted. Its completion is shown here after active work drains.",
					);
				} else {
					queueSaveNotice = "Saved";
					if (result.revision) form.dataset.revision = result.revision;
					tracker.reset();
					notify("Buffer settings saved.");
				}
				requestRefresh();
			} catch (e) {
				queueSaveNotice = e.message;
				notify(e.message, true);
			} finally {
				tracker.busy(!!queuePendingTask);
				for (const input of form.querySelectorAll("input"))
					input.disabled = readOnly || !!queuePendingTask;
			}
		};
	}
	text("#queue-status", "Independent discovery inventory");
	text("#queue-reason", q.reason);
	setHTML(
		"#queue-metrics",
		Object.entries(q.stages || {})
			.map(
				([key, value]) =>
					`<div><strong>${n(value)}</strong><span>${esc({ enrich: "Need details", score: "Need scoring", tailor: "Need résumé", cover: "Need cover letter", brief: "Need interview brief", pdf: "Files to convert" }[key])}</span></div>`,
			)
			.join("") +
			`<div><strong>${n(q.actionable)}${q.actionable_capped ? "+" : ""}</strong><span>Application candidates</span></div>`,
	);
	text(
		"#queue-freshness",
		`${n(q.stale)} old or unverified records need a source recheck · ${n(q.closed)} reported closed · ${n(q.total)} stored all time. Checked ${dateText(q.at)}.`,
	);
	const bufferRows = Object.values(q.pipeline_buffers || {});
	setHTML(
		"#pipeline-buffers",
		bufferRows.length
			? `<div class="table-scroll"><table><thead><tr><th scope="col">Producer output buffer</th><th scope="col">Stock counted</th><th scope="col">Reserved</th><th scope="col">Resume / pause</th><th scope="col">Producer status</th></tr></thead><tbody>${bufferRows.map((b) => `<tr><th scope="row">${esc(b.label || b.name)}</th><td>${b.available ? (typeof b.consumable_stock === "number" ? `${n(b.consumable_stock)} runnable<br><small>${n(b.prepared_stock)} prepared</small>` : n(b.stock)) : "—"}</td><td>${b.available ? n(b.reserved) : "—"}</td><td>${b.mode === "automatic" ? "Automatic capacity" : `${n(b.low)} / ${n(b.high)}`}</td><td>${esc(b.reason || "Not observed")}</td></tr>`).join("")}</tbody></table></div><p class="fine-print">${q.pipeline_buffers_running ? "Runtime observation" : "Saved runtime observation"} · ${esc(dateText(q.pipeline_buffers_at))}. Automatic mode uses execution capacity; manual watermarks apply only when selected.</p>`
			: '<p class="fine-print">Producer buffer occupancy appears when preparation starts.</p>',
	);
	const form = $("#queue-form");
	queueForm?.busy(!!queuePendingTask);
	if (form)
		for (const input of form.querySelectorAll("input"))
			input.disabled = readOnly || !!queuePendingTask;
	if (
		form &&
		!queueForm?.dirty() &&
		!queuePendingTask &&
		form.dataset.revision !== q.revision
	) {
		for (const [key, value] of Object.entries(limits))
			form.elements[key].value = value;
		fillBacklog(form, q.flow_settings);
		form.dataset.revision = q.revision;
		queueForm?.reset();
	}
}
function renderOverview(recent) {
	const t = summary.totals;
	const starting =
		workspaceBrief?.needs_setup === true || !summary.database_available;
	const nextHref = starting
		? "#setup"
		: t.attention
			? "#jobs?view=attention"
			: "#jobs";
	const nextTitle = starting
		? "Let’s set up your job search."
		: t.attention
			? `${n(t.attention)} ${t.attention === 1 ? "job needs" : "jobs need"} your review.`
			: "Explore your saved opportunities.";
	const nextCopy = starting
		? "Add your experience and choose what comes next. We’ll guide you through the essentials before anything runs."
		: t.attention
			? `${n(t.attention)} saved ${t.attention === 1 ? "job needs" : "jobs need"} your review. Open a role to see what’s missing and decide your next step.`
			: "Your jobs, application progress and next steps, together. Review any role before opening its original listing.";
	setHTML(
		"#overview-next",
		`<section class="next-move"><div class="next-move-copy"><span class="section-kicker">${starting ? "A STRONG START" : "YOUR NEXT STEP"}</span><h2>${esc(nextTitle)}</h2><p>${esc(nextCopy)}</p><a class="btn btn-primary" href="${nextHref}">${starting ? "Continue setup" : t.attention ? "Review jobs needing attention" : "Explore your jobs"} ${icon("arrow")}</a></div><p class="fine-print">Independent services work concurrently on eligible jobs. Per-job evidence, documents and permissions remain required.</p></section>`,
	);
	const actions = [];
	if (t.attention && starting)
		actions.push(
			`<a class="attention-item" href="#jobs?view=attention"><strong>${n(t.attention)} ${t.attention === 1 ? "job needs" : "jobs need"} a closer look</strong><span>Open affected jobs ${icon("arrow")}</span></a>`,
		);
	if (t.pending_requests)
		actions.push(
			`<a class="attention-item" href="#outreach"><strong>${n(t.pending_requests)} outreach requests awaiting dispatch</strong><span>Preview or cancel before sending ${icon("arrow")}</span></a>`,
		);
	const incidentCount =
		operationsSummary?.unacknowledged_incidents ??
		operationsSummary?.unacknowledged;
	if (incidentCount)
		actions.push(
			`<a class="attention-item" href="#operations"><strong>${n(incidentCount)} retained incident groups to review</strong><span>Same groups as Operations · not a live failure count ${icon("arrow")}</span></a>`,
		);
	setHTML(
		"#overview-hero",
		!summary.database_available
			? `<div class="notice">Start with your profile and search preferences. <a href="#setup">Open setup →</a></div>`
			: actions.length
				? `<section class="attention-inbox" aria-label="Items to review"><h2>Worth a look</h2>${actions.join("")}</section>`
				: t.attention
					? ""
					: '<div class="quiet-status">No jobs or outreach requests are flagged for your review. Connection and backup warnings are shown separately.</div>',
	);
	renderQueue();
	const ready = queueState?.available
		? `${n(queueState.actionable)}${queueState.actionable_capped ? "+" : ""}`
		: "—";
	setHTML(
		"#overview-stats",
		stat(
			"Saved opportunities",
			summary.database_available ? n(t.jobs) : "—",
			'<a href="#jobs">Explore all jobs ' + icon("arrow") + "</a>",
			"briefcase",
		) +
			stat(
				"Ready to apply",
				ready,
				'<a href="#overview?panel=engine">Current application candidates ' +
					icon("arrow") +
					"</a>",
				"layers",
			) +
			stat(
				"Applications submitted",
				summary.database_available ? n(t.submitted) : "—",
				`<span>${n(t.submitted_today)} today · recorded, all time</span>`,
				"check",
			) +
			stat(
				"Needs your attention",
				summary.database_available ? n(t.attention) : "—",
				'<a href="#jobs?view=attention">Review affected jobs ' +
					icon("arrow") +
					"</a>",
				"alert",
			),
	);

	text(
		"#chart-subtitle",
		`${n(summary.period.discovered)} discoveries · ${n(summary.period.submitted)} submissions · last ${chartDays} days, UTC`,
	);
	lineChart($("#momentum-chart"), summary.series, seriesVisible);
	setHTML("#chart-data", chartTable(summary.series));
	const groups = [
		["Discover & enrich", ["discovered", "enriched", "scored"]],
		["Prepare documents", ["tailoring", "tailored", "cover_writing"]],
		["Ready to apply", ["ready_to_apply"]],
		["In progress", ["applying"]],
		[
			"Currently in post-submission states",
			["applied", "responded", "interview", "offer", "rejected", "ghosted"],
		],
		[
			"Needs attention",
			[
				"manual_only",
				"verification_required",
				"verification_quarantined",
				"enrich_failed",
				"score_failed",
				"tailor_failed",
				"cover_failed",
				"apply_failed",
			],
		],
	];
	const seen = new Set(groups.flatMap(([, a]) => a));
	const counts = new Map(summary.stages.map((r) => [r.label, r.value]));
	const distributions = groups.map(([label, states]) => ({
		label,
		value: states.reduce((s, v) => s + (counts.get(v) || 0), 0),
	}));
	const other = summary.stages
		.filter((r) => !seen.has(r.label))
		.reduce((s, r) => s + r.value, 0);
	if (other) distributions.push({ label: "Other states", value: other });
	setHTML(
		"#stage-distribution",
		distributions
			.map(
				(r, i) =>
					`<div class="distribution-row"><div class="distribution-title"><span class="stage-dot stage-${i}"></span>${esc(r.label)}<strong>${n(r.value)}</strong></div><div class="distribution-track"><span style="width:${t.jobs ? (100 * r.value) / t.jobs : 0}%"></span></div></div>`,
			)
			.join(""),
	);
	const candidates = queueState?.available && queueState.next_jobs?.length;
	const visibleJobs = candidates ? queueState.next_jobs : recent?.items || [];
	text(
		"#recent-jobs-title",
		candidates ? "Next application candidates" : "Recent opportunities",
	);
	text(
		"#recent-jobs-note",
		candidates
			? "Pass the current queue checks; final dispatch safeguards still apply."
			: "Recently discovered jobs · open a role to see its details and progress.",
	);
	setHTML(
		"#recent-jobs",
		!candidates && !recent
			? empty(
					"Recent jobs could not load",
					"Use Refresh to try again. Your saved jobs have not been changed.",
					"alert",
				)
			: jobRows(visibleJobs.slice(0, 5), true),
	);
	setHTML(
		"#top-companies",
		summary.top_companies.length
			? bars(summary.top_companies.slice(0, 5))
			: empty(
					"No companies yet",
					"Company counts appear as jobs arrive.",
					"briefcase",
				),
	);
}
function renderResults() {
	const rows = summary.outcomes || [],
		groups = summary.outcome_groups || {};
	const count = (key) => (Object.hasOwn(groups, key) ? n(groups[key]) : "—");
	setHTML(
		"#results-summary",
		stat(
			"Submitted",
			n(summary.totals.submitted),
			"<span>recorded submissions · all time</span>",
			"check",
		) +
			stat(
				"Recruiter responses",
				count("responses"),
				"<span>distinct jobs · not automated receipts</span>",
				"send",
			) +
			stat(
				"Interviews & screens",
				count("interviews"),
				"<span>distinct jobs · invitations or completed events</span>",
				"briefcase",
			) +
			stat(
				"Offers recorded",
				count("offers"),
				"<span>distinct jobs · not necessarily active offers</span>",
				"spark",
			),
	);
	setHTML(
		"#results-list",
		!summary.database_available ||
			(summary.missing_tables || []).includes("application_outcomes")
			? empty(
					"Result records are not available",
					"The outcomes database is not initialized or available. This is not a zero-results report.",
					"database",
				)
			: rows.length
				? `<div class="table-scroll"><table><thead><tr><th scope="col">Result</th><th scope="col">Distinct jobs</th><th scope="col">Recorded events</th></tr></thead><tbody>${rows.map((r) => `<tr><th scope="row">${esc(human(r.label))}</th><td>${n(r.jobs)}</td><td>${n(r.value)}</td></tr>`).join("")}</tbody></table></div>`
				: empty(
						"Your next chapter is taking shape",
						"Responses, interviews and offers will appear when recorded. An empty report does not mean nothing has happened.",
						"check",
					) +
					'<div class="empty-actions"><a class="btn" href="#jobs?view=submitted">Review submitted jobs ' +
					icon("arrow") +
					'</a><a class="text-link" href="#connections?group=mailbox">Check mailbox connection</a></div>',
	);
}
function renderOutreach(data) {
	lastOutreach = data;
	const t = summary.totals,
		counts = new Map(data.counts.map((r) => [r.label, r.value]));
	outreachControls?.update(settings);
	setHTML("#outreach-notice", "");
	setHTML(
		"#outreach-stats",
		stat(
			"Sent, all time",
			n(t.outreach_sent),
			"<span>Recorded sent messages, not attempts</span>",
			"send",
		) +
			stat(
				"Attempts today",
				`${n(t.outreach_attempted_today)} / ${settings.daily_send_limit === -1 ? "No cap" : n(settings.daily_send_limit)}`,
				"<span>Shared across channels · UTC</span>",
				"activity",
			) +
			stat(
				"Pending initial requests",
				n(data.pending.length),
				"<span>Published windows awaiting dispatch</span>",
				"clock",
			) +
			stat(
				"Uncertain sends",
				n(counts.get("uncertain") || 0),
				"<span>Reconcile before another send</span>",
				"alert",
			),
	);
	setHTML(
		"#pending-requests",
		data.pending.length
			? `<div class="pending-grid">${data.pending.map((p) => `<article class="pending-card"><div><h3>${esc(p.name)} <span class="badge">${esc(human(p.channel))}</span></h3><p>${esc(p.company)} · ${esc(p.title)}</p><span class="countdown" data-deadline="${esc(p.send_after)}">Calculating window…</span></div><div class="pending-actions"><button class="btn btn-sm" data-outreach="${esc(p.outbox_id)}">Preview</button><button class="btn btn-danger btn-sm" data-cancel="${esc(p.outbox_id)}"${readOnly ? " disabled" : ""}>Cancel request</button></div></article>`).join("")}</div>`
			: empty(
					"No published requests waiting",
					"A request appears here once its exact draft and cancellation window are recorded. There is nothing you need to approve.",
					"clock",
				),
	);
	setHTML(
		"#outreach-list",
		data.items.length
			? `<div class="table-scroll"><table><thead><tr><th>Person & opportunity</th><th>Channel</th><th>Type</th><th>Status</th><th>Created</th><th><span class="sr-only">Preview</span></th></tr></thead><tbody>${data.items.map((r) => `<tr><td><button class="job-link" data-outreach="${esc(r.id)}">${esc(r.name)}</button><span class="cell-secondary">${esc(r.company)} · ${esc(r.title)}</span></td><td>${esc(human(r.channel))}</td><td>${esc(human(r.kind))}</td><td>${badge(r.state)}</td><td>${esc(dateText(r.created_at))}</td><td><button class="icon-btn" data-outreach="${esc(r.id)}" aria-label="Preview message to ${esc(r.name)}">${icon("arrow")}</button></td></tr>`).join("")}</tbody></table></div>`
			: empty(
					"No messages match",
					"Requests and conversation replies will appear here as the networking services record them.",
					"send",
				),
	);
	pagination(data, "#outreach-pagination", outFilters);
	countdowns();
	runtimeUI();
}
function countdowns() {
	if (updatesPaused()) return;
	$$("[data-deadline]").forEach((el) => {
		const d = dateValue(el.dataset.deadline);
		const seconds = d ? Math.max(0, Math.ceil((d - Date.now()) / 1000)) : 0;
		el.textContent = seconds
			? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} until eligible · cancel before dispatch`
			: "Eligible for dispatch · cancel only before sending starts";
	});
}
function renderActivity(data) {
	const labels = {
		pipeline: "Job progress",
		outcome: "Outcome",
		outreach: "Message status",
		setup: "Account & setup",
		snapshot: "Imported state snapshot",
		recheck: "Repeated state check",
	};
	const titles = { snapshot: "Snapshot: ", recheck: "State unchanged: " };
	const notes = {
		snapshot:
			"Imported from stored fields. This timestamp is the migration, not when the work originally happened.",
		recheck:
			"The recorded state did not change. This is not a new job-state transition.",
	};
	setHTML(
		"#activity-list",
		data.items.length
			? `<div class="activity-list">${data.items
					.map((e) => {
						const setup = e.kind === "setup";
						const target = setup
							? e.event === "gmail_connect"
								? "gmail"
								: e.event === "social_connect"
									? "browser"
									: ""
							: "";
						const detail = setup
							? `<a class="job-link" href="#connections${target ? "?group=" + target : ""}">Account task details</a>`
							: `<button class="job-link" data-job="${esc(e.job_url)}">${esc(e.title)}</button><p>${esc(e.company)}</p>`;
						const result = setup
							? e.authorization === false
								? "Account check did not complete"
								: e.authorization === true
									? "Outreach settings saved"
									: e.saved === true && e.verified === false
										? "Session saved; reader access was not live-verified"
										: e.connected === true
											? "Account connected"
											: human(e.task_state)
							: "";
						return `<div class="activity-item"><span class="activity-icon ${esc(e.kind)}">${icon(setup ? "settings" : e.kind === "outreach" ? "send" : e.kind === "outcome" ? "check" : "layers")}</span><div><h3>${esc((titles[e.kind] || "") + human(e.event))} <span class="muted">· ${esc(labels[e.kind] || e.kind)}</span></h3>${detail}${setup ? `<p>${esc(result)}</p><p class="fine-print">${e.current_build ? "Saved task from this build" : "Saved task from an earlier/unidentified build"} · not a fresh account check</p>` : ""}${notes[e.kind] ? `<p class="fine-print">${esc(notes[e.kind])}</p>` : ""}${e.reason ? `<p class="fine-print">${esc(e.reason.slice(0, 300))}</p>` : ""}</div><time title="${esc(fullDate(e.at))}">${esc(dateText(e.at))}</time></div>`;
					})
					.join("")}</div>`
			: empty(
					"No records in this view",
					"Try a different activity type. Current queue eligibility is shown separately on Overview.",
					"activity",
				),
	);
	const problems = data.warnings || [];
	const error = $("#activity-error");
	if (error) {
		error.hidden = !problems.length;
		error.textContent = problems.join(" ");
	}
	text(
		"#activity-updated",
		`History checked ${dateText(data.at)} · ${n(data.items.length)} displayed records`,
	);
}
function activityError(error) {
	const box = $("#activity-error");
	if (!box) return;
	box.hidden = false;
	box.textContent = `Activity could not update: ${error.message} Any records below are from the last successful check, not a current snapshot.`;
}

function renderRuntimeDetails() {
	const active = Boolean(runtime?.running && !runtime?.stale),
		p = active ? runtime.pipeline || {} : {},
		workers = active ? runtime.workers || [] : [];
	const last =
		runtime?.last_report ||
		(!active && runtime?.at
			? { at: runtime.at, pipeline: runtime.pipeline, workers: runtime.workers }
			: null);
	setHTML(
		"#runtime-banner",
		!active
			? '<div class="notice notice-warning">No verified current runtime report. Worker and preparation panels are unavailable—not zero or idle. Stored jobs and last recorded source health remain available.</div>'
			: "",
	);
	text(
		"#worker-count",
		active ? `${n(workers.length)} reported` : "Not reporting",
	);
	setHTML(
		"#workers",
		workers.length
			? `<div class="worker-grid">${workers.map((w) => `<article class="worker-card"><h3>${esc(human(w.role || "Application"))} ${esc(w.worker_id)}${badge(w.status)}</h3><p>${esc(w.company || "Waiting for work")}</p><strong>${esc(w.job_title || "No job currently reported")}</strong><div class="worker-stats"><span>${n(w.jobs_applied)} applied</span><span>${n(w.jobs_failed)} failed</span><span>${n(w.actions)} actions</span></div></article>`).join("")}</div>`
			: empty(
					active ? "No workers in this report" : "No current worker report",
					active
						? "The runtime has not reported any application worker cards."
						: "The runtime may be stopped, starting or unreachable. Check the lifecycle message above before starting anything.",
					"activity",
				),
	);
	const count = (k) => (Number.isFinite(p[k]) ? n(p[k]) : "Not reported");
	const sources =
		Number.isFinite(p.discovery_total) && p.discovery_total > 0
			? `${count("discovery_done")} / ${n(p.discovery_total)}`
			: "Not reported yet";
	setHTML(
		"#cycle-metrics",
		active
			? metrics([
					["Engine state", human(p.pipeline_state)],
					["Execution mode", human(p.pipeline_mode || "finite")],
					["Sources processed", sources],
					["New jobs reported", count("new_jobs")],
					["Duplicate records", count("duplicates")],
					["Scoring queue", count("scoring_queue")],
					["Scoring workers active", count("scoring_active")],
					[
						"Minimum fit score",
						p.min_score === undefined ? "Not reported" : `${p.min_score}/10`,
					],
				])
			: '<p class="card-content">Current preparation state is unknown. A previous RUNNING value is not evidence of running workers.</p>',
	);
	setHTML(
		"#historical-runtime",
		last
			? `<details class="comfort-details"><summary>Last saved runtime report · historical only</summary><div class="card-content"><p>Received ${esc(fullDate(last.at))}. This is not current activity.</p>${metrics(
					[
						[
							"Previously recorded engine state",
							human(last.pipeline?.pipeline_state),
						],
						[
							"Previously recorded mode",
							human(last.pipeline?.pipeline_mode || "finite"),
						],
						["Previously recorded worker cards", n(last.workers?.length)],
					],
				)}</div></details>`
			: "",
	);
}
function renderSources(data) {
	renderRuntimeDetails();
	setHTML(
		"#source-totals",
		data.totals.length
			? bars(
					data.totals
						.slice(0, 12)
						.map((r) => ({ label: r.source, value: r.jobs })),
				)
			: empty(
					"No stored source records",
					"Your existing providers continue to populate these totals.",
					"layers",
				),
	);
	setHTML(
		"#source-health",
		data.health.length
			? `<div class="table-scroll"><table><thead><tr><th>Provider</th><th>Status</th><th>Found / stored</th><th>Consecutive failures</th><th>Last run</th><th>Last success</th></tr></thead><tbody>${data.health.map((r) => `<tr><td>${esc(r.source_name)}</td><td>${badge(r.status)}</td><td>${n(r.jobs_found)} / ${n(r.jobs_stored)}</td><td>${n(r.consecutive_failures)}</td><td>${esc(dateText(r.last_run_at))}</td><td>${esc(dateText(r.last_success_at))}</td></tr>`).join("")}</tbody></table></div>`
			: empty(
					"No source health records yet",
					"The website reads provider health as it is written; it does not run a discovery cycle.",
					"layers",
				),
	);
}
function renderSystem(data) {
	const m = data.metrics || {};
	setHTML(
		"#database-stats",
		stat(
			"PostgreSQL store",
			bytes(data.database.bytes),
			"<span>Tables and indexes; cluster WAL is not attributed per store</span>",
			"database",
		) +
			stat(
				"Agent runs recorded",
				n(m.runs),
				"<span>Only runs in agent_run_metrics</span>",
				"activity",
			) +
			stat(
				"Tokens recorded",
				n((m.input_tokens || 0) + (m.output_tokens || 0)),
				`<span>${n(m.input_tokens)} input · ${n(m.output_tokens)} output</span>`,
				"spark",
			) +
			stat(
				"Tool calls recorded",
				n(m.tool_calls),
				"<span>Not a billing or cost estimate</span>",
				"layers",
			),
	);
	setHTML(
		"#database-counts",
		Object.keys(data.database.tables).length
			? metrics([
					...Object.entries(data.database.tables).map(([k, v]) => [
						human(k),
						n(v),
					]),
					["Scored jobs", n(summary.totals.scored)],
					[
						"Average recorded fit",
						summary.totals.average_score == null
							? "Not scored"
							: `${Number(summary.totals.average_score).toFixed(1)} / 10`,
					],
				])
			: empty(
					"No tables yet",
					"Run your normal setup to initialize the database.",
					"database",
				),
	);
	setHTML(
		"#outcome-totals",
		summary.outcomes.length
			? `<div class="table-scroll"><table><thead><tr><th>Outcome</th><th>Events</th><th>Distinct jobs</th></tr></thead><tbody>${summary.outcomes.map((r) => `<tr><td>${badge(r.label)}</td><td>${n(r.value)}</td><td>${n(r.jobs)}</td></tr>`).join("")}</tbody></table></div><p class="card-foot">A job can have several outcome types. Events are not unique applicants or a causal referral success rate.</p>`
			: empty(
					"No outcomes recorded yet",
					"Interview, response, offer, and other outcomes appear only when the system records them.",
					"check",
				),
	);
	setHTML(
		"#incidents",
		data.incidents.length
			? `<div class="activity-list">${data.incidents.map((r) => `<div class="incident-item"><h3>${esc(human(r.component))}<span class="badge orange">${n(r.occurrences)} occurrences</span></h3><p>${esc(r.reason)}</p><time>${esc(dateText(r.last_at))}</time></div>`).join("")}</div>`
			: empty(
					"No unresolved incidents recorded",
					"This is the incident table, not a guarantee that every external provider is healthy.",
					"check",
				),
	);
	setHTML(
		"#task-checkpoints",
		data.tasks.length
			? `<div class="metric-list">${data.tasks.map((r) => `<div class="task-row"><h3>${esc(human(r.name))}${r.failures ? `<span class="badge orange">${n(r.failures)} failures</span>` : ""}</h3><p>Next: ${esc(dateText(r.next_at))}<br>Last success: ${esc(dateText(r.last_success))}${r.last_error ? `<br>${esc(r.last_error)}` : ""}</p></div>`).join("")}</div>`
			: empty(
					"No task checkpoints yet",
					"Unattended workers save their next run and recovery state here.",
					"clock",
				),
	);
}
function renderSettings() {
	outreachControls?.update(settings);
}

function requestRefresh() {
	generation++;
	renderActiveFilters();
	return refresh();
}
async function refresh() {
	clearTimeout(timer);
	if (!csrf) return;

	if (route === "overview") {
		await workView?.refresh();
		return;
	}
	// Workers are a separate, cheap read path. Slow summaries/backups must not hold it hostage.
	if (route === "workers") {
		await workersView?.refresh();
		return;
	}
	if (route === "providers") {
		await providersView?.refresh();
		return;
	}
	if (busy) {
		needsRefresh = true;
		return;
	}
	busy = true;
	needsRefresh = false;
	const version = generation,
		current = route;
	const refreshButton = $("#refresh-button");
	refreshButton.disabled = true;
	refreshButton.setAttribute("aria-busy", "true");
	refreshButton.innerHTML = icon("refresh") + "Refreshing…";
	try {
		const endpoints = [`summary?days=${chartDays}`, "settings", "queue"];
		if (current === "overview")
			endpoints.push("jobs?page_size=10&sort=newest", "setup/brief");
		if (current === "jobs")
			endpoints.push(`jobs?${query(jobFilters)}`, "facets");
		if (current === "outreach") endpoints.push(`outreach?${query(outFilters)}`);
		if (current === "activity") endpoints.push(`activity?${query(actFilters)}`);
		if (current === "sources") endpoints.push("sources");
		if (current === "system") endpoints.push("system");
		if (queuePendingTask) endpoints.push("setup/tasks");
		endpoints.push("operations/summary", "backups");
		const responses = await Promise.allSettled(
			endpoints.map(async (endpoint) => {
				try {
					const value = await api(endpoint);
					if (version === generation && current === route && csrf) {
						// Controls and the requested history must not wait on heavier queue,
						// backup or chart queries. A failing optional card is not a form lock.
						if (endpoint === "settings") {
							settings = value;
							readOnly = value.read_only;
							outreachControls?.update(value);
							engineOverview?.updateSettings(value);
						}
						if (endpoint === "queue") {
							acceptQueue(value);
							engineOverview?.updateQueue(queueState);
							if (["overview", "capacity"].includes(current)) renderQueue();
						}
						if (current === "activity" && endpoint.startsWith("activity?"))
							renderActivity(value);
					}
					return value;
				} catch (error) {
					if (
						version === generation &&
						current === route &&
						csrf &&
						current === "activity" &&
						endpoint.startsWith("activity?")
					)
						activityError(error);
					throw error;
				}
			}),
		);
		if (version !== generation || current !== route || !csrf) return;
		const result = responses.map((r) =>
			r.status === "fulfilled" ? r.value : null,
		);
		const failed = endpoints.filter(
			(_, i) => responses[i].status === "rejected",
		);
		if (result[0]?.database_available || !summary?.database_available) {
			if (result[0]) summary = result[0];
		} else if (result[0]) {
			summary = { ...summary, retained: true };
			failed.push("summary");
		}
		if (result[1]) {
			settings = result[1];
			readOnly = settings.read_only;
		}
		acceptQueue(
			result[2] || {
				available: false,
				reason: "Queue update delayed. Showing the last received counts.",
			},
		);
		if (result[2]?.available === false) failed.push("queue");
		engineOverview?.updateQueue(queueState);
		if (queuePendingTask) {
			const row = result[endpoints.indexOf("setup/tasks")]?.tasks?.find(
				(r) => r.id === queuePendingTask.id,
			);
			if (
				row &&
				[
					"completed",
					"failed",
					"timed_out",
					"cancelled",
					"interrupted",
				].includes(row.state)
			) {
				const submitted = queuePendingTask;
				queuePendingTask = null;
				if (row.state === "completed") {
					queueSaveNotice = "Saved";
					notify(
						"Buffer settings applied. Autopilot resumes unless you paused it.",
					);
					const form = $("#queue-form");
					if (
						form &&
						Object.entries(submitted.buffer).every(
							([key, value]) => Number(form.elements[key].value) === value,
						)
					) {
						if (row.result?.revision)
							form.dataset.revision = row.result.revision;
						queueForm?.reset();
					}
					try {
						acceptQueue(await api("queue"));
					} catch {
						acceptQueue({
							available: false,
							reason:
								"The change completed, but its queue check is delayed. Showing the last received counts.",
						});
					}
					if (version !== generation || current !== route || !csrf) return;
				} else {
					queueSaveNotice =
						row.message ||
						"Buffer settings were not applied. Your edits are retained.";
					notify(queueSaveNotice, true);
				}
			}
		}
		if (!summary || !settings)
			throw new Error(
				"Workspace totals or settings are unavailable. Runtime controls are checked separately.",
			);
		runtimeUI();
		outreachControls?.update(settings);
		const operational = result.at(-2),
			opsAlert = $("#operations-alert");
		operationsSummary = operational;
		if (operational) {
			const count =
				operational.unacknowledged_incidents ?? operational.unacknowledged ?? 0;
			opsAlert.hidden = !count || current === "operations";
			opsAlert.innerHTML = count
				? `<a href="#operations">${n(count)} retained incident groups to review</a> · Historical records, not the current Autopilot state.`
				: "";
		}
		const backup = result.at(-1),
			backupAlert = $("#backup-alert");
		if (backup) {
			backupAlert.hidden = !(
				backup.config.enabled &&
				(backup.snapshot_overdue || backup.state.error)
			);
			backupAlert.innerHTML = backupAlert.hidden
				? ""
				: `<strong>Backup needs attention.</strong> ${esc(backup.state.error || "No recent verified off-device database snapshot.")} <a href="#backups">Open recovery →</a>`;
		}
		banner(
			failed.length
				? `Some sections could not update: ${failed.map((p) => p.split("?")[0]).join(", ")}. Autopilot status is checked independently.`
				: "",
		);
		online(!failed.length);
		if (current === "overview") {
			if (result[4]) workspaceBrief = result[4];
			if (result[3]) overviewRecent = result[3];
			renderOverview(overviewRecent);
		}
		if (current === "jobs" && result[3]) {
			jobsResponse = result[3];
			const facets = result[4];
			if (facets) {
				if (document.activeElement !== $("#state-filter"))
					setHTML(
						"#state-filter",
						options(facets.states, jobFilters.state, "All states"),
					);
				if (document.activeElement !== $("#source-filter"))
					setHTML(
						"#source-filter",
						options(facets.sources, jobFilters.source, "All sources"),
					);
			}
			text(
				"#jobs-caption",
				`${n(jobsResponse.total)} matching ${jobsResponse.total === 1 ? "job" : "jobs"} · filters apply to this table and its export`,
			);
			setHTML("#jobs-list", jobRows(jobsResponse.items));
			pagination(jobsResponse, "#jobs-pagination", jobFilters, true);
		}
		if (current === "jobs" && !result[3]) {
			text(
				"#jobs-caption",
				"Jobs could not update. Displayed records may be out of date. Use Refresh to try again.",
			);
			if (!jobsResponse)
				setHTML(
					"#jobs-list",
					empty(
						"Jobs could not load",
						"Your saved jobs have not been changed. Use Refresh to try again.",
						"alert",
					),
				);
		}
		if (current === "outcomes") renderResults();
		if (current === "outreach" && result[3]) renderOutreach(result[3]);
		if (current === "sources" && result[3]) renderSources(result[3]);
		if (current === "system" && result[3]) renderSystem(result[3]);
		if (current === "settings") renderSettings();
		if (current === "capacity") renderQueue();
	} catch (e) {
		if (version === generation && csrf) {
			banner(e.message);
			online(false);
		}
	} finally {
		busy = false;
		$("#refresh-button").disabled = false;
		$("#refresh-button").setAttribute("aria-busy", "false");
		$("#refresh-button").innerHTML = icon("refresh") + "Refresh";
		if (csrf) {
			if (needsRefresh) queueMicrotask(refresh);
			else if (!document.hidden && !updatesPaused())
				timer = setTimeout(
					refresh,
					queuePendingTask ? 2500 : liveConnection?.healthy ? 30000 : 5000,
				);
		}
	}
}

function revealEnginePanel() {
	const panel = $("#overview-engine-details");
	if (!panel) return;
	pagePanels?.show("engine", { focus: true });
	panel.open = true;
	panel.scrollIntoView({ block: "start", behavior: "instant" });
}
async function navigate(force = false) {
	const navigation = ++navigationVersion,
		requestedHash = location.hash;
	if (!csrf) return;
	const hash = location.hash.slice(1).split("?")[0];
	const next = Object.hasOwn(titles, hash) ? hash : "overview";
	const routeParams = new URLSearchParams(location.hash.split("?")[1] || "");
	const requestedView = routeParams.get("view"),
		engineRequested =
			next === "overview" && routeParams.get("panel") === "engine";
	const filterChange =
		next === "jobs" &&
		["all", "ready", "submitted", "attention"].includes(requestedView) &&
		(jobFilters.view !== requestedView || location.hash !== lastRouteHash);
	if (
		!force &&
		next === route &&
		!filterChange &&
		!["setup", "connections", "evidence", "profile", "searches"].includes(next)
	) {
		if (engineRequested) revealEnginePanel();
		return;
	}
	if (!force) {
		const leave = new CustomEvent("applypilot:before-navigate", {
			cancelable: true,
			detail: { restoreHash: lastRouteHash, confirmations: [] },
		});
		const allowed = window.dispatchEvent(leave);
		if (settingsDirty || queueForm?.dirty())
			leave.detail.confirmations.push("Leave your unsaved settings?");
		const accepted =
			allowed &&
			(!leave.detail.confirmations.length ||
				(await confirmAction(leave.detail.confirmations.join(" "), {
					title: "Unsaved changes",
					accept: "Discard edits",
					cancel: "Keep editing",
				})));
		if (navigation !== navigationVersion) return;
		if (!accepted) {
			history.replaceState(
				null,
				"",
				location.pathname + location.search + leave.detail.restoreHash,
			);
			return;
		}
	}
	if (filterChange) {
		jobFilters = { ...defaultJobs, view: requestedView };
		persistFilters();
	}
	lastRouteHash = requestedHash;
	unmountOperations();
	unmountSetup();
	providersView?.dispose();
	providersView = null;
	unmountMemory();
	route = next;
	generation++;
	shell();
	refresh();
	if (!force) {
		$("#page-title").focus({ preventScroll: true });
		window.scrollTo({ top: 0, behavior: "instant" });
	}
	renderActiveFilters();
	if (engineRequested) revealEnginePanel();
}
async function exportJobs() {
	if (
		jobsResponse?.total > 10000 &&
		!(await confirmAction(
			`The current filters match ${n(jobsResponse.total)} jobs. Export the first 10,000 in the selected sort order?`,
		))
	)
		return;
	const b = $("#export-jobs");
	b.disabled = true;
	try {
		const r = await fetch(`/api/jobs.csv?${query(jobFilters)}`, {
			credentials: "same-origin",
			cache: "no-store",
			signal: AbortSignal.timeout(15000),
		});
		if (!r.ok) {
			let v = {};
			try {
				v = await r.json();
			} catch {}
			throw new Error(v.error || "Export failed");
		}
		const blob = await r.blob(),
			url = URL.createObjectURL(blob),
			a = document.createElement("a");
		a.href = url;
		a.download = "applypilot-jobs.csv";
		a.click();
		setTimeout(() => URL.revokeObjectURL(url), 30000);
		notify(
			r.headers.get("X-Export-Truncated") === "true"
				? "Exported the first 10,000 matching rows."
				: "Export downloaded with the current filters.",
		);
	} catch (e) {
		notify(e.message, true);
	} finally {
		b.disabled = false;
	}
}
function openDialog(kicker) {
	const dlg = $("#detail-dialog");
	if (!dlg.open) detailReturn = document.activeElement;
	text("#detail-kicker", kicker);
	setHTML(
		"#detail-content",
		'<h2 id="detail-title">Loading details…</h2><div class="loading-state"><div class="loader"></div></div>',
	);
	if (!dlg.open) dlg.showModal();
	$("#close-detail").focus();
}
function properties(values) {
	return `<dl class="detail-properties">${values.map(([k, v]) => `<div class="detail-property"><dt>${esc(k)}</dt><dd>${esc(v ?? "Not recorded")}</dd></div>`).join("")}</dl>`;
}
let detailGeneration = 0;
async function openJob(url) {
	const id = ++detailGeneration;
	openDialog("OPPORTUNITY DETAILS");
	try {
		const { job: j, timeline, outcomes } = await api(`job?${query({ url })}`);
		if (id !== detailGeneration || !$("#detail-dialog").open) return;
		setHTML(
			"#detail-content",
			`<h2 id="detail-title">${esc(j.title || "Untitled job")}</h2><p class="detail-subtitle">${esc(j.company)} · ${esc(j.location || "Location not recorded")}</p>${badge(j.state)}<div class="detail-actions">${external(j.application_url || j.url, "Open original listing")}${j.documents.map((d) => `<a class="btn btn-sm" href="/api/document?${esc(query({ url: j.url, kind: d.kind }))}" download>${icon("download")}${esc(human(d.kind))}</a>`).join("")}</div>${properties(
				[
					[
						"Fit score",
						j.fit_score == null ? "Not scored" : `${j.fit_score}/10`,
					],
					["Source", j.site],
					["Salary", j.salary || "Not recorded"],
					["Discovered", dateText(j.discovered_at)],
					["Recorded submission", dateText(j.applied_at)],
					["Application attempts", n(j.apply_attempts)],
				],
			)}${j.score_reasoning ? `<section class="detail-section"><h3>Why this fit score?</h3><p class="ai-disclosure">AI-generated assessment · compare this with the original listing.</p><p>${esc(j.score_reasoning)}</p></section>` : ""}${j.apply_error || j.detail_error || j.score_error ? `<section class="detail-section"><h3>Latest recorded issue</h3><p>${esc(j.apply_error || j.detail_error || j.score_error)}</p></section>` : ""}<section class="detail-section"><h3>Job description</h3><p>${esc(j.description || "A description has not been captured yet.")}</p></section><section class="detail-section"><h3>Recorded outcomes</h3>${outcomes.length ? outcomes.map((o) => `<div class="timeline-item">${badge(o.event_type)}<time>${esc(dateText(o.occurred_at))} · ${esc(o.source)} · ${esc(o.trust || "trust not recorded")}</time></div>`).join("") : "<p>No outcomes recorded for this job.</p>"}</section><details class="detail-section technical-details"><summary>Pipeline history · latest 100</summary><div class="detail-timeline">${timeline.length ? timeline.map((t) => `<div class="timeline-item">${esc(human(t.from_state))} → ${esc(human(t.to_state))}<time>${esc(dateText(t.at))}</time>${t.reason ? `<p>${esc(t.reason)}</p>` : ""}</div>`).join("") : "<p>No state transitions recorded.</p>"}</div></details>`,
		);
	} catch (e) {
		if (id !== detailGeneration || !$("#detail-dialog").open) return;
		setHTML(
			"#detail-content",
			`<h2 id="detail-title">Details unavailable</h2><div class="notice notice-warning" role="alert">${esc(e.message)}</div><button class="btn btn-primary" data-job="${esc(url)}">Try again</button>`,
		);
	}
}
async function openOutreach(id) {
	const sequence = ++detailGeneration;
	openDialog("MESSAGE PREVIEW");
	try {
		const r = await api(`outreach/${encodeURIComponent(id)}`);
		if (sequence !== detailGeneration || !$("#detail-dialog").open) return;
		setHTML(
			"#detail-content",
			`<h2 id="detail-title">${esc(r.name)}</h2><p class="detail-subtitle">${esc(r.company)} · ${esc(r.title)}</p>${badge(r.state)}<div class="detail-actions">${external(r.profile_url, "Professional profile")}<button class="btn btn-sm" data-job="${esc(r.job_url)}">Job details ${icon("arrow")}</button>${r.cancellable ? `<button class="btn btn-danger btn-sm" data-cancel="${esc(r.id)}"${readOnly ? " disabled" : ""}>Cancel request</button>` : ""}</div>${properties(
				[
					["Channel", human(r.channel)],
					["Message type", human(r.kind)],
					["Relationship", human(r.relationship)],
					["Function", r.role_function || "Not recorded"],
					["Created", dateText(r.created_at)],
					["Sent", dateText(r.sent_at)],
				],
			)}<section class="detail-section"><h3>${esc(r.payload.subject || "Exact saved message")}</h3>${r.payload.to ? `<p>To: ${esc(r.payload.to)}</p>` : ""}<pre>${esc(r.payload.body || "No text body recorded.")}</pre>${r.payload.attachment ? `<p>Attachment: ${esc(r.payload.attachment)}</p>` : ""}</section><section class="detail-section"><h3>Dispatch rules</h3><p>This is a preview, not an approval requirement. Cancellation only succeeds before dispatch starts. An eligible window does not guarantee delivery; budgets, provider availability, and reconciliation still apply.</p></section>`,
		);
	} catch (e) {
		if (sequence !== detailGeneration || !$("#detail-dialog").open) return;
		setHTML(
			"#detail-content",
			`<h2 id="detail-title">Preview unavailable</h2><div class="notice notice-warning" role="alert">${esc(e.message)}</div><button class="btn btn-primary" data-outreach="${esc(id)}">Try again</button>`,
		);
	}
}
async function cancelRequest(id, button) {
	if (readOnly) return;
	button.disabled = true;
	try {
		await api(`outreach/${encodeURIComponent(id)}/cancel`, {
			method: "POST",
			body: {},
		});
		notify("Request cancelled. The application services continue.");
		if ($("#detail-dialog").open) $("#detail-dialog").close();
		await refresh();
	} catch (e) {
		notify(e.message, true);
		button.disabled = false;
		refresh();
	}
}

document.addEventListener("click", (e) => {
	const clear = e.target.closest(
		"[data-clear-filter],[data-clear-all-filters]",
	);
	if (clear) {
		if (clear.hasAttribute("data-clear-all-filters")) {
			clearAllJobFilters();
			return;
		} else jobFilters[clear.dataset.clearFilter] = "";
		jobFilters.page = 1;
		persistFilters();
		const form = $("#job-filters");
		if (form)
			for (const [key, value] of Object.entries(jobFilters)) {
				if (form.elements[key]) form.elements[key].value = value;
			}
		syncJobViews();
		renderActiveFilters();
		requestRefresh();
		form?.elements.q.focus();
		return;
	}
	const cancel = e.target.closest("[data-cancel]");
	if (cancel) {
		cancelRequest(cancel.dataset.cancel, cancel);
		return;
	}
	const job = e.target.closest("[data-job]");
	if (job) {
		openJob(job.dataset.job);
		return;
	}
	const message = e.target.closest("[data-outreach]");
	if (message) {
		openOutreach(message.dataset.outreach);
		return;
	}
});
$("#close-detail").addEventListener("click", () => $("#detail-dialog").close());
$("#detail-dialog").addEventListener("close", () => {
	detailGeneration++;
	if (detailReturn?.isConnected) detailReturn.focus();
	else $("#main").focus();
});
$("#detail-dialog").addEventListener("click", (e) => {
	if (e.target === $("#detail-dialog")) {
		const r = e.target.getBoundingClientRect();
		if (
			e.clientX < r.left ||
			e.clientX > r.right ||
			e.clientY < r.top ||
			e.clientY > r.bottom
		)
			e.target.close();
	}
});
async function sendAutopilotAction(action) {
	if (controlBusy || readOnly) return;
	controlBusy = true;
	controlError = "";
	runtimeUI();
	try {
		const result = await api("autopilot/control", {
			method: "POST",
			body: { action },
		});
		runtimeControlReport = { ...(runtimeControlReport || {}), control: result };
		runtime = { ...(runtime || {}), control: result };
		if (!result.runtime_reported) {
			runtime.running = false;
			runtime.stale = true;
		}
		runtimeUI();
		notify(result.message || "Autopilot control updated.");
		pollRuntime(true);
		await requestRefresh();
	} catch (e) {
		controlError = e.message;
		notify(e.message, true);
		pollRuntime();
	} finally {
		controlBusy = false;
		runtimeUI();
	}
}
$("#autopilot-toggle").addEventListener("click", () =>
	sendAutopilotAction($("#autopilot-toggle").dataset.action),
);
$("#autopilot-stop").addEventListener("click", async () => {
	if (controlBusy || readOnly || !runtime?.control?.can_stop) return;
	if (
		await confirmAction(
			"Stop autopilot and its controller? Active work follows its safe shutdown path. Saved jobs, submissions and settings are kept.",
			{ title: "Stop Autopilot", accept: "Stop controller", danger: true },
		)
	)
		sendAutopilotAction("stop");
});
$("#refresh-button").addEventListener("click", () => {
	engineOverview?.refresh();
	workersView?.refresh(true);
	pollRuntime(true);
	requestRefresh();
});
navigationUI = mountNavigation({ localGet, localSet });
for (const group of $$(".nav-group")) {
	group.open = localGet("ap-" + group.id, false) === true;
	group.addEventListener("toggle", () =>
		localSet("ap-" + group.id, group.open),
	);
}
$("#global-search-form").addEventListener("submit", (e) => {
	e.preventDefault();
	jobFilters = {
		...defaultJobs,
		q: $("#global-search").value.trim().slice(0, 200),
	};
	persistFilters();
	if (route === "jobs") {
		shell();
		requestRefresh();
		$("#job-filters input[name=q]")?.focus();
	} else location.hash = "jobs";
});
$("#unlock-form").addEventListener("submit", async (e) => {
	e.preventDefault();
	const input = $("#unlock-token"),
		button = e.currentTarget.querySelector("button");
	if (button.disabled) return;
	button.disabled = true;
	button.textContent = "Unlocking…";
	button.setAttribute("aria-busy", "true");
	input.removeAttribute("aria-invalid");
	text("#auth-error", "");
	try {
		await unlock(input.value.trim());
		$("#page-title").focus();
	} catch (err) {
		text("#auth-error", err.message);
		input.setAttribute("aria-invalid", "true");
		input.focus();
	} finally {
		button.disabled = false;
		button.textContent = "Unlock workspace";
		button.setAttribute("aria-busy", "false");
	}
});
$("#theme-toggle").addEventListener("click", () => {
	const theme =
		document.documentElement.dataset.theme === "dark" ? "light" : "dark";
	document.documentElement.dataset.theme = theme;
	localSet("ap-theme", theme);
});
window.addEventListener("hashchange", () => navigate());
document.addEventListener("visibilitychange", () => {
	clearTimeout(timer);
	if (!document.hidden && !updatesPaused()) {
		pollRuntime();
		refresh();
	}
});

setInterval(countdowns, 1000);
icons();
document.documentElement.dataset.theme = localGet("ap-theme", "light");
(async () => {
	const hash = new URLSearchParams(location.hash.slice(1)),
		token = hash.get("token");
	if (token) {
		const view = hash.get("view") === "setup" ? "setup" : "overview";
		history.replaceState(null, "", location.pathname + "#" + view);
		try {
			await unlock(token);
		} catch (e) {
			showAuth();
			text("#auth-error", e.message);
		}
		return;
	}
	try {
		const session = await api("session");
		csrf = session.csrf;
		readOnly = session.read_only;
		startLiveConnection();
		pollRuntime();
		await chooseInitialView();
		navigate(true);
	} catch {
		showAuth();
	}
})();
