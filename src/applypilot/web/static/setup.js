import { attachDiaryEditor } from "./diary_editor.js";
import {
	connectionCards,
	policyControls,
	readPolicyControls,
	runtimeControls,
	syncIntegrationButtons,
	wireIntegrations,
} from "./integration_ui.js";
import {
	mountActivity,
	renderTaskResults,
	setActivityOpen,
	showActivityRuntime,
	taskDetails,
	taskRuntimeLine,
	unmountActivity,
} from "./panels.js";
import { changeReview, exactRecord, label, readableValue } from "./readable.js";
import { help, impactEntry, tokenLink, wireHelp } from "./setup_ui.js";
import { confirmAction, trackForm, updatesPaused } from "./ux.js";

/* Owner setup workspace. No credentials in localStorage; all source text is escaped.
 * Expensive actions are bounded jobs; polling never replaces dirty owner edits.
 */
const esc = (v) =>
	String(v ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const list = (s) =>
	String(s || "")
		.split(/[,\n]/)
		.map((x) => x.trim())
		.filter(Boolean);
const get = (v, path) => path.split(".").reduce((a, k) => a?.[k], v);
const put = (v, path, value) => {
	const keys = path.split(".");
	let t = v;
	for (const k of keys.slice(0, -1)) t = t[k] ??= {};
	t[keys.at(-1)] = value;
};
const formatDate = (v) => (v ? new Date(v).toLocaleString() : "Not checked");
const steps = [
	["materials", "Your material", "Bring what you already have"],
	["connections", "Connections", "Connect the tools you use"],
	["profile", "Your profile", "Facts, not guesses"],
	["searches", "Your search", "Markets, opportunity signals and exploration"],
	["impact", "Impact diary", "Optional · save useful examples"],
	["launch", "Review & launch", "One setup. The same autopilot."],
];
let cleanup = () => {};

export function unmountSetup() {
	cleanup();
	cleanup = () => {};
}
export function mountSetup(
	root,
	{ api, notify, view = "setup", readOnly = false },
) {
	unmountSetup();
	root.dataset.readOnly = String(readOnly);
	let state = null,
		env = null,
		runtimePolicy = null,
		diaryData = { entries: [], total: 0 },
		step =
			new URLSearchParams(location.hash.split("?")[1] || "").get("step") ||
			{
				connections: "connections",
				evidence: "materials",
				profile: "profile",
				searches: "searches",
			}[view] ||
			"materials";
	if (step === "evidence") step = "materials";
	if (!steps.some((s) => s[0] === step)) step = "materials";
	let materialConsent = false,
		autoSuggest = true,
		materialGoals = "",
		acceptedHash = location.hash;
	let formTrackers = [];
	let secretTimers = [],
		secretHiders = [],
		dirty = false,
		disposed = false,
		timer = null,
		taskState = "",
		group =
			new URLSearchParams(location.hash.split("?")[1] || "").get("group") ||
			"llm",
		lastJobs = [];
	const destroyEditor = attachDiaryEditor(root, {
		api,
		notify,
		onChanged: load,
		readOnly,
	});
	const beforeUnload = (e) => {
		if (dirty) {
			e.preventDefault();
			e.returnValue = "";
		}
	};
	const beforeNavigate = (e) => {
		if (e.detail) {
			e.detail.restoreHash = acceptedHash;
			if (dirty)
				e.detail.confirmations.push(
					"Leave these unsaved edits? Your saved draft will be kept.",
				);
		}
	};
	window.addEventListener("applypilot:before-navigate", beforeNavigate);
	const hideSecrets = () => {
		for (const hide of secretHiders) hide();
		secretHiders = [];
	};
	window.addEventListener("beforeunload", beforeUnload);
	window.addEventListener("blur", hideSecrets);
	document.addEventListener("visibilitychange", hideSecrets);
	cleanup = () => {
		formTrackers.forEach((t) => t.destroy());
		window.removeEventListener("applypilot:before-navigate", beforeNavigate);
		unmountActivity();
		destroyEditor();
		disposed = true;
		hideSecrets();
		window.removeEventListener("blur", hideSecrets);
		document.removeEventListener("visibilitychange", hideSecrets);
		clearTimeout(timer);
		for (const t of secretTimers) clearTimeout(t);
		window.removeEventListener("beforeunload", beforeUnload);
	};
	function recordDirty() {
		dirty = formTrackers.some((t) => t.dirty());
		const e = $("#setup-dirty", root);
		if (e) e.hidden = !dirty;
	}
	function setDirty() {
		formTrackers.forEach((t) => t.refresh());
		recordDirty();
	}
	const post = (path, body) =>
		api(path, {
			method: "POST",
			body,
			signal: AbortSignal.timeout(path.includes("intake") ? 45000 : 20000),
		});
	async function load() {
		try {
			const [s, e, r, d] = await Promise.all([
				api("setup", { signal: AbortSignal.timeout(20000) }),
				api("setup/connections"),
				api("setup/runtime-policy"),
				api("diary"),
			]);
			if (disposed) return;
			state = s;
			env = e;
			if (!env.groups.some((g) => g.id === group)) group = "llm";
			runtimePolicy = r;
			diaryData = d;
			lastJobs = s.tasks;
			dirty = false;
			render();
		} catch (e) {
			if (!disposed) {
				root.innerHTML = `<div class="notice notice-warning" role="alert">${esc(e.message)}</div><button class="btn" id="setup-reload">Retry setup view</button>`;
				$("#setup-reload", root).onclick = load;
			}
		}
	}
	function taskBox(jobs) {
		if (!jobs.length)
			return '<p class="fine-print">No setup tasks yet. Routine connection steps are handled for you. These setup tasks do not send outreach.</p>';
		return jobs
			.slice(0, 12)
			.map(
				(j) =>
					`<article class="setup-task" data-task-id="${esc(j.id)}"><div><strong>${esc(j.operation.replaceAll("_", " "))}</strong><span class="badge ${j.state === "completed" ? "green" : j.state === "failed" || j.state === "timed_out" ? "red" : "orange"}">${esc(j.state)}</span></div>${taskRuntimeLine(j)}<p>${esc(j.message)}</p>${j.auth_url ? `<a class="btn btn-primary" href="${esc(j.auth_url)}" target="_blank" rel="noopener noreferrer">Open sign-in page ↗</a>` : ""}${j.device_code ? `<p>One-time code: <strong>${esc(j.device_code)}</strong> <button class="btn btn-sm" data-copy-device="${esc(j.device_code)}">Copy code</button></p>` : ""}${["running", "queued"].includes(j.state) ? `<button class="btn btn-sm" data-stop-task="${esc(j.id)}">Cancel this task</button>` : ""}${taskDetails(j)}</article>`,
			)
			.join("");
	}
	function wireTasks() {
		$$("[data-copy-device]", root).forEach(
			(b) =>
				(b.onclick = async () => {
					try {
						await navigator.clipboard.writeText(b.dataset.copyDevice);
						notify("One-time code copied.");
					} catch {
						notify(
							"Clipboard unavailable. Select the displayed code to copy it.",
							true,
						);
					}
				}),
		);
		$$("[data-stop-task]", root).forEach(
			(b) =>
				(b.onclick = () =>
					perform(
						() => post("setup/task/cancel", { id: b.dataset.stopTask }),
						false,
					)),
		);
	}
	async function poll() {
		clearTimeout(timer);
		if (disposed) return;
		if (updatesPaused() || document.hidden) {
			timer = setTimeout(poll, 1000);
			return;
		}
		try {
			const data = await api("setup/tasks");
			if (disposed || updatesPaused()) return;
			lastJobs = data.tasks;
			syncIntegrationButtons(root, lastJobs, state?.integrations);
			showActivityRuntime(root, data.runtime);
			const signature = JSON.stringify(
				data.tasks.map((j) => [
					j.id,
					j.state,
					j.message,
					j.auth_url,
					j.device_code,
				]),
			);
			const box = $("#setup-tasks", root);
			if (signature !== taskState) {
				taskState = signature;
				if (box) {
					renderTaskResults(box, taskBox(data.tasks));
					wireTasks();
				}
			}
			const completed = data.tasks.some(
				(j) =>
					["completed", "failed", "timed_out", "cancelled"].includes(j.state) &&
					!state?.tasks?.some((o) => o.id === j.id && o.state === j.state),
			);
			if (completed) {
				if (!dirty) await load();
				else {
					state.tasks = data.tasks;
					notify(
						"Setup task finished. Save your edits, then refresh to load the result.",
					);
				}
			}
		} catch {
			/* Existing parent dashboard displays session/offline errors. */
		} finally {
			if (!disposed) timer = setTimeout(poll, 2500);
		}
	}
	async function perform(fn, reload = true) {
		formTrackers.forEach((t) => t.busy(true));
		const buttons = $$(
			"button[type=submit], [data-action], [data-verify], [data-social-connect], [data-session-file], [data-session-reuse]",
			root,
		).map((b) => [b, b.disabled]);
		buttons.forEach(([b]) => (b.disabled = true));
		try {
			const result = await fn();
			if (disposed) return result;
			notify(
				result?.task_id
					? "Task started. Activity & results shows its progress."
					: result?.message || result?.note || "Saved.",
			);
			if (result?.task_id) setActivityOpen(root, true);
			if (reload) await load();
			return result;
		} catch (e) {
			if (!disposed) {
				notify(e.message, true);
				const banner = $("#setup-error", root);
				if (banner) {
					banner.textContent = e.message;
					banner.hidden = false;
					banner.scrollIntoView({ block: "nearest" });
				}
			}
		} finally {
			if (!disposed) {
				buttons.forEach(([b, wasDisabled]) => {
					if (b.isConnected) b.disabled = readOnly || wasDisabled;
				});
				formTrackers.forEach((t) => t.busy(false));
				syncIntegrationButtons(root, lastJobs, state?.integrations);
				if (readOnly)
					$$("[data-action]", root)
						.filter((b) => b.dataset.action !== "reload")
						.forEach((b) => (b.disabled = true));
			}
		}
	}
	async function launchTask(operation, payload) {
		const result = await perform(
			() => post("setup/task/" + operation, payload),
			false,
		);
		if (result?.task_id) {
			lastJobs = [
				{ id: result.task_id, operation, state: "queued" },
				...lastJobs,
			];
			syncIntegrationButtons(root, lastJobs, state?.integrations);
		}
		return result;
	}
	const aliases = {
		"experience.professional_summary": [
			"experience.positioning_summary",
			"experience.summary",
		],
		"experience.current_job_title": ["experience.current_title"],
		"experience.current_company": [
			"experience.company",
			"experience.current_employer",
		],
		"experience.years_of_experience_total": ["experience.years_of_experience"],
		"skills_boundary.languages": ["skills_boundary.programming_languages"],
		"skills_boundary.databases": ["skills_boundary.databases_and_messaging"],
		"skills_boundary.devops": ["skills_boundary.cloud_and_platform"],
		"personal.portfolio_url": ["personal.website_url"],
	};
	function known(path) {
		for (const k of [path, ...(aliases[path] || [])]) {
			const v = get(state.draft.profile, k);
			if (
				v !== undefined &&
				v !== null &&
				v !== "" &&
				(!Array.isArray(v) || v.length)
			)
				return v;
		}
		return "";
	}
	function field(path, label, type = "text", hint = "", choices = null) {
		const value = known(path),
			id = "sf-" + path.replaceAll(".", "-");
		const input = choices
			? `<select id="${id}" data-profile="${path}">${choices.map(([v, l]) => `<option value="${esc(v)}"${String(value) === v ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`
			: `<input id="${id}" data-profile="${path}" type="${type}" value="${esc(Array.isArray(value) ? value.join(", ") : value)}"${path.startsWith("skills_boundary.") ? ' data-list="true"' : ""} autocomplete="off">`;
		return `<div class="setup-field"><label for="${id}">${esc(label)}</label>${hint ? help(hint) : ""}${input}</div>`;
	}
	function card(title, sub, html) {
		return `<section class="card setup-card"><div class="card-head"><div><h2>${title}</h2>${sub ? `<p>${sub}</p>` : ""}</div></div><div class="setup-card-body">${html}</div></section>`;
	}
	function materials() {
		const existing = state.sources.filter((s) => s.existing),
			uploads = state.sources.filter((s) => !s.existing);
		const rows = (items) =>
			items
				.map(
					(s) =>
						`<div class="setup-source"><span class="source-symbol">▤</span><div><strong>${esc(s.name)}</strong><p>${esc(s.impact ? "Impact diary" : s.evidence ? "Project evidence" : s.kind)} · ${Number(s.characters || 0).toLocaleString()} characters ${s.existing ? "· already in your workspace" : ""}</p></div><div class="setup-inline">${!s.existing ? `${s.kind !== "supporting" ? `<button class="btn btn-sm" data-use-source="${s.id}">Use in draft</button>` : ""}<button class="btn btn-sm" data-remove-source="${s.id}">Remove intake copy</button>` : '<span class="badge green">Detected</span>'}</div></div>`,
				)
				.join("");
		return `${card(state.workspace?.existing ? "Welcome back. Your workspace comes first." : "Start with your material", state.workspace?.root || "", `<div class="setup-mini-stats"><div><strong>${state.workspace?.files.length || 0}</strong><span>existing workspace files</span></div><div><strong>${state.draft.profile.public_evidence?.length || 0}</strong><span>saved project evidence</span></div><div><strong>${diaryData.total}</strong><span>impact diary entries</span></div></div><p>${state.workspace?.existing ? "Your profile, search strategy, master résumé and impact history are reused. There is no need to upload them again." : "Upload what you have; supported facts fill a draft, not your live files."}</p><details class="setup-details"><summary>Detected workspace material (${existing.length})</summary>${rows(existing)}<p class="fine-print">Data lives at the path above. A different workspace must be selected with APPLYPILOT_DIR before launching; this page never silently switches databases.</p></details>${state.live_conflict ? '<div class="notice notice-warning">An older draft differs from your current files. <button type="button" class="btn" data-action="reset-draft">Reload draft from existing workspace</button> This resets draft edits only; live files and material are kept.</div>' : ""}`)}
  <div class="setup-columns">${card("Add material", "Files, notes, public links and GitHub projects belong here.", `<form id="intake-file"><label class="upload-zone" id="upload-zone"><strong>Choose a résumé or supporting file</strong><span>PDF · DOCX · TXT · Markdown · profile.json · searches.yaml</span><input id="intake-files" type="file" accept=".pdf,.docx,.txt,.md,.json,.yaml,.yml"></label><label class="setup-field">Use document as<select id="intake-kind"><option value="resume">Master résumé</option><option value="supporting" ${state.workspace?.existing ? "selected" : ""}>Supporting material</option></select></label><button type="submit" class="btn btn-primary">Import file</button><p id="file-queue" class="fine-print"></p></form><details class="setup-details"><summary>Paste a résumé or notes</summary><form id="intake-paste"><label class="setup-field">Your text<textarea id="intake-text" rows="5" maxlength="120000"></textarea></label><label class="setup-check"><input id="paste-resume" type="checkbox">Use as master résumé</label><button class="btn" type="submit">Add text</button></form></details><form id="material-url" class="setup-stack"><label class="setup-field">Website, article, portfolio or GitHub URL<input id="material-url-input" type="url" placeholder="https://…" required></label><label class="setup-field">Read with<select id="material-reader"><option value="auto">Configured public reader · HTTP / Playwright</option><option value="browseros">My BrowserOS session · one page</option><option value="http">HTTP only · no browser</option></select></label><label class="setup-check" id="material-browser-permission" hidden><input id="material-browser-consent" type="checkbox">I authorize importing this page with my signed-in browser; it may contain private content. This does not authorize crawling other pages or sending it to a model.</label><label class="setup-check"><input id="material-github-private" type="checkbox">Include private GitHub repositories I am authorized to inspect, using my saved GitHub account. Private profile discovery covers owned repositories only.</label><button class="btn" type="submit">Collect this link</button>${help("The public reader retains robots and URL protections. For an owner-authorized one-page import, choose BrowserOS and confirm session access. Login, CAPTCHA and paywall screens are not bypassed. GitHub keeps revision-pinned collection.")}</form><details class="setup-details"><summary>GitHub projects & contribution evidence</summary>${evidence()}</details>`)}
  ${card("Fill the gaps, not another questionnaire", "Existing values win. Facts need source support; your compensation stays opportunity-specific.", `<form id="suggest-form"><label class="setup-field">Anything new about your goals?<textarea id="owner-goals" rows="3" maxlength="6000" placeholder="Leave empty to retain your search strategy">${esc(materialGoals)}</textarea></label><label class="setup-check"><input id="share-model" type="checkbox" ${materialConsent ? "checked" : ""}>Use my configured model to read this material and fill missing facts (including bounded private repository excerpts when private GitHub intake is selected)</label><label class="setup-check"><input id="auto-suggest" type="checkbox" ${autoSuggest ? "checked" : ""}>After adding material, fill missing facts automatically when model sharing is enabled</label><label class="setup-check"><input id="replace-master" type="checkbox">Replace my master with the newest uploaded résumé</label><button class="btn btn-primary" type="submit">Read material & fill missing facts</button>${help("Up to eight documents of 24,000 characters each, plus your existing profile and search preferences. Explicit contact/title/skill facts can be extracted locally without a model. Missing or ambiguous facts remain unresolved; no metrics, legal answers or experience are invented.")}</form><div class="setup-inline"><button type="button" class="btn" data-go-profile>Review profile →</button><button type="button" class="btn" data-go-impact>Impact diary →</button></div>`)}
  </div>${uploads.length ? card("Added material", `${uploads.length} intake copies`, rows(uploads)) : ""}`;
	}
	function connections() {
		const current = env.groups.find((g) => g.id === group);
		function envField(f) {
			const id = "env-" + f.key;
			const control =
				f.choices.length && !f.local_only
					? `<select id="${id}" data-env="${f.key}"><option value="">Use runtime default</option>${f.choices.map((v) => `<option value="${esc(v)}"${v === f.value ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`
					: `<input id="${id}" ${f.local_only ? "readonly" : `data-env="${f.key}"`} type="${f.secret ? "password" : "text"}" autocomplete="${f.secret ? "new-password" : "off"}" value="${esc(f.value)}" placeholder="${esc(f.placeholder)}">`;
			return `<div class="setup-field"><div class="setup-field-title"><label for="${id}">${esc(f.label)}</label>${help(f.hint || f.key)}</div><div class="secret-input">${control}${f.secret && f.configured ? `<button type="button" class="btn btn-sm" data-reveal="${f.key}" aria-label="Show ${esc(f.label)}">Show</button>` : ""}</div><small class="setting-origin">${esc(f.origin?.label || "Not configured")}${f.process_differs ? " · shell override is active" : ""}</small>${f.origin?.path ? help("Read from " + f.origin.path) : ""}${!f.local_only ? `<label class="setup-clear"><input type="checkbox" data-clear="${f.key}">Clear workspace override</label>` : ""}</div>`;
		}
		const gmail = state.gmail || {};
		return `<div class="notice">${esc(env.notice)}</div><div class="setup-connections"><aside class="setup-group-nav" aria-label="Connection categories">${env.groups.map((g) => `<button data-env-group="${g.id}" class="${group === g.id ? "active" : ""}">${esc(g.title)}<small>${env.fields.filter((f) => f.group === g.id && f.configured).length} detected</small></button>`).join("")}</aside><div>${connectionCards(group, state, runtimePolicy.policy, env, runtimePolicy.maximum_applied === true)}
  ${group === "gmail" ? card(gmail.reconnect_required ? "Gmail needs reconnection" : gmail.configured ? "Gmail credentials detected" : "Gmail connection", esc(gmail.message || ""), `<p class="fine-print">${esc(gmail.folder || "")}</p>${gmail.reconnect_required ? '<p class="notice notice-warning">Google rejected the saved refresh token. Reconnect the SAME Gmail account. Your OAuth client file will be kept.</p>' : ""}<label class="setup-field">Gmail account<input id="gmail-account" type="email" value="${esc(gmail.mailbox || known("personal.email"))}" autocomplete="email" placeholder="Your existing Gmail address"></label><p class="fine-print">Connect your Gmail to read replies and send outreach from this account.</p><div class="setup-inline"><button class="btn btn-primary" data-action="gmail-connect">${gmail.configured ? "Reconnect same Gmail account" : "Connect Gmail"}</button><button class="btn" data-verify="gmail">Verify Gmail connection</button></div><details class="setup-details"><summary>Advanced · supply or replace a Desktop OAuth client</summary><p>Existing clients are reused. Upload only when a client is missing or you intentionally replace it.</p><a class="btn btn-sm" href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer">Google OAuth credentials ↗</a><form id="gmail-upload" class="setup-stack"><label class="setup-field">Desktop OAuth JSON<input type="file" id="gmail-client-file" accept=".json"></label><button class="btn" type="submit">Save OAuth client</button></form></details>`) : ""}
  ${card(
		esc(current.title),
		"",
		`${group !== "llm" ? '<details class="setup-details"><summary>Advanced overrides · optional tokens, paths and environment settings</summary>' : ""}<form id="env-form"><div class="setup-fields">${env.fields
			.filter((f) => f.group === group)
			.map(envField)
			.join(
				"",
			)}</div><button class="btn btn-primary" type="submit">Save application settings</button></form>${group !== "llm" ? "</details>" : ""}`,
	)}
  ${group === "browser" ? card("Your signed-in browser", "", `<p>LinkedIn: ${esc(known("personal.linkedin_url") || "Not saved")}<br>X: ${esc(known("personal.x_url") || "Not saved")}</p><button class="btn" data-verify="browseros">Check browser connection</button><a class="btn btn-quiet" href="#setup?step=profile">Edit account URLs</a>`) : ""}
  ${group === "llm" ? '<button class="btn" data-verify="llm">Verify model aliases</button>' : ""}
  ${
		group === "sources"
			? card(
					"Local complementary readers",
					"Docker carries the SDKs; your host keeps its existing sessions.",
					`<p><strong>Source-tools:</strong> ${env.local_services?.source_tools_running ? "Running" : "Not detected"}. Search auto combines SearXNG and DDGS with deduplication. Reader auto uses HTTP first, then renders thin pages with Playwright; hybrid combines both for every permitted page.</p><div class="setup-inline"><button class="btn btn-primary" data-action="local-defaults">Use complementary local defaults</button><button class="btn" data-verify="sources">Verify local readers & detected sessions</button></div><details class="setup-details"><summary>Start the local services</summary><pre class="setup-code">cd docs/guides
docker compose -f docker-compose.litellm.yml up -d --build searxng source-tools</pre><p class="fine-print">No arbitrary Docker commands are accepted by this website. Source-tools includes DDGS, Playwright, Twikit, linkedin_scraper, linkedin-rs and Gmail MCP. FreeHire remains its remote API with your existing CLI credential. Existing BrowserOS accounts remain separate.</p></details>`,
				)
			: ""
	}

  ${
		Object.entries(state.checks).length
			? card(
					"Previous connection checks",
					"Detected configuration and live verification are different.",
					Object.entries(state.checks)
						.map(
							([name, c]) =>
								`<details class="setup-check-result"><summary>${esc(name)} · ${c.stale ? "Needs recheck" : c.ok ? "Verified" : "Needs attention"} · ${esc(formatDate(c.checked_at))}</summary>${(c.checks || []).map((r) => `<p>${esc(r.detail)}</p>`).join("")}</details>`,
						)
						.join(""),
				)
			: ""
	}
  </div></div>`;
	}
	function profile() {
		const p = state.draft.profile;
		const summary = [
			["experience.current_job_title", "Most recent role"],
			["experience.current_company", "Employer"],
			[
				"experience.years_of_experience_total",
				"Stated professional experience",
			],
			["experience.professional_summary", "Professional narrative"],
		];
		return `${card("Your profile", "Review your saved details.", `<h3>${esc(known("personal.full_name") || "Name not yet supplied")}</h3><p>${esc(known("personal.email"))} · ${esc(known("personal.city"))} · ${esc(known("personal.country"))}</p><dl class="profile-summary">${summary.map(([path, label]) => `<div><dt>${esc(label)}</dt><dd>${esc(Array.isArray(known(path)) ? known(path).join(", ") : known(path) || "Not explicitly stated in current material")}</dd></div>`).join("")}</dl><button type="button" class="btn" data-go-materials>Read more material instead of retyping →</button>`)}
  <form id="profile-form"><details class="setup-details"${view === "profile" ? " open" : ""}><summary>Edit contact details, accounts or experience</summary><div class="setup-fields">${field("personal.full_name", "Full name")}${field("personal.email", "Email", "email")}${field("personal.phone", "Phone")}${field("personal.city", "City")}${field("personal.country", "Country")}${field("personal.linkedin_url", "LinkedIn profile", "url")}${field("personal.x_url", "X profile or handle")}${field("personal.github_url", "GitHub profile", "url")}${field("personal.portfolio_url", "Portfolio", "url")}${field("experience.current_job_title", "Most recent title")}${field("experience.current_company", "Most recent employer")}${field("experience.years_of_experience_total", "Stated professional experience")}${field("experience.education_level", "Education")}<label class="setup-field">Professional narrative<textarea data-profile="experience.professional_summary" rows="4">${esc(known("experience.professional_summary"))}</textarea></label></div></details>
  <details class="comfort-disclosure"><summary>Compensation preferences</summary>${card("Compensation strategy", "An opportunity-specific decision, not a universal salary field.", `<label class="setup-field">Your negotiation objective<textarea data-profile="compensation.objective" rows="4">${esc(p.compensation?.objective || state.compensation_default)}</textarea></label>${help("The same runtime policy evaluates responsibility and ownership, job location, employer pay practices, employee/contractor terms, and your evidence. No universal midpoint, percentile or home-country discount is imposed. Required numeric expectations may be reasoned estimates when their basis is clear; they are not presented as verified market data.")}<details class="setup-details"><summary>Optional constraints and retained context</summary><label class="setup-field">Only constraints you actually want applied<textarea data-profile="compensation.compensation_strategy" rows="3">${esc(p.compensation?.compensation_strategy || "")}</textarea></label>${readableValue(p.compensation || {})}${exactRecord(p.compensation || {})}</details>`)}
  </details><details class="setup-details"><summary>Availability & country-specific work authorization</summary>${field("availability.earliest_start_date", "Availability")}<div id="country-facts">${Object.entries(
		p.work_authorization?.by_country || {},
	)
		.map(([c, f]) => countryRow(c, f))
		.join(
			"",
		)}</div><button type="button" class="btn btn-sm" id="add-country-fact">Add a country answer</button></details><div class="setup-savebar"><span id="setup-dirty" hidden>Unsaved changes</span><button class="btn btn-primary" type="submit">Save draft & review changes</button></div></form>
  <details class="comfort-disclosure"><summary>Supporting work & recorded claims · ${state.portfolio.evidence_count} materials</summary>${card(
		"Supporting work",
		"Projects and accomplishments available for tailoring.",
		`<div class="setup-mini-stats"><div><strong>${state.portfolio.evidence_count}</strong><span>source-backed projects / materials</span></div><div><strong>${state.portfolio.claims_count}</strong><span>recorded claims</span></div><div><strong>${state.portfolio.impact_count}</strong><span>impact accomplishments</span></div></div><label class="setup-field">Find a capability, source or contribution<input id="portfolio-filter" type="search" placeholder="Search your evidence"></label><div class="portfolio-list">${
			state.portfolio.evidence
				.map(
					(r) =>
						`<article class="portfolio-item" data-portfolio-text="${esc(JSON.stringify(r).toLowerCase())}"><h3>${esc(r.title)}</h3><p class="fine-print">${esc(r.provider)} · ${esc(r.scope)} · ${r.resume_eligible ? "Selected for tailoring" : "Not selected for tailoring"}</p>${(
							r.allowed_claims || []
						)
							.slice(0, 4)
							.map((t) => `<p>${esc(t)}</p>`)
							.join(
								"",
							)}<details><summary>Contribution, evidence and source details</summary>${readableValue(r)}${exactRecord(r)}</details></article>`,
				)
				.join("") ||
			"<p>Your supporting materials and impact history remain available. Add source evidence in Your material; no technology categories are required.</p>"
		}</div><a class="btn" href="#diary">View or edit your impact history →</a>`,
	)}</details>
  <details class="setup-details"><summary>Source-backed profile suggestions (${state.draft.facts.length})</summary>${state.draft.facts.map((f) => `<details class="setup-fact"><summary>${esc(label(f.path))} · ${esc(f.status)}</summary><p>${esc(Array.isArray(f.value) ? f.value.join(", ") : f.value)}</p><blockquote>${esc(f.quote)}</blockquote><small>${esc(f.source)}</small></details>`).join("")}</details>
  <details class="setup-details"><summary>Original profile & résumé constraints · advanced</summary><p>Preserved employers, projects, education and measured facts remain in the original schema; no duplicate questionnaire.</p><form id="profile-json-form"><label class="setup-field">Profile JSON<textarea id="profile-json" class="setup-code" rows="15" spellcheck="false">${esc(JSON.stringify(p, null, 2))}</textarea></label><button class="btn" type="submit">Save advanced draft</button></form></details>`;
	}
	function impact() {
		return `${card("Your impact diary", `${diaryData.total} existing entries · the same history used by your CLI and tailoring`, `<form id="setup-impact-form"><label class="setup-field">What have you done, learned or measured?<textarea id="setup-impact-note" rows="4" maxlength="16000" required></textarea></label><label class="setup-check"><input id="setup-impact-ai" type="checkbox">Also structure this note using my configured model</label><button class="btn btn-primary" type="submit">Save to impact diary</button></form><p class="fine-print">Saved immediately, independently of the profile draft. Existing accomplishment records already count as diary entries.</p>`)}${
			diaryData.entries
				.slice(0, 12)
				.map((row) => impactEntry(row, { readOnly }))
				.join("") ||
			"<p>No impact entries yet. A useful project decision, measurement or accomplishment is a good start.</p>"
		}<div class="setup-inline"><a class="btn" href="#diary">Open full diary & search →</a><button type="button" class="btn" data-diary-history>Edit history & restore</button></div>`;
	}
	function countryRow(country = "", facts = {}) {
		const options = (value) =>
			[
				["", "Unknown / not answered"],
				["Yes", "Yes"],
				["No", "No"],
			]
				.map(
					([v, l]) =>
						`<option value="${v}"${String(value === true ? "Yes" : value === false ? "No" : (value ?? "")) === v ? " selected" : ""}>${l}</option>`,
				)
				.join("");
		return `<div class="setup-country-row"><label>Country code<input data-country-code maxlength="2" value="${esc(country)}"${country ? " readonly" : ""} placeholder="PK"></label><label>Legally authorized<select data-authorized>${options(facts.legally_authorized_to_work ?? facts.legally_authorized)}</select></label><label>Need sponsorship<select data-sponsorship>${options(facts.require_sponsorship ?? facts.needs_sponsorship)}</select></label></div>`;
	}
	function searches() {
		const queries = state.draft.searches.queries || [];
		const strategy = state.draft.searches;
		return `
  ${card("Search strategy", "Set your priorities and exploration preferences.", `<form id="strategy-form"><h3>Ordered preference rules</h3><div id="strategy-order">${(strategy.preferences || []).map((r) => `<section class="strategy-rule" data-preference-id="${esc(r.id)}"><div class="strategy-rule-head"><strong>${esc(r.id)}</strong><span><button class="btn btn-sm" type="button" data-rule-up aria-label="Move ${esc(r.id)} earlier">↑</button><button class="btn btn-sm" type="button" data-rule-down aria-label="Move ${esc(r.id)} later">↓</button></span></div><details><summary>Matching conditions</summary>${readableValue(r.match || r)}${exactRecord(r)}</details></section>`).join("") || "<p>No ordered preference rules are saved yet. The full strategy editor below supports adding them.</p>"}</div><h3>Exploration allocation</h3><div class="setup-fields">${["discovery_share", "application_share"].map((k) => `<label class="setup-field">${k === "discovery_share" ? "Discovery" : "Application"} exploration share<input type="number" min="0" max="1" step="0.01" data-exploration="${k}" value="${esc(strategy.exploration?.[k] ?? "")}" placeholder="Use existing planner default"></label>`).join("")}</div><label class="setup-field">Exploration probes<textarea id="strategy-probes" class="strategy-probes" placeholder="Capability, problem, domain or query seed—one per line">${esc((strategy.probes || []).join("\n"))}</textarea></label>${help("These are the existing planner’s discovery/application exploration controls. A probe is not an exclusive job-title filter. Preference conditions, market definitions, variants, role filters and custom configuration are retained. Changing a UI label does not create a new planning algorithm.")}<button type="submit" class="btn btn-primary">Save strategy changes to draft</button></form>`)}<div class="setup-columns">
   <details class="setup-details"><summary>Optional query seeds & new-market builder</summary>${card("Query seeds", "Optional queries to guide discovery.", `<form id="search-builder"><div class="setup-role-list">${state.draft.role_suggestions.length ? state.draft.role_suggestions.map((r, i) => `<label class="setup-role"><input type="checkbox" data-role-index="${i}"${queries.some((q) => q.query?.toLowerCase() === r.title.toLowerCase()) ? " disabled" : " checked"}><span><strong>${esc(r.title)}</strong><small>${esc(r.why)}</small></span><select aria-label="Priority for ${esc(r.title)}" data-role-tier="${i}"><option value="1">Tier 1 · closest fit</option><option value="2"${i > 2 ? " selected" : ""}>Tier 2 · adjacent</option><option value="3">Tier 3 · broader</option></select></label>`).join("") : '<p class="fine-print">Generate suggestions in Your material, or add an explicit query here.</p>'}</div><label class="setup-field">Additional discovery queries<textarea id="extra-roles" rows="3" placeholder="One query per line"></textarea></label><details class="setup-details"${!state.draft.searches.queries ? " open" : ""}><summary>Target countries & work arrangements</summary><label class="setup-check"><input id="rebuild-markets" type="checkbox"${!state.draft.searches.queries ? " checked" : ""}>Replace my current market/preference definitions with the simple builder below</label><p class="fine-print">This is a deliberate replacement of market/location/preferences/variants only. Leave unchecked to preserve your existing nuanced portfolio.</p><label class="setup-field">Target countries<input id="target-countries" placeholder="For example Canada, United Arab Emirates, PK"></label><div class="setup-inline">${["remote", "hybrid", "onsite"].map((m) => `<label class="setup-check"><input type="checkbox" data-work-mode="${m}"${m === "remote" ? " checked" : ""}>${m}</label>`).join("")}</div><label class="setup-check"><input id="worldwide" type="checkbox">Include worldwide-remote search opportunities</label><p class="fine-print">A search preference does not establish that any employer can hire you or sponsor you.</p></details><button class="btn btn-primary" type="submit">Add to search draft</button></form>`)}</details>
   ${card(
			"Your search at a glance",
			"Compiled using the same validator as job discovery.",
			`<div class="setup-mini-stats"><div><strong>${state.compiled.active_queries ?? 0}</strong><span>active queries</span></div><div><strong>${state.compiled.active_markets?.length ?? 0}</strong><span>active markets</span></div><div><strong>${state.compiled.preferences?.length ?? 0}</strong><span>ordered preferences</span></div></div><p><strong>Markets</strong><br>${esc((state.compiled.active_markets || []).join(", ") || "Not defined")}</p><p><strong>Preference order</strong><br>${esc((state.compiled.preferences || []).join(" → ") || "No ordered preference rules")}</p><p><strong>Sources</strong><br>${esc((state.compiled.sources || []).join(", ") || "Not defined")}</p><details class="setup-details"><summary>Saved query seeds (secondary inputs)</summary><div class="setup-query-list">${queries
				.slice(0, 80)
				.map(
					(q) =>
						`<div><span class="badge">Tier ${esc(q.tier ?? 1)}</span>${esc(q.query)}</div>`,
				)
				.join(
					"",
				)}</div>${queries.length > 80 ? '<p class="fine-print">Full list remains in the advanced editor below.</p>' : ""}</details>`,
		)}
   </div><details class="setup-details"><summary>Advanced search configuration</summary>${card("Search YAML", "Your original structure is supported. Validation happens before the draft is saved.", `<form id="search-yaml-form"><label class="setup-field">Search YAML<textarea id="search-yaml" rows="19" class="setup-code" spellcheck="false">${esc(state.searches_yaml)}</textarea></label><button class="btn" type="submit">Validate & save search draft</button></form>`)}</details>
   ${card("Discovery access & budgets", "Separate runtime policy; changes do not rewrite your job preferences.", `<form id="source-policy-form">${policyControls(runtimePolicy.policy)}${runtimeControls(runtimePolicy.policy)}<div class="setup-fields"><label class="setup-field">Planner rollout<select id="runtime-rollout"><option value="shadow"${runtimePolicy.policy.rollout_mode === "shadow" ? " selected" : ""}>shadow</option><option value="live"${runtimePolicy.policy.rollout_mode === "live" ? " selected" : ""}>live</option></select></label></div><details class="setup-details"><summary>Explicit source access grants</summary><p>Grant access only to sources/accounts you are entitled to use. Presence of credentials is not an authorization by itself.</p><div class="setup-grants">${runtimePolicy.providers.map((k) => `<label class="setup-check"><span>${esc(k)}</span><select data-provider-grant="${k}" aria-label="${esc(k)} access"><option value=""${runtimePolicy.policy.authorized_sources[k] === undefined ? " selected" : ""}>Default / not granted</option><option value="allow"${runtimePolicy.policy.authorized_sources[k] === true ? " selected" : ""}>Explicitly allow</option><option value="deny"${runtimePolicy.policy.authorized_sources[k] === false ? " selected" : ""}>Explicitly deny</option></select></label>`).join("")}</div></details><button type="submit" class="btn">Save & apply source policy</button></form>`)}
  `;
	}
	function evidence() {
		const cards = state.github
			.map((e) => {
				const privateSource =
					e.private === true || ["private", "internal"].includes(e.visibility);
				const claims = e.claims
					.map(
						(c) =>
							`<div class="setup-evidence-claim"><label><input type="checkbox" data-claim="${esc(c.id)}">Select claim</label><div><strong>${esc(c.allowed_resume_claim)}</strong>${privateSource ? `<label class="setup-field">Public-safe claim — remove confidential names, identifiers and unreleased details<textarea data-disclosure="${esc(c.id)}" rows="3" maxlength="1500" placeholder="Describe the technical contribution without confidential details">${esc(c.allowed_resume_claim)}</textarea></label>` : ""}<small>${esc(c.path)} · ${esc(c.source_kind)}</small><blockquote>${esc(c.excerpt)}</blockquote></div></div>`,
					)
					.join("");
				return card(
					esc(e.title),
					esc(e.ref) +
						" · " +
						(privateSource ? "PRIVATE · " : "PUBLIC · ") +
						"revision " +
						esc(e.version?.slice(0, 12) || "not recorded"),
					`<a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer" class="btn btn-sm">Open repository ↗</a>${privateSource ? '<p class="notice notice-warning">Private source: provenance stays in local intake. Disclosure-safe claims are published automatically; repository names, links and excerpts are omitted.</p>' : ""}${e.fork ? '<p class="notice notice-warning">This is a fork. Upstream work is not automatically your contribution.</p>' : ""}${e.pack_status ? `<p class="fine-print">Collection: ${esc(e.pack_status)} · Extraction: ${esc(e.extraction_status || "pending")}${e.pack_error || e.extraction_error ? `<br>${esc(e.pack_error || e.extraction_error)}` : ""}</p>` : ""}${claims || '<p class="fine-print">No supported claims available yet. Collect again to retry incomplete evidence.</p>'}<details class="setup-details"><summary>Captured observations (${e.observations.length})</summary>${e.observations.map((o) => `<p><strong>${esc(o.path || o.source_kind)}</strong></p><pre class="setup-code">${esc(o.excerpt)}</pre>`).join("")}</details>`,
				);
			})
			.join("");
		return `<form id="evidence-curate">${cards || "<p>Paste a GitHub profile or repository in the link field above. Existing approved claims remain in your profile.</p>"}
   ${state.github.some((e) => e.claims.length) ? `<div class="setup-savebar"><p>Supported claims publish automatically. Select claims to save optional wording changes.</p><button class="btn btn-primary" type="submit">Update selected claims</button></div>` : ""}</form>`;
	}
	function launch() {
		return `<div class="setup-mini-stats"><div><strong>${state.compiled.active_queries ?? 0}</strong><span>active search queries</span></div><div><strong>${state.draft.profile.public_evidence?.length ?? 0}</strong><span>profile evidence entries</span></div><div><strong>${state.changes.filter((c) => c.changed).length}</strong><span>files with proposed changes</span></div></div>
   ${state.live_conflict ? '<div class="notice notice-warning">Live configuration changed since this draft began. Use Start from current files below, then make your edits again. Applying this stale draft is blocked.</div>' : ""}
   ${state.problems.length ? `<div class="notice notice-warning"><strong>Before applying the draft</strong><p>${state.problems.map(esc).join("<br>")}</p></div>` : ""}
   ${state.draft.questions.length ? card("A few decisions still belong to you", "Suggestions cannot decide these personal facts.", state.draft.questions.map((q) => `<p>${esc(q)}</p>`).join("")) : ""}
   ${card("Review the changes", "Review what changes and when it takes effect. Credentials remain hidden.", changeReview(state.changes) + `<form id="commit-form"><label class="setup-check"><input type="checkbox" id="commit-confirm">I have reviewed these facts and search preferences. Apply this draft to my live files, preserving hidden credentials and custom fields.</label><button class="btn btn-primary" type="submit"${state.live_conflict || state.problems.length ? " disabled" : ""}>Apply reviewed changes</button></form>${state.running ? '<p class="fine-print">Autopilot will pause safely, apply your reviewed changes, then resume automatically. You do not need to restart it yourself.</p>' : ""}<div class="setup-inline"><button class="btn btn-quiet" data-action="reset-draft">Start draft from current files</button>${state.backups.length ? `<select id="restore-snapshot" aria-label="Saved configuration snapshot">${state.backups.map((b) => `<option value="${esc(b.id)}">${esc(b.id)}</option>`).join("")}</select><button class="btn btn-quiet" data-action="restore">Restore snapshot</button>` : ""}</div>`)}
   <details class="comfort-disclosure"${view === "setup" ? " open" : ""}><summary>Readiness & startup <span>Check requirements and start the engine</span></summary>
   ${card("Installation readiness", "Required items for the normal engine; optional integrations can remain unconfigured.", `<div class="setup-readiness">${state.readiness.map((r) => `<div><span class="badge ${r.ok ? "green" : r.required ? "orange" : ""}">${r.ok ? "Present" : r.required ? "Required" : "Optional"}</span><strong>${esc(r.component)}</strong><p>${esc(r.detail)}</p></div>`).join("")}</div>`)}
   ${card("Outreach", "Uses your signed-in email and social accounts automatically.", `<p>Outreach is on by default. Manage daily limits or pause sending in Outreach settings.</p><a class="btn" href="#outreach?panel=controls">Outreach settings</a>`)}
   ${card("Ready for autopilot", "Discovery → score → prepare → apply. Your existing permissions and limits remain in effect.", `<p>${state.running ? "Use the Pause button above whenever you need a break. Settings changes handle their own safe pause and resume." : "Your configuration is saved locally. Press Start autopilot above, or start here."}</p>${!state.running ? '<form id="launch-form"><label class="setup-check"><input type="checkbox" id="launch-confirm">Start applications using my committed settings and existing permissions</label><button class="btn btn-primary" type="submit">Start autopilot →</button></form>' : ""}`)}
   </details>
  `;
	}
	function render() {
		if (disposed || !state) return;
		hideSecrets();
		formTrackers.forEach((t) => t.destroy());
		formTrackers = [];
		const expandedResults = new Map(
			$$("[data-task-id]", root).map((item) => [
				item.dataset.taskId,
				$$("details", item).map((d) => d.open),
			]),
		);
		const resultScroll = $(".task-drawer-body", root)?.scrollTop || 0;
		root.innerHTML = `<div class="setup-area"><nav class="setup-steps" aria-label="Setup progress">${steps.map(([id, title, desc], i) => `<button data-step="${id}"${step === id ? ' aria-current="step"' : ""} class="${step === id ? "active" : ""}"><span>${i + 1}</span><strong>${title}</strong><small>${desc}</small></button>`).join("")}</nav><div id="setup-error" class="notice notice-warning" role="alert" hidden></div>${readOnly ? '<div class="notice notice-warning">Read-only viewer: setup changes, account connections and jobs are disabled.</div>' : ""}<div class="setup-step-heading"><div><div class="eyebrow">STEP ${steps.findIndex((s) => s[0] === step) + 1} OF ${steps.length}</div><h2>${steps.find((s) => s[0] === step)[1]}</h2></div><button class="btn btn-quiet" data-action="reload">Refresh setup</button></div>${["profile", "searches", "evidence"].includes(view) ? `<nav class="workspace-tabs" aria-label="Workspace settings"><a href="#profile" ${step === "profile" ? 'aria-current="page"' : ""}>Profile</a><a href="#searches" ${step === "searches" ? 'aria-current="page"' : ""}>Search preferences</a><a href="#evidence" ${step === "materials" ? 'aria-current="page"' : ""}>Material</a><a href="#${view}?step=launch" ${step === "launch" ? 'aria-current="page"' : ""}>Review changes</a></nav><div class="notice draft-scope"><strong>${state.changes.some((c) => c.changed) ? "Draft changes are waiting for review." : "You are editing a draft."}</strong> Profile and search drafts are not live until applied. Diary entries, connections and permissions save separately. <a href="#${view}?step=launch">Review all pending changes →</a></div>` : ""}<div id="setup-content">${{ materials, connections, profile, searches, impact, launch }[step]()}</div><section class="card setup-card setup-task-panel"><div class="card-head"><div><h2>Setup activity</h2><p>Saved task history with timestamps and build IDs. New tasks do not replay after restart.</p></div></div><div class="setup-card-body" id="setup-tasks">${taskBox(lastJobs)}</div></section><div class="setup-bottom-nav"><button class="btn" id="setup-back"${step === "materials" ? " disabled" : ""}>← Previous</button><span>Drafts are local. Your application engine is unchanged.</span><button class="btn btn-primary" id="setup-next"${step === "launch" ? " disabled" : ""}>Next step →</button></div></div>`;
		wire();
		wireHelp(root);
		mountActivity(root, "#setup-tasks");
		for (const item of $$("[data-task-id]", root)) {
			const states = expandedResults.get(item.dataset.taskId);
			if (states)
				$$("details", item).forEach((d, i) => (d.open = states[i] === true));
		}
		if ($(".task-drawer-body", root))
			$(".task-drawer-body", root).scrollTop = resultScroll;
		if (["profile", "searches", "evidence"].includes(view)) {
			$(".setup-steps", root).hidden = true;
			$(".setup-bottom-nav", root).hidden = true;
			$(".setup-step-heading .eyebrow", root).textContent =
				step === "launch" ? "REVIEW BEFORE APPLYING" : "WORKSPACE SETTINGS";
			$('[data-action="reload"]', root).textContent = "Reload saved draft";
		}
		if (view === "connections") {
			$(".setup-steps", root).hidden = true;
			$(".setup-bottom-nav", root).hidden = true;
			$(".setup-step-heading .eyebrow", root).textContent =
				"CONNECTION CATEGORY";
			$(".setup-step-heading h2", root).textContent =
				env.groups.find((g) => g.id === group)?.title || "Connected tools";
			$('[data-action="reload"]', root).textContent = "Reload saved settings";
		}
	}
	async function changeStep(next) {
		if (
			dirty &&
			!(await confirmAction(
				"Leave these unsaved edits? Saved drafts are kept.",
			))
		)
			return;
		dirty = false;
		step = next;
		if (view === "connections") {
			location.hash = "#setup?step=" + encodeURIComponent(next);
			return;
		}
		history.pushState(
			null,
			"",
			"#" +
				(["profile", "searches", "evidence"].includes(view) ? view : "setup") +
				"?step=" +
				encodeURIComponent(next),
		);
		acceptedHash = location.hash;
		render();
		const heading = $(".setup-step-heading h2", root);
		heading.tabIndex = -1;
		heading.focus({ preventScroll: true });
		root.scrollIntoView({ block: "start", behavior: "instant" });
		document.querySelector("#page-status").textContent =
			`Step ${steps.findIndex((s) => s[0] === step) + 1} of ${steps.length}: ${steps.find((s) => s[0] === step)[1]}`;
	}
	function wire() {
		$$("[data-step]", root).forEach(
			(b) => (b.onclick = () => changeStep(b.dataset.step)),
		);
		$("#setup-back", root).onclick = () =>
			changeStep(
				steps[Math.max(0, steps.findIndex((s) => s[0] === step) - 1)][0],
			);
		$("#setup-next", root).onclick = () =>
			changeStep(
				steps[
					Math.min(steps.length - 1, steps.findIndex((s) => s[0] === step) + 1)
				][0],
			);
		$('[data-action="reload"]', root).onclick = async () => {
			if (
				!dirty ||
				(await confirmAction("Discard unsaved edits and load the saved draft?"))
			)
				load();
		};
		$$("input,textarea,select", root).forEach((el) =>
			el.addEventListener("input", setDirty),
		);
		if ($("#share-model", root))
			$("#share-model", root).onchange = (e) => {
				materialConsent = e.target.checked;
			};
		if ($("#auto-suggest", root))
			$("#auto-suggest", root).onchange = (e) => {
				autoSuggest = e.target.checked;
			};
		if ($("#owner-goals", root))
			$("#owner-goals", root).oninput = (e) => {
				materialGoals = e.target.value;
			};
		function form(id, fn) {
			const el = $(id, root);
			if (el)
				el.onsubmit = (e) => {
					e.preventDefault();
					fn();
				};
		}
		function action(name, fn) {
			const el = $(`[data-action="${name}"]`, root);
			if (el) el.onclick = fn;
		}
		async function draft(payload) {
			const result = await perform(() =>
				post("setup/draft", { revision: state.draft.revision, ...payload }),
			);
			if (
				result &&
				!disposed &&
				((["profile", "searches"].includes(view) && !payload.reset) ||
					payload.profile)
			) {
				step = "launch";
				history.replaceState(null, "", "#" + view + "?step=launch");
				acceptedHash = location.hash;
				render();
				notify("Draft saved. Review all pending changes before applying them.");
			}
			return result;
		}
		$$("[data-reveal]", root).forEach(
			(b) =>
				(b.onclick = async () => {
					const input = $("#env-" + b.dataset.reveal, root);
					const hide = () => {
						if (input.value === input._revealedOriginal) input.value = "";
						input.type = "password";
						delete input._revealedOriginal;
						b.textContent = "Show";
					};
					if (b.textContent === "Hide") {
						hide();
						return;
					}
					try {
						const r = await post("setup/reveal-secret", {
							key: b.dataset.reveal,
							revision: env.revision,
							confirm: true,
						});
						if (disposed) return;
						input.type = "text";
						input.value = r.value;
						input._revealedOriginal = r.value;
						b.textContent = "Hide";
						secretHiders.push(hide);
						secretTimers.push(setTimeout(hide, 30000));
						input.onblur = hide;
					} catch (e) {
						notify(e.message, true);
					}
				}),
		);
		for (const [selector, target] of [
			["[data-go-profile]", "profile"],
			["[data-go-impact]", "impact"],
			["[data-go-materials]", "materials"],
		]) {
			const b = $(selector, root);
			if (b) b.onclick = () => changeStep(target);
		}
		const reader = $("#material-reader", root);
		if (reader)
			reader.onchange = () => {
				const browser = reader.value === "browseros";
				$("#material-browser-permission", root).hidden = !browser;
				$("#material-browser-consent", root).required = browser;
			};
		form("#material-url", () => {
			dirty = false;
			launchTask("material_url", {
				url: $("#material-url-input", root).value,
				reader: reader.value,
				browser_consent: $("#material-browser-consent", root).checked,
				include_private: $("#material-github-private", root).checked,
				use_ai: $("#share-model", root).checked,
				share_with_model: $("#share-model", root).checked,
			});
		});
		form("#setup-impact-form", async () => {
			await perform(async () => {
				const r = await post("diary/note", {
					note: $("#setup-impact-note", root).value,
				});
				if ($("#setup-impact-ai", root).checked)
					await post("diary/extract", { id: r.id, confirm_model: true });
				return r;
			});
		});
		form("#intake-file", () => {
			const files = $("#intake-files", root).files;
			if (files.length !== 1) {
				notify(
					"Import one file at a time so each source has its own bounded task.",
					true,
				);
				return;
			}
			const f = files[0];
			if (f.size > 8000000) {
				notify("Choose a file under 8 MB.", true);
				return;
			}
			const reader = new FileReader();
			reader.onload = () => {
				dirty = false;
				launchTask("intake", {
					name: f.name,
					kind: $("#intake-kind", root).value,
					content_base64: reader.result.split(",")[1],
					auto_suggest: $("#auto-suggest", root)?.checked === true,
					share_with_model: $("#share-model", root)?.checked === true,
				});
			};
			reader.readAsDataURL(f);
		});
		const zone = $("#upload-zone", root);
		if (zone) {
			zone.ondragover = (e) => {
				e.preventDefault();
				zone.classList.add("dragover");
			};
			zone.ondragleave = () => zone.classList.remove("dragover");
			zone.ondrop = (e) => {
				e.preventDefault();
				zone.classList.remove("dragover");
				$("#intake-files", root).files = e.dataTransfer.files;
				$("#file-queue", root).textContent = [...e.dataTransfer.files]
					.map((f) => f.name)
					.join(", ");
			};
		}
		form("#intake-paste", () => {
			dirty = false;
			launchTask("intake", {
				text: $("#intake-text", root).value,
				kind: $("#paste-resume", root).checked ? "resume" : "supporting",
				auto_suggest: $("#auto-suggest", root)?.checked === true,
				share_with_model: $("#share-model", root)?.checked === true,
			});
		});
		form("#suggest-form", () => {
			const body = {
				revision: state.draft.revision,
				use_ai: $("#share-model", root).checked,
				share_with_model: $("#share-model", root).checked,
				replace_resume: $("#replace-master", root).checked,
				goals: $("#owner-goals", root).value,
			};
			dirty = false;
			launchTask("suggest", body);
		});
		$$("[data-use-source]", root).forEach(
			(b) =>
				(b.onclick = async () => {
					if (
						await confirmAction(
							"Use this source in the draft? Your live files remain unchanged.",
						)
					)
						draft({ import_source: b.dataset.useSource });
				}),
		);
		$$("[data-remove-source]", root).forEach(
			(b) =>
				(b.onclick = () =>
					perform(() =>
						post("setup/remove-source", { id: b.dataset.removeSource }),
					)),
		);
		$$("[data-env-group]", root).forEach(
			(b) =>
				(b.onclick = async () => {
					if (
						dirty &&
						!(await confirmAction("Discard unsaved connection edits?"))
					)
						return;
					dirty = false;
					group = b.dataset.envGroup;
					render();
				}),
		);
		action("local-defaults", () =>
			perform(() =>
				post("setup/connections", {
					revision: env.revision,
					values: {
						APPLYPILOT_SOURCE_RUNTIME: "auto",
						APPLYPILOT_GMAIL_RUNTIME: "auto",
						APPLYPILOT_SEARCH_BACKEND: "auto",
						APPLYPILOT_CRAWLER: "auto",
						...(!env.fields.find((f) => f.key === "SEARXNG_URL")?.configured
							? { SEARXNG_URL: "http://127.0.0.1:8888" }
							: {}),
					},
					clear: [],
				}),
			),
		);
		const portfolioFilter = $("#portfolio-filter", root);
		if (portfolioFilter)
			portfolioFilter.oninput = () => {
				$$("[data-portfolio-text]", root).forEach(
					(e) =>
						(e.hidden = !e.dataset.portfolioText.includes(
							portfolioFilter.value.toLowerCase(),
						)),
				);
			};
		$$("[data-rule-up],[data-rule-down]", root).forEach(
			(b) =>
				(b.onclick = () => {
					const row = b.closest("[data-preference-id]");
					if (b.hasAttribute("data-rule-up") && row.previousElementSibling)
						row.parentNode.insertBefore(row, row.previousElementSibling);
					else if (b.hasAttribute("data-rule-down") && row.nextElementSibling)
						row.parentNode.insertBefore(row.nextElementSibling, row);
					setDirty();
				}),
		);
		form("#strategy-form", () => {
			const strategy = {
				preference_order: $$("[data-preference-id]", root).map(
					(e) => e.dataset.preferenceId,
				),
				probes: $("#strategy-probes", root)
					.value.split("\n")
					.map((s) => s.trim())
					.filter(Boolean),
				exploration: {},
			};
			$$("[data-exploration]", root).forEach((e) => {
				if (e.value !== "")
					strategy.exploration[e.dataset.exploration] = Number(e.value);
			});
			draft({ strategy });
		});
		form("#env-form", () => {
			const values = {},
				clear = [];
			$$("[data-env]", root).forEach((el) => {
				const f = env.fields.find((f) => f.key === el.dataset.env);
				if (
					f.secret
						? el.value !== "" && el.value !== el._revealedOriginal
						: el.value !== f.value
				)
					values[f.key] = el.value;
			});
			$$("[data-clear]:checked", root).forEach((el) =>
				clear.push(el.dataset.clear),
			);
			perform(() =>
				post("setup/connections", { revision: env.revision, values, clear }),
			);
		});
		$$("[data-verify]", root).forEach(
			(b) =>
				(b.onclick = () => {
					const accounts = {};
					for (const [channel, key] of [
						["linkedin", "linkedin_url"],
						["x", "x_url"],
					]) {
						const value = get(state.draft.profile, "personal." + key);
						if (value) accounts[channel] = value;
					}
					launchTask("verify", { component: b.dataset.verify, accounts });
				}),
		);
		form("#gmail-upload", async () => {
			const f = $("#gmail-client-file", root).files[0];
			if (!f || f.size > 32000) {
				notify("Choose a Desktop OAuth JSON under 32 KB.", true);
				return;
			}
			const raw = await f.text();
			perform(() => post("setup/gmail-client", { client_json: raw }));
		});
		action("gmail-connect", () => {
			dirty = false;
			launchTask("gmail_connect", {
				consent: true,
				email: $("#gmail-account", root).value.trim(),
			});
		});
		form("#profile-form", () => {
			const profile = {};
			$$("[data-profile]", root).forEach((el) => {
				let val = el.dataset.list ? list(el.value) : el.value.trim();
				if (
					el.dataset.profile === "personal.x_url" &&
					val &&
					!String(val).includes("://")
				)
					val = "https://x.com/" + String(val).replace(/^@/, "");
				if (el.value !== el.defaultValue) put(profile, el.dataset.profile, val);
			});
			const by_country = structuredClone(
				state.draft.profile.work_authorization?.by_country || {},
			);
			const initialCountry = JSON.stringify(by_country);
			for (const row of $$(".setup-country-row", root)) {
				const code = $("[data-country-code]", row).value.toUpperCase().trim();
				if (!code) continue;
				const auth = $("[data-authorized]", row),
					sponsor = $("[data-sponsorship]", row);
				const changed = (el) =>
					el.value !== [...el.options].find((o) => o.defaultSelected)?.value;
				if (!by_country[code] || changed(auth) || changed(sponsor)) {
					by_country[code] = {
						...by_country[code],
						legally_authorized_to_work: auth.value || null,
						require_sponsorship: sponsor.value || null,
					};
					delete by_country[code].legally_authorized;
					delete by_country[code].needs_sponsorship;
				}
			}
			if (JSON.stringify(by_country) !== initialCountry)
				put(profile, "work_authorization.by_country", by_country);
			draft({ profile });
		});
		if ($("#add-country-fact", root))
			$("#add-country-fact", root).onclick = () => {
				$("#country-facts", root).insertAdjacentHTML("beforeend", countryRow());
				setDirty();
			};
		form("#profile-json-form", () => {
			try {
				draft({ profile: JSON.parse($("#profile-json", root).value) });
			} catch (e) {
				notify("Profile JSON: " + e.message, true);
			}
		});
		form("#search-builder", () => {
			const roles = $$("[data-role-index]:checked", root).map((el) => ({
				query: state.draft.role_suggestions[Number(el.dataset.roleIndex)].title,
				tier: Number(
					$(`[data-role-tier="${el.dataset.roleIndex}"]`, root).value,
				),
			}));
			roles.push(
				...$("#extra-roles", root)
					.value.split("\n")
					.map((x) => x.trim())
					.filter(Boolean)
					.map((query) => ({ query, tier: 2 })),
			);
			draft({
				builder: {
					roles,
					rebuild_markets: $("#rebuild-markets", root).checked,
					countries: list($("#target-countries", root).value).map((c) =>
						c.toUpperCase(),
					),
					work_modes: $$("[data-work-mode]:checked", root).map(
						(e) => e.dataset.workMode,
					),
					worldwide_remote: $("#worldwide", root).checked,
				},
			});
		});
		form("#search-yaml-form", () =>
			draft({ searches_yaml: $("#search-yaml", root).value }),
		);
		form("#source-policy-form", () => {
			const policy = {
				...readPolicyControls(root),
				authorized_sources: {},
				rollout_mode: $("#runtime-rollout", root).value,
			};
			$$("[data-provider-grant]", root).forEach((el) => {
				if (el.value)
					policy.authorized_sources[el.dataset.providerGrant] =
						el.value === "allow";
			});
			perform(() =>
				post("setup/runtime-policy", {
					revision: runtimePolicy.revision,
					policy,
				}),
			);
		});
		form("#evidence-curate", () =>
			perform(() =>
				post("setup/evidence", {
					revision: state.draft.revision,
					claim_ids: $$("[data-claim]:checked", root).map(
						(e) => e.dataset.claim,
					),
					disclosure_texts: Object.fromEntries(
						$$("[data-disclosure]", root).map((e) => [
							e.dataset.disclosure,
							e.value,
						]),
					),
				}),
			),
		);
		form("#commit-form", () =>
			perform(() =>
				post("setup/commit", {
					revision: state.draft.revision,
					confirm: $("#commit-confirm", root).checked,
				}),
			),
		);
		action("reset-draft", async () => {
			if (
				await confirmAction(
					"Replace this draft with the current live files? Imported materials and GitHub observations are retained.",
				)
			)
				draft({ reset: true });
		});
		action("restore", async () => {
			if (
				await confirmAction(
					"Restore the selected profile/search/résumé snapshot? Autopilot pauses and resumes automatically. Connection secrets are not restored by this control.",
				)
			)
				perform(() =>
					post("setup/restore", {
						backup: $("#restore-snapshot", root).value,
						confirm: true,
					}),
				);
		});

		form("#launch-form", async () => {
			const r = await perform(
				() =>
					post("setup/launch", {
						nonce: state.gate.nonce,
						confirm: $("#launch-confirm", root).checked,
					}),
				false,
			);
			if (r?.starting || ["starting", "running"].includes(r?.state)) {
				dirty = false;
				location.hash = "overview";
				notify("Starting autopilot with your committed settings.");
			}
		});
		wireIntegrations(root, {
			state,
			env,
			runtimePolicy,
			post,
			perform,
			launchTask,
			notify,
			readOnly,
		});
		for (const id of ["launch-form", "commit-form"]) {
			const form = $("#" + id, root);
			if (form) {
				const check = form.querySelector("input[type=checkbox]");
				const button = form.querySelector("button[type=submit]");
				const blocked =
					id === "commit-form" &&
					(state.live_conflict ||
						state.problems.length ||
						!state.changes.some((c) => c.changed));
				const sync = () =>
					(button.disabled = readOnly || blocked || !check.checked);
				check.addEventListener("change", sync);
				sync();
			}
		}
		wireTasks();
		for (const id of [
			"profile-form",
			"profile-json-form",
			"strategy-form",
			"env-form",
			"search-yaml-form",
			"source-policy-form",
			"search-builder",
			"evidence-curate",
		]) {
			const form = $("#" + id, root);
			if (form)
				formTrackers.push(trackForm(form, { readOnly, onChange: recordDirty }));
		}
		recordDirty();
		syncIntegrationButtons(root, lastJobs, state.integrations);
		if (readOnly) {
			$$(
				'form input,form textarea,form select,form button,[data-action]:not([data-action="reload"]),[data-verify],[data-use-source],[data-remove-source],[data-stop-task]',
				root,
			).forEach((el) => (el.disabled = true));
		}
	}
	load().then(() => {
		if (!disposed) poll();
	});
	return cleanup;
}
