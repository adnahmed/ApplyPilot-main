import { esc } from "./setup_ui.js";
import { confirmAction, trackForm } from "./ux.js";
/* Owner-only edits. Delegation survives polling; modal state is outside refreshed cards. */
export function attachDiaryEditor(
	root,
	{ api, notify, onChanged, readOnly = false },
) {
	let dialog = null,
		disposed = false,
		pollTimer = null,
		busy = false,
		editorTracker = null;
	const post = (path, body) =>
		api(path, { method: "POST", body, signal: AbortSignal.timeout(20000) });
	function close() {
		editorTracker?.destroy();
		editorTracker = null;
		clearTimeout(pollTimer);
		dialog?.close();
		dialog?.remove();
		dialog = null;
		busy = false;
	}
	function shell(title) {
		close();
		dialog = document.createElement("dialog");
		dialog.className = "diary-dialog";
		dialog.innerHTML = `<div class="diary-dialog-head"><h2>${esc(title)}</h2><button type="button" class="btn" data-close>Close</button></div><p class="notice notice-warning" data-error hidden role="alert"></p><div data-content></div>`;
		document.body.append(dialog);
		dialog.querySelector("[data-close]").onclick = close;
		dialog.showModal();
		return dialog;
	}
	function error(e) {
		editorTracker?.busy(false);
		if (!dialog) return;
		const box = dialog.querySelector("[data-error]");
		box.hidden = false;
		box.textContent = e.message;
	}
	async function archive(kind, id) {
		const d = await api("diary/item?" + new URLSearchParams({ kind, id }));
		if (
			!(await confirmAction(
				kind === "note"
					? "Archive this note? Extracted records stay separately editable; previous versions remain in backups."
					: "Archive this impact record? It leaves future selection, but sent documents and historic job snapshots are not rewritten.",
			))
		)
			return;
		await post("diary/edit", { ...d, operation: "archive", confirm: true });
		notify("Archived. Restore is available in diary edit history.");
		await onChanged();
	}
	async function open(kind, id) {
		const loaded = await api("diary/item?" + new URLSearchParams({ kind, id }));
		if (disposed) return;
		const d = shell(
			kind === "note" ? "Edit your note" : "Edit this accomplishment",
		);
		let item = structuredClone(loaded.item),
			jsonDirty = false,
			dirty = false;
		const q = (s) => d.querySelector(s);
		q("[data-content]").innerHTML =
			`<p>Original versions are retained. ${kind === "record" ? "Changed claims return to draft unless you explicitly review this correction below." : "Changing this note flags its derived records for review."}</p>
   <div data-edit-fields><div class="setup-stack" data-basic>${kind === "note" ? '<label class="setup-field">Your note<textarea data-note rows="9" maxlength="16000"></textarea></label>' : '<label class="setup-field">Situation / problem<textarea data-problem rows="3"></textarea></label><label class="setup-field">What you did<textarea data-action rows="5"></textarea></label><label class="setup-field">Context / project<input data-project></label>'}</div>
   ${kind === "record" ? '<details class="setup-details"><summary>All record fields · JSON editor</summary><p class="fine-print">Measurements, outcomes, sources and attribution stay intact unless changed. Editing this JSON takes precedence over the short fields.</p><textarea data-json class="setup-code" rows="16" spellcheck="false"></textarea></details>' : ""}
   ${kind === "record" ? '<label class="setup-check"><input type="checkbox" data-reviewed> I have reviewed this correction; make this record eligible for future tailoring.</label>' : ""}
   </div><div class="setup-inline"><button type="button" class="btn btn-primary" data-save disabled>Save correction</button><button type="button" class="btn" data-archive>Archive this ${kind === "note" ? "note" : "record"}</button></div>
   <details class="setup-details"><summary>Describe a correction in natural language</summary><label class="setup-field">What should change?<textarea data-instruction rows="3" maxlength="4000" placeholder="Correct the result to 12%, and clarify that this was a pilot, not a full rollout."></textarea></label><p class="fine-print">Uses your configured model. Review its proposed changes here before saving; no live record is updated automatically.</p><button type="button" class="btn" data-revise>Draft my correction</button><p data-progress role="status"></p><pre data-explanation class="setup-code"></pre></details>`;
		function fill() {
			if (kind === "note") q("[data-note]").value = item.note || "";
			else {
				q("[data-problem]").value = item.problem || "";
				q("[data-action]").value = item.intervention?.summary || "";
				q("[data-project]").value = item.scope?.project || "";
				q("[data-json]").value = JSON.stringify(item, null, 2);
			}
			jsonDirty = false;
		}
		fill();
		editorTracker = trackForm(q("[data-edit-fields]"), {
			readOnly,
			buttons: [q("[data-save]")],
			valid: () => true,
			onChange: (value) => (dirty = value),
		});
		const reviseButton = q("[data-revise]");
		const syncRevise = () =>
			(reviseButton.disabled =
				readOnly || busy || !q("[data-instruction]").value.trim());
		q("[data-instruction]").addEventListener("input", syncRevise);
		syncRevise();
		const guardedClose = async () => {
			if (
				(dirty || busy || q("[data-instruction]").value.trim()) &&
				!(await confirmAction(
					"Close this editor without saving its changes? A running draft task is not a saved correction.",
				))
			)
				return;
			close();
		};
		q("[data-close]").onclick = guardedClose;
		d.addEventListener("cancel", (e) => {
			e.preventDefault();
			guardedClose();
		});
		if (q("[data-json]"))
			q("[data-json]").oninput = () => {
				jsonDirty = true;
				q("[data-basic]")
					.querySelectorAll("input,textarea")
					.forEach((e) => (e.disabled = true));
			};
		q("[data-save]").onclick = async () => {
			if (busy || !editorTracker?.dirty()) return;
			try {
				if (kind === "note") item.note = q("[data-note]").value;
				else if (jsonDirty) item = JSON.parse(q("[data-json]").value);
				else {
					item.problem = q("[data-problem]").value;
					item.intervention = {
						...item.intervention,
						summary: q("[data-action]").value,
					};
					item.scope = { ...item.scope, project: q("[data-project]").value };
				}
				busy = true;
				editorTracker.busy(true);
				syncRevise();
				await post("diary/edit", {
					kind,
					id,
					revision: loaded.revision,
					item,
					operation: "edit",
					confirm: true,
					owner_reviewed: kind === "record" && q("[data-reviewed]").checked,
				});
				dirty = false;
				notify("Correction saved; original version retained.");
				close();
				await onChanged();
			} catch (e) {
				busy = false;
				syncRevise();
				error(e);
			}
		};
		q("[data-archive]").onclick = async () => {
			try {
				await archive(kind, id);
				close();
			} catch (e) {
				error(e);
			}
		};
		q("[data-revise]").onclick = async () => {
			if (busy || !q("[data-instruction]").value.trim()) return;
			try {
				if (
					dirty &&
					!(await confirmAction(
						"Draft from the last saved version? Unsaved manual changes will not be sent to the model.",
					))
				)
					return;
				busy = true;
				editorTracker.busy(true);
				syncRevise();
				const task = await post("diary/revise", {
					kind,
					id,
					revision: loaded.revision,
					instruction: q("[data-instruction]").value,
					confirm_model: true,
				});
				const taskId = task.task_id || task.id;
				q("[data-progress]").textContent =
					"Drafting; the saved diary is unchanged.";
				async function check() {
					if (dialog !== d || disposed) return;
					try {
						const response = await api("setup/tasks");
						const row = response.tasks.find((r) => r.id === taskId);
						if (
							row &&
							[
								"completed",
								"failed",
								"timed_out",
								"interrupted",
								"cancelled",
							].includes(row.state)
						) {
							busy = false;
							editorTracker?.busy(false);
							syncRevise();
							if (row.state !== "completed" || !row.result?.proposal) {
								throw new Error(
									row.result?.error ||
										row.message ||
										"Revision was not produced",
								);
							}
							if (row.result.proposal.revision !== loaded.revision)
								throw new Error(
									"The source revision changed; reopen the record.",
								);
							item = row.result.proposal.item;
							fill();
							if (q("[data-reviewed]")) q("[data-reviewed]").checked = false;
							q("[data-basic]")
								.querySelectorAll("input,textarea")
								.forEach((e) => (e.disabled = false));
							editorTracker?.refresh();
							q("[data-progress]").textContent =
								"Proposed correction loaded above. Review it, then Save correction.";
							q("[data-explanation]").textContent =
								row.result.explanation || "";
							return;
						}
						pollTimer = setTimeout(check, 1800);
					} catch (e) {
						busy = false;
						syncRevise();
						error(e);
					}
				}
				check();
			} catch (e) {
				busy = false;
				syncRevise();
				error(e);
			}
		};
	}
	async function history() {
		const data = await api("diary/history");
		if (disposed) return;
		const d = shell("Diary edit history");
		d.querySelector("[data-content]").innerHTML =
			"<p>Restore reverses a specific change only when no newer edit would be overwritten. Archives remain in backups; this is not permanent erasure.</p>" +
			data.history
				.map(
					(r) =>
						`<article class="impact-entry"><strong>${esc(r.kind)} · ${esc(r.item_id)}</strong><p>${esc(r.operation)} · ${esc(r.created_at)}</p><button type="button" class="btn" data-undo="${esc(r.id)}">Restore preceding version</button></article>`,
				)
				.join("");
		for (const b of d.querySelectorAll("[data-undo]"))
			b.onclick = async () => {
				if (
					!(await confirmAction("Restore the preceding version of this item?"))
				)
					return;
				try {
					await post("diary/undo", {
						history_id: b.dataset.undo,
						confirm: true,
					});
					notify("Saved version restored.");
					close();
					await onChanged();
				} catch (e) {
					error(e);
				}
			};
	}
	const listener = async (e) => {
		const b = e.target.closest(
			"[data-diary-edit],[data-diary-archive],[data-diary-history]",
		);
		if (!b || !root.contains(b) || readOnly) return;
		e.preventDefault();
		try {
			if (b.hasAttribute("data-diary-history")) await history();
			else if (b.hasAttribute("data-diary-archive"))
				await archive(b.dataset.diaryKind, b.dataset.diaryId);
			else await open(b.dataset.diaryKind, b.dataset.diaryId);
		} catch (err) {
			notify(err.message, true);
		}
	};
	root.addEventListener("click", listener);
	return () => {
		disposed = true;
		root.removeEventListener("click", listener);
		close();
	};
}
