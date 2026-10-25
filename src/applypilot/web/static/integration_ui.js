import { confirmAction, trackForm } from "./ux.js";

/* Owner-triggered integrations. No secrets or OAuth codes in persistent browser storage. */
const esc = (v) =>
	String(v ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const $ = (s, r) => r.querySelector(s);
const $$ = (s, r) => [...r.querySelectorAll(s)];
const card = (title, body) =>
	`<section class="card setup-card"><div class="card-head"><h2>${esc(title)}</h2></div><div class="setup-card-body">${body}</div></section>`;
const modes = {
	web_search: ["search"],
	twitter: ["auto", "export", "twikit", "xactions", "x_use"],
	linkedin_posts: [
		"auto",
		"export",
		"linkedin_rs",
		"linkedin_mcp",
		"linkedin_scraper",
	],
};
const labels = {
	web_search: "Web search",
	twitter: "X / Twitter",
	linkedin_posts: "LinkedIn posts",
};

export function limitField(label, attribute, value) {
	return `<div class="setup-field"><label>${esc(label)}<input ${attribute} data-work-limit type="number" min="0" step="1" required value="${value === -1 ? "" : value}"${value === -1 ? " disabled" : ""} placeholder="Uncapped"></label><label class="setup-check"><input type="checkbox" data-uncapped${value === -1 ? " checked" : ""}>Uncapped</label></div>`;
}
const readLimit = (el) =>
	el.closest(".setup-field").querySelector("[data-uncapped]").checked
		? -1
		: Number(el.value);
export function runtimeControls(policy) {
	const volume = [
		"request_budget",
		"http_budget",
		"browser_budget",
		"model_budget",
		"evidence_budget",
		"max_pages_per_task",
		"probe_limit",
	];
	return `<h3>Run volume</h3><p>Uncapped removes ApplyPilot's workload ceiling. Zero pauses a workload. Provider limits still apply.</p><div class="setup-fields">${volume.map((k) => limitField(k.replaceAll("_", " "), `data-runtime="${k}"`, policy[k])).join("")}${limitField("Job-board results per query", "data-site-results", policy.request_defaults.results_per_site)}</div><p>Preparation, discovery and applications share the configured execution workers.</p>`;
}
export function policyControls(policy) {
	const research = policy.research;
	return `<h3>Discovery throughput & research</h3><p>New workspaces default to uncapped result targets. Every connected reader keeps its own pagination contract; a snapshot-only reader is reported as limited coverage, not falsely marked exhaustive.</p><div class="setup-fields">${Object.keys(
		modes,
	)
		.map(
			(name) =>
				`${limitField(labels[name] + " · result target", `data-source-limit="${name}"`, policy.source_inputs[name].limit)}<label class="setup-field">${labels[name]} · reader<select data-source-mode="${name}">${modes[name].map((mode) => `<option value="${mode}"${policy.source_inputs[name].mode === mode ? " selected" : ""}>${esc(mode)}</option>`).join("")}</select></label>`,
		)
		.join("")}</div>
 <label class="setup-check"><input type="checkbox" id="research-enabled"${research.enabled ? " checked" : ""}>Research companies alongside enrichment</label><div class="setup-fields">${limitField("Companies per research pass", 'id="research-companies"', research.companies)}</div><p>Research uses the shared execution workers.</p>
 <details class="setup-details"><summary>Reader-specific options</summary><div class="setup-fields"><label class="setup-field">x-use account name (only for that MCP reader)<input id="source-x-account" value="${esc(policy.source_inputs.twitter.account || "")}" autocomplete="off"></label><label class="setup-field">LinkedIn company URL (only for linkedin_scraper)<input type="url" id="source-li-company" value="${esc(policy.source_inputs.linkedin_posts.company_url || "")}" placeholder="https://www.linkedin.com/company/example/"></label></div><p>Auto uses compatible, already connected readers. The company scraper cannot search arbitrary role keywords.</p></details>`;
}
export function readPolicyControls(root) {
	const source_inputs = {};
	$$("[data-source-limit]", root).forEach(
		(el) => (source_inputs[el.dataset.sourceLimit] = { limit: readLimit(el) }),
	);
	$$("[data-source-mode]", root).forEach(
		(el) => (source_inputs[el.dataset.sourceMode].mode = el.value),
	);
	source_inputs.twitter.account = $("#source-x-account", root).value.trim();
	source_inputs.linkedin_posts.company_url = $(
		"#source-li-company",
		root,
	).value.trim();
	const policy = {
		source_inputs,
		research: {
			enabled: $("#research-enabled", root).checked,
			companies: readLimit($("#research-companies", root)),
		},
		request_defaults: {
			results_per_site: readLimit($("[data-site-results]", root)),
		},
	};
	$$("[data-runtime]", root).forEach(
		(el) =>
			(policy[el.dataset.runtime] = el.hasAttribute("data-work-limit")
				? readLimit(el)
				: Number(el.value)),
	);
	return policy;
}
export function connectionCards(
	group,
	state,
	policy,
	env,
	maximumApplied = false,
) {
	const integrations = state.integrations || {},
		github = integrations.github || {},
		mcp = integrations.mcp || {},
		servers = mcp.servers || [];
	let html =
		group === "sources"
			? `<details class="setup-details"><summary>Discovery workload preset</summary>${card("Local workload defaults", `<p>Uncapped built-in workload defaults need no environment flags. Apply the preset once to migrate existing positive caps to <strong>Uncapped</strong>, using the configured shared execution workers. Explicit zero/paused settings stay paused. Paid-service and outreach budgets are unchanged. Research still uses your configured model and its quotas.</p><button class="btn btn-primary" data-action="max-local" ${maximumApplied ? 'disabled aria-disabled="true"' : ""}>${maximumApplied ? "Uncapped free/local preset is active" : "Use Uncapped free/local preset"}</button> <a class="btn" href="#setup?step=searches">Adjust throughput & research</a>${Object.keys(integrations.research_overrides || {}).length ? `<p class="notice notice-warning">Environment research overrides are active: ${esc(JSON.stringify(integrations.research_overrides))}. They win over dashboard policy.</p><button class="btn" data-action="research-dashboard">Use dashboard instead of workspace research overrides</button>` : ""}`)}</details>`
			: "";
	if (["github", "backup"].includes(group)) {
		const tokenOverride = env.fields.some(
			(f) =>
				[
					"GITHUB_TOKEN",
					"GH_TOKEN",
					...(group === "backup" ? ["APPLYPILOT_BACKUP_TOKEN"] : []),
				].includes(f.key) && f.configured,
		);
		html += card(
			"GitHub · browser sign-in",
			`<p>${github.login ? `Last verified account: <strong>${esc(github.login)}</strong>.` : "Connect your existing GitHub CLI login, or approve sign-in in your browser."} ${github.cli_installed ? "GitHub CLI is installed." : "Install GitHub CLI on this machine once, then reload this page."}</p><div class="setup-inline"><button class="btn btn-primary" data-action="github-connect">Connect GitHub</button><button class="btn" data-action="github-reconnect">Reconnect / change GitHub account</button>${!github.cli_installed ? '<a class="btn" href="https://cli.github.com/" target="_blank" rel="noopener noreferrer">Get GitHub CLI ↗</a>' : ""}</div><p>No personal token needs to be created or pasted. GitHub CLI owns OAuth credential storage; ApplyPilot saves the verified account name.</p>${tokenOverride ? '<p class="notice notice-warning">Manual tokens are saved. Evidence tokens apply only to evidence reads; backup credentials follow the saved authentication mode in Backups. GitHub CLI mode ignores the backup token without deleting it.</p>' : ""}<div class="setup-inline"><button class="btn" data-action="github-repo" ${github.backup_repository ? "disabled" : ""}>${github.backup_repository ? "Backup repository already configured" : "Connect & prepare private backup"}</button><a class="btn" href="#backups">Recovery key, schedule & first snapshot →</a></div><p class="fine-print">Creating an empty repository does not upload your data or enable scheduled backups. GitHub CLI OAuth is broader than a repository-scoped PAT; keep a PAT only when you need that narrower boundary.</p>`,
		);
	}
	if (["sources", "browser"].includes(group)) {
		html += card(
			"Your MCP servers · reuse mcporter",
			`<p>${mcp.installed ? "mcporter detected." : "mcporter is not visible to this process. Run ApplyPilot under the same OS account and PATH as your existing MCP clients."} BrowserOS checks existing Codex, Cursor, VS Code and mcporter configuration automatically; Neo is preferred when reachable. No URL needs to be pasted.</p><button class="btn btn-primary" data-action="mcporter-scan">Discover / refresh MCP servers</button><p class="fine-print">Discovery checks tools, not deployment actions, with a 2-second per-server timeout. No automatic authentication. Slow servers can be retried in mcporter.</p>${(mcp.browser_candidates || []).map((r) => `<p><strong>${esc(r.name)}</strong> · ${esc(r.url)} <small>configuration detected, not a live check</small></p>`).join("")}${servers.length ? `<div class="setup-stack">${servers.map((row) => `<div class="setup-inline"><strong>${esc(row.name)}</strong><span class="badge ${row.status === "ok" ? "green" : "orange"}">${esc(row.status)} · ${row.tool_count || 0} tools</span>${row.can_auth ? `<button class="btn" data-mcp-auth="${esc(row.name)}">Authenticate in browser</button>` : ""}</div>`).join("")}</div>` : ""}<p>Cloudflare, Vercel and Railway are shown, not injected into the job services. Only supported read tools may be bound below; an HTTP 400 is not treated as an expired login.</p>${servers.length ? `<details class="setup-details"><summary>Connect an existing social MCP reader</summary><form id="mcp-binding" class="setup-stack"><label class="setup-field">Reader<select id="mcp-provider"><option value="linkedin_mcp">Custom LinkedIn MCP</option><option value="xactions">XActions</option><option value="x_use">x-use</option></select></label><label class="setup-field">Your configured server<select id="mcp-server">${servers.map((r) => `<option value="${esc(r.name)}">${esc(r.name)} · ${esc(r.status)}</option>`).join("")}</select></label><label class="setup-check"><input id="mcp-read-consent" type="checkbox" required>I own this session and have disabled paid modes, background writes and automatic account importing in this server</label><button class="btn" type="submit">Use this reader</button><p>ApplyPilot verifies required tool names before binding and calls only its fixed read allowlist. Configure x-use's account name in Your search → Reader-specific options.</p></form><pre class="setup-code">${esc(JSON.stringify(mcp.bindings || {}, null, 2))}</pre></details>` : ""}`,
		);
	}
	if (group === "sources")
		html += card(
			"Connect your social accounts",
			`<p>Connect once. ApplyPilot reuses your saved login and prepares the compatible readers automatically. When a browser approval is needed, finish it in the window that opens.</p><div class="social-connect-actions"><button class="btn btn-primary" data-social-connect="linkedin_scraper">Connect LinkedIn</button><button class="btn btn-primary" data-social-connect="twikit">Connect X / Twitter</button></div><p class="fine-print">Saved sessions support discovery. Outreach reuses your signed-in browser accounts and follows your outreach settings.</p><details class="setup-details"><summary>Advanced · reader status, import or change login</summary>${(integrations.sessions || []).map((row) => `<section class="setup-task"><div><strong>${esc(row.provider)}</strong><span class="badge ${row.session_ready ? "green" : "orange"}">${row.session_ready ? "Session saved · not live-verified" : row.present ? "Session needs attention" : "Not connected"}</span></div><p>${esc(row.detail || "")}</p><div class="setup-inline"><button class="btn" data-social-connect="${esc(row.provider)}" data-reconnect="true">Sign in again</button><label class="btn file-import">Import session<input type="file" data-session-file="${esc(row.provider)}" accept=".json,.txt"></label></div></section>`).join("")}<p class="fine-print">Managed sessions are private and excluded from backups. Login and two-factor approval remain yours. Optional reader availability does not erase a successfully saved session.</p></details>`,
		);

	return html;
}

export function wireIntegrations(
	root,
	{ state, env, runtimePolicy, post, perform, launchTask, notify, readOnly },
) {
	$$("[data-uncapped]", root).forEach(
		(toggle) =>
			(toggle.onchange = () => {
				const input = toggle
					.closest(".setup-field")
					.querySelector("[data-work-limit]");
				input.disabled = toggle.checked;
				if (!toggle.checked && !input.value) input.value = "500";
			}),
	);
	const action = (name, fn) => {
		const el = $(`[data-action="${name}"]`, root);
		if (el) el.onclick = fn;
	};
	action("max-local", async () => {
		if (
			await confirmAction(
				"Apply this preset and automatically pause/resume autopilot as needed? Remove positive discovery/research workload caps and use CPU-aware finite pools? Explicit zeros/pauses, denies, populated exports, paid budgets and outreach remain unchanged. Metered model calls can still cost money.",
			)
		)
			perform(() =>
				post("setup/max-local", {
					revision: runtimePolicy.revision,
					confirm: true,
				}),
			);
	});
	action("research-dashboard", async () => {
		if (
			await confirmAction(
				"Clear only workspace research environment overrides? Shell/project overrides cannot be removed by this button and may still take precedence.",
			)
		)
			perform(() =>
				post("setup/connections", {
					revision: env.revision,
					values: {},
					clear: [
						"APPLYPILOT_RESEARCH_ON_ENRICH",
						"APPLYPILOT_RESEARCH_COMPANIES",
					],
				}),
			);
	});
	for (const [name, reconnect] of [
		["github-connect", false],
		["github-reconnect", true],
	])
		action(name, async () => {
			if (
				await confirmAction(
					"Connect your own GitHub account using GitHub CLI? Approve GitHub CLI access in the browser. No backup upload is enabled by this action.",
				)
			)
				launchTask("github_connect", { consent: true, reconnect });
		});
	action("github-repo", async () => {
		if (
			await confirmAction(
				"Connect your GitHub account and prepare your private applypilot-backup repository? Existing private repositories are reused; nothing is uploaded until you authorize backups.",
			)
		)
			launchTask("github_backup_repo", { confirm: true });
	});
	action("mcporter-scan", () => launchTask("mcporter_scan", {}));
	$$("[data-mcp-auth]", root).forEach(
		(b) =>
			(b.onclick = async () => {
				if (
					await confirmAction(
						"Open the configured sign-in flow for " +
							b.dataset.mcpAuth +
							" on the machine running ApplyPilot?",
					)
				)
					launchTask("mcporter_auth", {
						server: b.dataset.mcpAuth,
						consent: true,
					});
			}),
	);
	$$("[data-social-connect]", root).forEach(
		(b) =>
			(b.onclick = () =>
				launchTask("social_connect", {
					provider: b.dataset.socialConnect,
					browser_backend: "cloakbrowser",
					consent: true,
					reconnect: b.dataset.reconnect === "true",
				})),
	);
	$$("[data-session-file]", root).forEach(
		(input) =>
			(input.onchange = async () => {
				const file = input.files[0];
				if (!file) return;
				if (file.size > 8000000) {
					notify("Choose a session file under 8 MB.", true);
					input.value = "";
					return;
				}
				if (
					!(await confirmAction(
						"Import your own " +
							input.dataset.sessionFile +
							" session into the workspace? This replaces its managed session file, not any source file. Autopilot pauses and resumes automatically if needed.",
					))
				) {
					input.value = "";
					return;
				}
				const reader = new FileReader();
				reader.onerror = () =>
					notify("Could not read that session file.", true);
				reader.onload = () => {
					const content = String(reader.result).split(",")[1];
					launchTask("session_import", {
						provider: input.dataset.sessionFile,
						file_base64: content,
						consent: true,
					});
					input.value = "";
				};
				reader.readAsDataURL(file);
			}),
	);
	$$("[data-session-reuse]", root).forEach(
		(b) =>
			(b.onclick = async () => {
				if (
					await confirmAction(
						"Reuse your existing authorized session for " +
							b.dataset.sessionReuse +
							"? Only the managed destination is updated; no passwords are requested and no messages are sent. Autopilot pauses and resumes automatically if needed.",
					)
				)
					launchTask("session_reuse", {
						provider: b.dataset.sessionReuse,
						consent: true,
					});
			}),
	);
	const binding = $("#mcp-binding", root);
	if (binding)
		binding.onsubmit = (e) => {
			e.preventDefault();
			perform(() =>
				post("setup/mcp-binding", {
					provider: $("#mcp-provider", root).value,
					server: $("#mcp-server", root).value,
					read_only_confirmed: $("#mcp-read-consent", root).checked,
				}),
			);
		};
	if (readOnly)
		$$(
			"[data-mcp-auth],[data-social-connect],[data-session-file],[data-session-reuse],#social-browser",
			root,
		).forEach((el) => (el.disabled = true));
}

export function syncIntegrationButtons(root, tasks = [], integrations = {}) {
	const github = integrations?.github || {};
	const active = tasks.find(
		(t) =>
			["github_connect", "github_backup_repo"].includes(t.operation) &&
			["queued", "running"].includes(t.state),
	);
	const completed = tasks.find(
		(t) =>
			t.operation === "github_connect" &&
			t.state === "completed" &&
			t.result?.login,
	);
	const login = completed?.result?.login || github.login;
	const connect = root.querySelector('[data-action="github-connect"]'),
		reconnect = root.querySelector('[data-action="github-reconnect"]');
	if (connect) {
		connect.disabled = !!active || !!login || !github.cli_installed;
		connect.textContent = active
			? "Connecting GitHub…"
			: login
				? "Connected · " + login
				: "Connect GitHub";
		connect.title = login
			? "Last verified account. Use Reconnect to verify or change it."
			: !github.cli_installed
				? "Install GitHub CLI on this machine first"
				: "";
	}
	if (reconnect) {
		reconnect.disabled = !!active || !github.cli_installed;
		reconnect.textContent = "Recheck / change account";
	}
	if (root.dataset.readOnly === "true") {
		if (connect) connect.disabled = true;
		if (reconnect) reconnect.disabled = true;
	}
}
