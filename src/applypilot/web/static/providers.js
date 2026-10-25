import { confirmAction } from "./ux.js";

const esc = (value) =>
	String(value ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const date = (value) =>
	value
		? new Date(
				typeof value === "number" ? value * 1000 : value,
			).toLocaleString()
		: "Not observed";
const label = (value) => String(value || "unobserved").replaceAll("_", " ");
const badge = (value) =>
	`<span class="badge ${value === "healthy" ? "green" : ["held", "needs_attention"].includes(value) ? "orange" : "blue"}">${esc(label(value))}</span>`;

export function mountProviders(root, { api, notify, readOnly }) {
	if (!document.querySelector("link[data-provider-style]")) {
		const link = document.createElement("link");
		link.rel = "stylesheet";
		link.href = "/static/providers.css";
		link.dataset.providerStyle = "true";
		document.head.append(link);
	}
	let data = null,
		selected = "",
		filter = "",
		onlyProblems = false,
		disposed = false,
		busy = false,
		timer = null,
		checkTimer = null,
		check = null,
		reload = null,
		reloadTimer = null;
	root.innerHTML =
		'<div class="notice" role="status">Reading provider health…</div>';
	function render() {
		if (!data || disposed) return;
		// A live refresh must not erase a credential the owner is entering.
		if (
			[...root.querySelectorAll("input[type=password]")].some(
				(input) => input.value,
			)
		)
			return;
		const open = new Set(
			[...root.querySelectorAll("details[open]")].map(
				(el) => el.dataset.credential,
			),
		);
		const matching = data.providers.filter(
			(p) =>
				(!filter ||
					`${p.id} ${p.credentials.map((c) => c.slot).join(" ")}`
						.toLowerCase()
						.includes(filter.toLowerCase())) &&
				(!onlyProblems ||
					p.credentials.some((c) =>
						["held", "needs_attention"].includes(c.status),
					)),
		);
		if (!matching.some((p) => p.id === selected))
			selected = matching[0]?.id || "";
		const p = matching.find((p) => p.id === selected);
		const active = data.telemetry_version >= 2;
		const selectionPending =
			data.revision !== "initial" && data.applied_revision !== data.revision;
		root.innerHTML = `<div class="provider-summary"><div><strong>${data.totals.providers}</strong><span>providers</span></div><div><strong>${data.totals.credentials}</strong><span>credentials</span></div><div><strong>${data.totals.unique_errors}</strong><span>unique errors</span></div><div><strong>${data.totals.failures}</strong><span>upstream failures</span></div></div>
      <p class="fine-print">Retained evidence from ${esc(date(data.window_start))}. Last observed request: ${esc(date(data.last_observed))}. Credentials are labeled by slot; secret values are never displayed here.</p>
      ${!active ? '<p class="notice notice-warning">Historical failures are available. Restart LiteLLM with this update to record completed requests and activate provider selection controls. Missing success history is shown as unknown.</p>' : ""}
      ${selectionPending ? '<p class="notice" role="status">Selection saved. Waiting for the gateway to acknowledge it on a completed request.</p>' : ""}
      ${data.history_truncated ? '<p class="notice">Showing the most recent 32 MB of retained provider evidence. Counts apply to this window.</p>' : ""}
      ${data.unreadable_records ? `<p class="notice notice-warning">${data.unreadable_records} records or files could not be read. Coverage is incomplete.</p>` : ""}
      <div class="provider-toolbar"><label>Find provider or credential<input id="provider-filter" type="search" value="${esc(filter)}" placeholder="e.g. Gemini or KEY_6"></label><label><input id="provider-problems" type="checkbox" ${onlyProblems ? "checked" : ""}> Needs attention only</label><button class="btn" id="provider-refresh">Refresh</button><a class="btn" href="#connections?group=gateway">Configure provider credentials</a></div>
      ${check ? `<p class="notice" role="status">${esc(check.provider)} / ${esc(check.credential)}: ${check.state === "running" ? "Checking the selected model…" : esc(check.message)}</p>` : ""}
      <div class="provider-layout"><nav class="provider-list" aria-label="AI providers">${matching.map((row) => `<button class="btn ${row.id === selected ? "btn-primary" : ""}" data-provider="${esc(row.id)}" aria-pressed="${row.id === selected}"><strong>${esc(row.id)}</strong><small>${row.credentials.length} credentials · ${row.model_count} models${row.preferred ? " · preferred" : ""}${!row.enabled ? " · paused" : ""}</small></button>`).join("")}</nav><div class="provider-detail">${
				p
					? `<section class="card"><div class="provider-heading"><div><h2>${esc(p.id)}</h2><p>${p.model_count} configured models · ${esc(p.tiers.join(", "))}</p></div><div class="provider-actions"><button class="btn" data-provider-enabled="${!p.enabled}" ${readOnly ? "disabled" : ""}>${p.enabled ? "Pause provider" : "Enable provider"}</button><button class="btn" data-provider-preferred="${!p.preferred}" ${readOnly ? "disabled" : ""}>${p.preferred ? "Remove preference" : "Prefer this provider"}</button></div></div><p class="fine-print">Preferences rank compatible candidates first. Model capabilities, quotas, and provider access restrictions still apply. Changes affect future dispatches.</p></section>
      ${p.credentials
				.map(
					(c) =>
						`<details class="card provider-credential" data-credential="${esc(c.slot)}" ${open.has(c.slot) ? "open" : ""}><summary><strong>${esc(c.slot)}</strong>${badge(c.status)}<span>${c.unique_errors} unique errors · ${c.failures} failures · ${c.successes} completions</span></summary><div class="provider-credential-body"><p>Last completed: ${esc(date(c.last_success))}<br>Last failure: ${esc(date(c.last_failure))}${c.hold_until > Date.now() / 1000 ? `<br>Hold until ${esc(date(c.hold_until))}: ${esc(label(c.hold_reason))}` : ""}</p><div class="provider-actions"><button class="btn" data-slot="${esc(c.slot)}" data-slot-enabled="${!c.selected}" ${readOnly ? "disabled" : ""}>${c.selected ? "Pause credential" : "Enable credential"}</button><label>Model<select data-probe-model="${esc(c.slot)}">${c.models
							.filter((m) => m.lane)
							.map(
								(m) =>
									`<option value="${esc(m.id)}">${esc(m.id)} · ${esc(label(m.status))}</option>`,
							)
							.join(
								"",
							)}</select></label><button class="btn" data-check="${esc(c.slot)}" ${readOnly || !c.enabled || check?.state === "running" ? "disabled" : ""}>Check this model</button></div><p class="fine-print">A check makes one small request using this credential and the selected model. It consumes provider quota and may incur usage charges. It does not switch credentials or clear holds.</p>${c.errors.length ? `<div class="provider-errors">${c.errors.map((e) => `<article class="provider-error"><div>${badge(e.classification)}<strong>${e.count} occurrences</strong>${e.http_status ? `<span>HTTP ${esc(e.http_status)}</span>` : ""}</div><p>${esc(e.message)}</p><p>${esc(e.remedy)}</p><small>First ${esc(date(e.first_at))} · Last ${esc(date(e.last_at))}<br>Models: ${esc(e.models.join(", "))}<br>Evidence: <code>${esc(e.evidence_id)}</code></small></article>`).join("")}</div>` : "<p>No upstream failures recorded for this credential in the retained window.</p>"}</div></details>`,
				)
				.join("")}`
					: '<p class="notice">No providers match these filters.</p>'
			}</div></div>
      <details class="card provider-catalog"><summary>Choose another provider</summary><p>The local catalog includes ${data.catalog.map(esc).join(", ")}. Select a configured provider above, enable its credentials, and mark it preferred. Configure credentials through Connections before using a new account.</p><p class="fine-print">Providers without generated routes require a catalog refresh and gateway rebuild; enabling a provider here does not invent models or pricing permissions.</p><a class="btn" href="#connections?group=gateway">Open credential settings</a></details>`;
		root.querySelector("#provider-filter").onchange = (e) => {
			filter = e.target.value;
			render();
		};
		root.querySelector("#provider-problems").onchange = (e) => {
			onlyProblems = e.target.checked;
			render();
		};
		root.querySelector("#provider-refresh").onclick = refresh;
		const reloadButton = document.createElement("button");
		reloadButton.className = "btn";
		reloadButton.type = "button";
		reloadButton.textContent =
			reload?.state === "running" ? "Reloading gateway…" : "Reload gateway";
		reloadButton.disabled = readOnly || reload?.state === "running";
		root.querySelector(".provider-toolbar").append(reloadButton);
		if (reload) {
			const message = document.createElement("p");
			message.className = "notice";
			message.setAttribute("role", "status");
			message.textContent = reload.message;
			root.querySelector(".provider-toolbar").after(message);
		}
		reloadButton.onclick = async () => {
			if (
				!(await confirmAction(
					"Reload LiteLLM to activate saved credentials and gateway code? Autopilot must be stopped. Any other requests currently using this gateway may be interrupted.",
				))
			)
				return;
			reloadButton.disabled = true;
			try {
				reload = await api("providers/reload", {
					method: "POST",
					body: { confirm: true },
				});
				render();
				pollReload();
			} catch (e) {
				notify(e.message, true);
				render();
			}
		};
		root.querySelectorAll("[data-provider]").forEach(
			(el) =>
				(el.onclick = () => {
					selected = el.dataset.provider;
					render();
				}),
		);
		root
			.querySelectorAll("[data-provider-enabled]")
			.forEach(
				(el) =>
					(el.onclick = () =>
						save({ enabled: el.dataset.providerEnabled === "true" })),
			);
		root
			.querySelectorAll("[data-provider-preferred]")
			.forEach(
				(el) =>
					(el.onclick = () =>
						save({ preferred: el.dataset.providerPreferred === "true" })),
			);
		root.querySelectorAll("[data-slot-enabled]").forEach(
			(el) =>
				(el.onclick = () =>
					save({
						credential: el.dataset.slot,
						enabled: el.dataset.slotEnabled === "true",
					})),
		);
		root.querySelectorAll("[data-check]").forEach(
			(el) =>
				(el.onclick = async () => {
					const slot = el.dataset.check;
					const select = [...root.querySelectorAll("[data-probe-model]")].find(
						(s) => s.dataset.probeModel === slot,
					);
					el.disabled = true;
					try {
						check = await api("providers/check", {
							method: "POST",
							body: {
								provider: selected,
								credential: slot,
								model: select.value,
							},
						});
						render();
						pollCheck();
					} catch (e) {
						notify(e.message, true);
						render();
					}
				}),
		);
		root.querySelectorAll(".provider-credential").forEach((section) => {
			const slot = section.dataset.credential;
			const credential = p.credentials.find((c) => c.slot === slot);
			const state = document.createElement("p");
			state.className = "fine-print";
			state.textContent = credential.restart_required
				? "Saved credential differs from the gateway’s observed credential. Recreate LiteLLM before checking the replacement."
				: `Credential state: ${label(credential.credential_state)}.`;
			section.querySelector(".provider-credential-body").prepend(state);
			if (credential.route_holds?.length) {
				const holds = document.createElement("div");
				holds.className = "notice";
				holds.innerHTML =
					"<strong>Model or request restrictions</strong>" +
					credential.route_holds
						.map(
							(h) =>
								`<p>${esc(h.model)} · ${esc(label(h.scope))}: ${esc(label(h.reason || h.classification))}<br>Until ${esc(date(h.until))}</p>`,
						)
						.join("");
				section.querySelector(".provider-credential-body").prepend(holds);
			}
			const editor = document.createElement("details");
			editor.className = "provider-credential-editor";
			editor.innerHTML = `<summary>${credential.configured ? "Replace saved credential" : "Configure this credential"}</summary><form class="provider-actions" data-revision="${esc(data.credential_revision)}"><label>New value for ${esc(slot)}<input type="password" autocomplete="new-password" minlength="8" maxlength="4096" required ${readOnly ? "disabled" : ""}></label><button class="btn" type="submit" ${readOnly ? "disabled" : ""}>Save credential</button></form><p class="fine-print">Saves only this gateway credential. Recreate LiteLLM to load the new value. The saved value is never returned to the page.</p>`;
			section.querySelector(".provider-credential-body").append(editor);
			editor.querySelector("form").onsubmit = async (event) => {
				event.preventDefault();
				const form = event.currentTarget,
					input = form.querySelector("input"),
					button = form.querySelector("button");
				button.disabled = true;
				try {
					const result = await api("providers/credential", {
						method: "POST",
						body: {
							provider: p.id,
							credential: slot,
							revision: form.dataset.revision,
							value: input.value,
						},
					});
					input.value = "";
					notify(result.message);
					await refresh();
				} catch (e) {
					notify(e.message, true);
				} finally {
					button.disabled = readOnly;
				}
			};
		});
	}
	async function save(values) {
		if (busy) return;
		busy = true;
		try {
			await api("providers/selection", {
				method: "POST",
				body: { revision: data.revision, provider: selected, ...values },
			});
			notify("Provider selection saved.");
		} catch (e) {
			notify(e.message, true);
		} finally {
			busy = false;
			await refresh();
		}
	}
	async function pollCheck() {
		clearTimeout(checkTimer);
		if (disposed || !check || check.state !== "running") return;
		try {
			check = await api("providers/check/" + check.id);
			render();
			if (check.state !== "running") {
				await refresh();
				return;
			}
		} catch (e) {
			if (!disposed) notify(e.message, true);
		}
		if (!disposed) checkTimer = setTimeout(pollCheck, 2000);
	}
	async function pollReload() {
		clearTimeout(reloadTimer);
		if (disposed || reload?.state !== "running") return;
		try {
			reload = await api("providers/reload/" + reload.id);
			render();
			if (reload.state !== "running") {
				await refresh();
				return;
			}
		} catch (e) {
			if (!disposed) notify(e.message, true);
		}
		if (!disposed) reloadTimer = setTimeout(pollReload, 2000);
	}
	async function refresh() {
		clearTimeout(timer);
		if (disposed || busy) return;
		busy = true;
		try {
			data = await api("providers");
			render();
		} catch (e) {
			if (!disposed) {
				if (!data)
					root.innerHTML = `<p class="notice notice-warning">${esc(e.message)}</p>`;
				else notify(e.message, true);
			}
		} finally {
			busy = false;
			if (!disposed)
				timer = setTimeout(() => {
					if (
						!document.hidden &&
						document.documentElement.dataset.updatesPaused !== "true"
					)
						refresh();
					else timer = setTimeout(refresh, 15000);
				}, 15000);
		}
	}
	refresh();
	return {
		refresh,
		dispose() {
			disposed = true;
			clearTimeout(timer);
			clearTimeout(checkTimer);
			clearTimeout(reloadTimer);
		},
	};
}
