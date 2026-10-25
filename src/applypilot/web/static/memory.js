import { attachDiaryEditor } from "./diary_editor.js";
import {
	mountActivity,
	renderTaskResults,
	setActivityOpen,
	showActivityRuntime,
	taskDetails,
	taskRuntimeLine,
	unmountActivity,
} from "./panels.js";
import { exactRecord, readableValue } from "./readable.js";
import { impactEntry, tokenLink } from "./setup_ui.js";
import { confirmAction, trackForm, updatesPaused } from "./ux.js";

/* Persistent diary and recovery controls. All untrusted data is escaped. */
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
const card = (title, subtitle, body) =>
	`<section class="card"><div class="card-head"><div><h2>${esc(title)}</h2><p>${esc(subtitle)}</p></div></div><div class="card-content setup-stack">${body}</div></section>`;
const date = (v) =>
	v
		? new Date(typeof v === "number" ? v * 1000 : v).toLocaleString()
		: "Not yet";
let cleanup = () => {};
export function unmountMemory() {
	cleanup();
	cleanup = () => {};
}
export function mountMemory(root, { api, notify, view, readOnly }) {
	unmountMemory();
	const formTrackers = [];
	let taskSignature = "",
		disposed = false,
		timer,
		dirty = false,
		page = 1,
		query = "",
		lastNote = null,
		backupStatus = null,
		actionBusy = false,
		setupBusy = false;
	const destroyEditor = attachDiaryEditor(root, {
		api,
		notify,
		onChanged: loadDiary,
		readOnly,
	});
	let temporalRequest = null;
	const post = async (path, body = {}) => {
		const result = await api(path, {
			method: "POST", body,
			signal: AbortSignal.timeout(path === "backups/settings" ? 90000 : 20000),
		});
		if (result.accepted && result.workflow_id) {
			temporalRequest = result;
			notify("Temporal accepted the request. Execution depends on an available worker: " + result.workflow_id);
		}
		return result;
	};
	const beforeUnload = (e) => {
		if (dirty) {
			e.preventDefault();
			e.returnValue = "";
		}
	};
	const beforeNavigate = (e) => {
		if (e.detail && dirty)
			e.detail.confirmations.push("Leave unsaved changes?");
	};
	window.addEventListener("applypilot:before-navigate", beforeNavigate);
	window.addEventListener("beforeunload", beforeUnload);
	cleanup = () => {
		formTrackers.forEach((t) => t.destroy());
		window.removeEventListener("applypilot:before-navigate", beforeNavigate);
		unmountActivity();
		destroyEditor();
		disposed = true;
		clearTimeout(timer);
		window.removeEventListener("beforeunload", beforeUnload);
	};
	function backupControls() {
		if (view !== "backups" || !backupStatus) return;
		const ready = backupStatus.readiness || { ready: false, problems: [] };
		const sync = $('[data-task="sync"]', root),
			enable = $("#backup-enable", root),
			hint = $("#backup-prerequisites", root);
		if (sync) {
			sync.disabled =
				readOnly ||
				actionBusy ||
				setupBusy ||
				backupStatus.active ||
				!ready.ready;
			sync.textContent = backupStatus.active
				? "Backup/recovery in progress"
				: backupStatus.state?.status === "authentication_required"
					? "Reconnect & resume backup"
					: "Back up now";
		}
		if (enable)
			enable.disabled =
				readOnly ||
				actionBusy ||
				setupBusy ||
				backupStatus.active ||
				!$("#backup-consent", root)?.checked ||
				(!$("#backup-config", root)?._formState?.dirty() &&
					backupStatus.config.enabled);
		const disable = $("#backup-config button[value=disable]", root);
		if (disable)
			disable.disabled =
				readOnly ||
				actionBusy ||
				setupBusy ||
				backupStatus.active ||
				(!backupStatus.config.enabled &&
					!$("#backup-config", root)?._formState?.dirty());
		const connect = $(
			'[data-backup-setup=github_connect]:not([data-reconnect="true"])',
			root,
		);
		if (connect) {
			const account = backupStatus.auth?.account;
			connect.disabled = readOnly || actionBusy || setupBusy || !!account;
			connect.textContent = setupBusy
				? "Connection task in progress…"
				: account
					? "Connected · " + account
					: "Connect GitHub";
		}
		const create = $('[data-backup-setup="github_backup_repo"]', root);
		if (create) {
			create.disabled =
				readOnly || actionBusy || !!backupStatus.config.repository;
			create.textContent = backupStatus.config.repository
				? "Private backup repository configured"
				: "Connect GitHub & prepare backup";
		}
		const auth = $("#backup-auth-status", root),
			selected = backupStatus.auth;
		if (auth && selected)
			auth.textContent =
				"Saved authentication: " +
				(selected.source === "github_cli"
					? "GitHub CLI OAuth"
					: "manual backup token") +
				(selected.account ? " · " + selected.account : "") +
				(selected.token_ignored
					? ". Saved backup token is ignored in this mode."
					: "") +
				". Saved credentials are checked automatically; required GitHub approval continues the same backup.";
		if (hint)
			hint.textContent = !ready.ready
				? ready.problems?.join(". ") ||
					"Connect your private repository, then confirm private-data upload and authorize backups."
				: backupStatus.active
					? "A backup or recovery is in progress. Its progress is shown below."
					: "Backups enabled. Your GitHub access lets you restore without a recovery key.";
	}
	async function action(fn) {
		if (actionBusy) return null;
		actionBusy = true;
		formTrackers.forEach((t) => t.busy(true));
		const buttons = $$("button:not([data-close-activity])", root).map((b) => [
			b,
			b.disabled,
		]);
		buttons.forEach(([b]) => (b.disabled = true));
		try {
			const result = await fn();
			if (result?.task_id) setActivityOpen(root, true);
			return result;
		} catch (e) {
			notify(e.message, true);
			const el = $("#memory-error", root);
			if (el) {
				el.hidden = false;
				el.textContent = e.message;
			}
			return null;
		} finally {
			actionBusy = false;
			formTrackers.forEach((t) => t.busy(false));
			buttons.forEach(([b, disabled]) => {
				if (b.isConnected) b.disabled = readOnly || disabled;
			});
			formTrackers.forEach((t) => t.refresh());
			backupControls();
		}
	}

	function tasks(rows) {
		return (
			rows
				.filter(
					(x) =>
						x.operation.startsWith("backup_") ||
						x.operation.startsWith("impact_") ||
						x.operation.startsWith("github_"),
				)
				.slice(0, 12)
				.map(
					(j) =>
						`<article class="setup-task" data-task-id="${esc(j.id)}"><div><strong>${esc(j.operation.replaceAll("_", " "))}</strong><span class="badge ${j.state === "completed" ? "green" : j.state === "failed" ? "red" : "orange"}">${esc(j.state)}</span></div>${taskRuntimeLine(j)}<p>${esc(j.message)}</p>${j.auth_url ? `<a class="btn btn-primary" href="${esc(j.auth_url)}" target="_blank" rel="noopener noreferrer">Approve GitHub sign-in ↗</a>` : ""}${j.device_code ? `<p>One-time code: <strong>${esc(j.device_code)}</strong> <button class="btn btn-sm" data-backup-copy-code="${esc(j.device_code)}">Copy code</button></p>` : ""}${["queued", "running"].includes(j.state) ? `<button class="btn btn-sm" data-backup-cancel="${esc(j.id)}">Cancel this task</button>` : ""}${taskDetails(j)}</article>`,
				)
				.join("") ||
			'<p class="fine-print">No manual backup or impact tasks yet.</p>'
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
			const [s, t] = await Promise.all([api("backups"), api("setup/tasks")]);
			if (disposed || updatesPaused()) return;
			if (temporalRequest) {
				const observation = await api("temporal/workflow?" + new URLSearchParams({
					workflow_id: temporalRequest.workflow_id,
					run_id: temporalRequest.run_id || "",
				}));
				if (!["RUNNING", "UNKNOWN"].includes(observation.status)) {
					notify("Temporal backup execution: " + observation.status,
						observation.status !== "COMPLETED");
					temporalRequest = null;
				}
			}
			setupBusy = t.tasks.some((x) => ["queued", "running"].includes(x.state));
			showActivityRuntime(root, t.runtime);
			const signature = JSON.stringify(t.tasks.map((x) => [x.id, x.state]));
			if (view !== "backups" && signature !== taskSignature) {
				taskSignature = signature;
				await loadDiary();
			}
			const box = $("#memory-tasks", root);
			if (box) {
				renderTaskResults(box, tasks(t.tasks));
				$$("[data-backup-copy-code]", box).forEach(
					(b) =>
						(b.onclick = async () => {
							try {
								await navigator.clipboard.writeText(b.dataset.backupCopyCode);
								notify("One-time code copied.");
							} catch {
								notify("Select the displayed code to copy it.", true);
							}
						}),
				);
				$$("[data-backup-cancel]", box).forEach((b) => {
					b.disabled = readOnly;
					b.onclick = () =>
						action(() =>
							post("setup/task/cancel", { id: b.dataset.backupCancel }),
						);
				});
			}
			if (view === "backups") {
				backupStatus = s;
				backupControls();
				const repo = $("#backup-config [name=repository]", root);
				if (repo && !repo.value && !dirty) {
					repo.value = s.config.repository || "";
					$("#backup-config", root)?._formState?.reset();
				}
				const state = s.state;
				$("#backup-health", root).innerHTML =
					`<div class="hero-strip"><div><h3>${s.snapshot_overdue ? "Your recovery snapshot needs attention" : state.snapshot ? "Your recovery point is recorded" : "Create your first recovery point"}</h3><p>Git checkpoint: ${esc(date(state.synced_at))} · Full database: ${esc(date(state.snapshot?.created_at))}</p><p>${esc(state.progress || state.status || "Backups not started")}${state.transfer?.expected_bytes ? " · " + esc(Math.floor((state.transfer.received_bytes || 0) / 1048576)) + " / " + esc(Math.ceil(state.transfer.expected_bytes / 1048576)) + " MiB read back" : ""}${state.retry_at > Date.now() / 1000 ? " · retry after " + esc(date(state.retry_at)) : ""}</p>${state.error ? `<p class="error-text">${esc(state.error)}</p>` : ""}${state.warning ? `<p>${esc(state.warning)}</p>` : ""}</div></div>`;
				const report = $("#backup-check", root),
					reportValue = JSON.stringify(state.connection || null);
				if (report.dataset.value !== reportValue) {
					report.dataset.value = reportValue;
					report.innerHTML = state.connection
						? readableValue(state.connection) +
							exactRecord(state.connection, "Exact repository check · JSON")
						: '<p class="fine-print">Check repository verifies access and reads back the encrypted repository to check its integrity. This can take time. Saving settings alone does not verify a backup.</p>';
				}
				const select = $("#restore-point", root);
				const selected = select.value;
				select.innerHTML =
					'<option value="">Latest snapshot + newest Git checkpoint</option>' +
					(state.points || [])
						.map(
							(x) =>
								`<option value="${esc(x.tag)}">${esc((x.created_at ? date(x.created_at) + " · " : "") + "Snapshot " + x.tag.slice(7, 19))}</option>`,
						)
						.join("");
				select.value = selected;
				$("#rollback-box", root).hidden = !s.restore?.state;
			}
		} catch (e) {
			if (!disposed) notify(e.message, true);
		} finally {
			if (!disposed) timer = setTimeout(poll, 4000);
		}
	}
	async function loadDiary() {
		const data = await api("diary?" + new URLSearchParams({ q: query, page }));
		if (disposed) return;
		$("#diary-count", root).textContent =
			`${data.total} diary entries · includes ${data.records_total} structured accomplishments`;
		$("#diary-entries", root).innerHTML =
			data.entries
				.map((row) => impactEntry(row, { extract: true, readOnly }))
				.join("") || "<p>No diary entries match this filter.</p>";
		$$("[data-extract]", root).forEach(
			(b) =>
				(b.onclick = () =>
					action(async () => {
						if (
							!(await confirmAction(
								"Use your configured model to extract evidence from this saved note? Normal model quotas/costs apply. The original note stays intact.",
							))
						)
							return;
						await post("diary/extract", {
							id: b.dataset.extract,
							confirm_model: true,
						});
						notify("Impact extraction started; progress is below.");
					})),
		);
		$("#diary-page", root).textContent = `Page ${page}`;
		$("#diary-prev", root).disabled = page === 1;
		$("#diary-next", root).disabled = page * 30 >= data.total;
	}
	async function boot() {
		try {
			if (view === "backups") {
				const s = await api("backups");
				if (disposed) return;
				backupStatus = s;
				const c = s.config;
				root.innerHTML = `<div id="memory-error" class="notice notice-warning" role="alert" hidden></div><div id="backup-health"></div><div class="notice">Your private GitHub repository holds complete recovery points for your databases and documents, including saved ATS accounts. Sign in to GitHub to back up or restore; there is no recovery key to remember. Anyone with repository read access can restore the data. OAuth credentials and browser sessions are excluded.</div><div class="two-col">
   ${card("Connect your recovery repository", "Your private GitHub access protects recovery. A dedicated repository is recommended.", `<div class="setup-inline"><button class="btn btn-primary" data-backup-setup="github_backup_repo" type="button" ${c.repository ? "disabled" : ""}>${c.repository ? "Private backup repository configured" : "Connect GitHub & prepare backup"}</button></div><p>Saved sign-ins are reused automatically. Back up now handles reconnection when GitHub needs approval, then continues the same backup. No email access or separate key setup is needed.</p><details class="setup-details"><summary>Advanced · change GitHub login</summary><button class="btn" data-backup-setup="github_connect" data-reconnect="true" type="button">Change GitHub login</button></details><p class="fine-print">Choose GitHub CLI OAuth below to use your connected account. An old manual token cannot override CLI mode.</p><p id="backup-auth-status" class="notice" role="status"></p><details class="setup-details"><summary>Advanced · optional repository-scoped token</summary><form id="backup-credentials" class="setup-stack"><a class="btn" href="${tokenLink("write")}" target="_blank" rel="noopener noreferrer">Create scoped token ↗</a><label class="setup-field">GitHub backup token<input id="backup-token" type="password" autocomplete="new-password" placeholder="${s.token_saved ? "Saved · used only when token mode is selected" : "Only used in manual token mode"}"></label><button class="btn" type="submit">Save GitHub token</button></form></details>`)}
   ${card("Automatic backup settings", "No second everyday command. Runs alongside autopilot or an open dashboard process.", `<form id="backup-config" class="setup-stack"><label class="setup-field">Backup authentication<select name="auth_mode"><option value="github_cli" ${c.auth_mode === "github_cli" ? "selected" : ""}>GitHub CLI OAuth · ignore saved manual tokens</option><option value="token" ${c.auth_mode === "token" ? "selected" : ""}>Manual repository token only</option><option value="auto" ${c.auth_mode === "auto" ? "selected" : ""}>Automatic · prefer connected CLI account</option></select></label><p class="fine-print">The saved account is pinned. Reconnecting another GitHub account does not silently move this backup or change its destination.</p><label class="setup-field">Private repository<input name="repository" value="${esc(c.repository)}" placeholder="your-name/applypilot-backup" required></label><div class="setup-fields"><label class="setup-field">Git checkpoint interval (minutes)<input name="static_minutes" type="number" min="1" max="1440" value="${c.static_minutes}"></label><label class="setup-field">Full snapshot interval (hours)<input name="snapshot_hours" type="number" min="1" max="168" value="${c.snapshot_hours}"></label><label class="setup-field">Recent full snapshots to keep<input name="keep_snapshots" type="number" min="3" max="365" value="${c.keep_snapshots}"></label><label class="setup-field">Maximum snapshot size (GiB)<input name="max_snapshot_gb" type="number" min="1" max="512" value="${c.max_snapshot_gb}"></label></div><label class="setup-check"><input name="include_documents" type="checkbox" ${c.include_documents ? "checked" : ""}>Include optional files/ documents (may contain ID or personal documents; encrypted)</label><p id="backup-prerequisites" class="notice" role="status"></p><label class="setup-check"><input id="backup-consent" type="checkbox">I authorize personal data uploads and understand that anyone with read access to this private repository can restore the data</label><div class="setup-inline"><button id="backup-enable" class="btn btn-primary" type="submit" value="enable">Authorize & save GitHub backup</button><button class="btn" type="submit" value="disable">Save with backups disabled</button></div></form>`)}
   </div>${card("Check, snapshot, recover", "Restoration is explicit. Upload sync never pulls over your local data.", `<div class="setup-inline"><button class="btn" data-task="check">Check repository</button><button class="btn btn-primary" data-task="sync">Back up now</button><button class="btn" data-task="points">List recovery points</button></div><div id="backup-check"></div><label class="setup-field">Recovery point<select id="restore-point"><option value="">Latest snapshot + newest Git checkpoint</option></select></label><p>Stop application workers before restoring. Recovery keeps a local rollback copy, rebases saved document paths, and disables old outreach grants. Data created after a snapshot may be missing; reconnect accounts before resuming.</p><button id="restore-backup" class="btn">Restore from GitHub</button><div id="rollback-box" class="notice notice-warning" hidden><p>An interrupted recovery needs rollback before workers can start.</p><button id="rollback-backup" class="btn">Roll back interrupted recovery</button></div>`)}${card("Operation history", "A recovery point is recorded only after its encrypted data passes verification.", `<div id="memory-tasks"></div>`)}`;
				$("#backup-credentials", root).onsubmit = (e) => {
					e.preventDefault();
					action(async () => {
						const catalog = await api("setup/connections");
						const values = {};
						if ($("#backup-token", root).value)
							values.APPLYPILOT_BACKUP_TOKEN = $("#backup-token", root).value;
						await post("setup/connections", {
							revision: catalog.revision,
							values,
							clear: [],
						});
						$("#backup-token", root).value = "";
						dirty = false;
						$("#backup-credentials", root)?._formState?.reset();
						backupStatus = await api("backups");
						backupControls();
						notify("GitHub backup token saved locally.");
					});
				};
				$("#backup-config", root).onsubmit = (e) => {
					e.preventDefault();
					action(async () => {
						const f = new FormData(e.target),
							data = {
								repository: f.get("repository"),
								auth_mode: f.get("auth_mode"),
								enabled: e.submitter?.value !== "disable",
								include_documents: f.has("include_documents"),
								confirm_private_upload: $("#backup-consent", root).checked,
							};
						for (const k of [
							"static_minutes",
							"snapshot_hours",
							"keep_snapshots",
							"max_snapshot_gb",
						])
							data[k] = Number(f.get(k));
						if (data.enabled && !data.confirm_private_upload)
							throw new Error(
								"Confirm private-data upload and repository access before enabling backups.",
							);
						backupStatus = await post("backups/settings", data);
						$("#backup-config [name=auth_mode]", root).value =
							backupStatus.config.auth_mode;
						dirty = false;
						$("#backup-config", root)?._formState?.reset();
						backupControls();
						if (data.enabled) {
							await post("backups/sync");
							setActivityOpen(root, true);
							notify(
								"Native backup Schedules enabled; the first snapshot request was submitted to Temporal.",
							);
						} else notify("Settings saved with GitHub backups disabled.");
					});
				};
				$("#backup-consent", root).onchange = backupControls;
				$$("[data-backup-setup]", root).forEach(
					(b) =>
						(b.onclick = async () => {
							const connect = b.dataset.backupSetup === "github_connect";
							if (
								await confirmAction(
									connect
										? "Connect your GitHub CLI login? No backup upload is enabled by connecting."
										: "Connect your GitHub account and prepare a PRIVATE backup repository? Existing private repositories are reused. Nothing is uploaded until you authorize backups.",
								)
							)
								action(async () => {
									const result = await post(
										"setup/task/" + b.dataset.backupSetup,
										connect
											? {
													consent: true,
													reconnect: b.dataset.reconnect === "true",
												}
											: { confirm: true },
									);
									setActivityOpen(root, true);
									return result;
								});
						}),
				);
				$$("[data-task]", root).forEach(
					(b) =>
						(b.onclick = () =>
							action(async () => {
								if (b.dataset.task === "sync") {
									const current = await api("backups");
									backupStatus = current;
									if (!current.readiness?.ready)
										throw new Error(
											(
												current.readiness?.problems || [
													"Authorize backups first",
												]
											).join("; "),
										);
								}
								const result = await post("backups/" + b.dataset.task);
								notify(
									result.status === "busy"
										? result.message
										: "Operation started. See Activity & results.",
								);
								await poll();
								return result;
							})),
				);
				$("#restore-backup", root).onclick = () =>
					action(async () => {
						if (
							!(await confirmAction(
								"RESTORE this workspace from GitHub? Stop all application workers first. Saved profile/searches and history will be replaced; existing files are kept in a local rollback folder. Old outreach authorization will be disabled.",
							))
						)
							return;
						await post("backups/restore", {
							confirm_restore: "RESTORE",
							tag: $("#restore-point", root).value || undefined,
						});
						notify(
							"Recovery started. Do not start workers until it completes.",
						);
					});
				$("#rollback-backup", root).onclick = () =>
					action(async () => {
						await post("backups/rollback");
						notify("Interrupted recovery rolled back.");
					});
			} else {
				root.innerHTML = `<div id="memory-error" class="notice notice-warning" role="alert" hidden></div>${card("What did you do today?", "Your original words are saved before any AI extraction. The terminal impact add command still works.", `<form id="diary-form" class="setup-stack"><label class="setup-field">Accomplishment, experiment, decision, or result<textarea id="diary-note" rows="7" maxlength="16000" required placeholder="Today I reduced the import job from 18 minutes to 6 by batching writes. Measured on the same 40k-row dataset. We have not measured production impact yet."></textarea></label><label class="setup-check"><input id="diary-extract" type="checkbox">Also use my configured model to extract grounded impact evidence (normal model quotas/costs apply)</label><button class="btn btn-primary" type="submit">Save this moment</button><p class="fine-print">No made-up metrics. Your raw note survives extraction errors. Enable Backups to keep an off-device copy.</p></form>`)}<form id="diary-search" class="toolbar"><label class="search-field"><input id="diary-query" placeholder="Search notes and structured impact…" maxlength="200" aria-label="Search diary"></label><button class="btn" type="submit">Filter</button><button class="btn" id="diary-refresh" type="button">Refresh</button><button class="btn" data-diary-history type="button">Edit history & restore</button></form><p id="diary-count"></p>${card("Your impact diary", "Existing accomplishments and new notes, in one history.", `<div id="diary-entries"></div>`)}<div class="setup-inline"><button class="btn" id="diary-prev">Previous</button><span id="diary-page"></span><button class="btn" id="diary-next">Next</button></div>${card("Extraction history", "You can keep living while these tasks complete.", `<div id="memory-tasks"></div>`)}`;
				$("#diary-form", root).onsubmit = (e) => {
					e.preventDefault();
					action(async () => {
						const text = $("#diary-note", root).value;
						if (!lastNote || lastNote.note !== text)
							lastNote = {
								note: text,
								id: Array.from(
									crypto.getRandomValues(new Uint8Array(12)),
									(v) => v.toString(16).padStart(2, "0"),
								).join(""),
							};
						const saved = await post("diary/note", lastNote);
						$("#diary-note", root).value = "";
						dirty = false;
						$("#diary-form", root)?._formState?.reset();
						lastNote = null;
						notify("Original note saved.");
						if ($("#diary-extract", root).checked)
							await post("diary/extract", {
								id: saved.id,
								confirm_model: true,
							});
						await loadDiary();
					});
				};
				$("#diary-search", root).onsubmit = (e) => {
					e.preventDefault();
					query = $("#diary-query", root).value;
					page = 1;
					action(loadDiary);
				};
				$("#diary-refresh", root).onclick = () => action(loadDiary);
				$("#diary-prev", root).onclick = () => {
					page = Math.max(1, page - 1);
					action(loadDiary);
				};
				$("#diary-next", root).onclick = () => {
					page++;
					action(loadDiary);
				};
				await loadDiary();
			}
			const recordDirty = () => {
				dirty = formTrackers.some((t) => t.dirty());
				backupControls();
			};
			for (const id of ["backup-credentials", "backup-config", "diary-form"]) {
				const form = $("#" + id, root);
				if (form)
					formTrackers.push(
						trackForm(form, {
							readOnly,
							onChange: recordDirty,
							...(id === "backup-config" ? { buttons: [] } : {}),
						}),
					);
			}
			recordDirty();
			if (readOnly)
				$$("button,input,textarea,select", root)
					.filter((el) => el.closest("form")?.id !== "diary-search")
					.forEach((el) => (el.disabled = true));
			mountActivity(root, "#memory-tasks");
			backupControls();
			poll();
		} catch (e) {
			if (!disposed)
				root.innerHTML = `<div class="notice notice-warning">${esc(e.message)}</div>`;
		}
	}
	boot();
}
