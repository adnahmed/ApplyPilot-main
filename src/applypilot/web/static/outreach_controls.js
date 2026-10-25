import { trackForm } from "./ux.js";

const $ = (selector, root) => root.querySelector(selector);
const channelName = (channel) =>
	({ email: "Email", linkedin: "LinkedIn", x: "X" })[channel] || channel;

export function mountOutreachControls(
	root,
	{ api, notify, readOnly = false, onChange = () => {} },
) {
	let current = null,
		tracker = null,
		disposed = false,
		busy = false;
	root.innerHTML = `<section class="card outreach-controls"><div class="card-head"><div><h2>Outreach</h2><p>Uses your signed-in accounts automatically.</p></div><span class="badge" data-mode>Checking</span></div><div class="card-content">
 <p data-status role="status">Loading outreach settings…</p><p class="fine-print" data-channels></p><p class="notice notice-warning" data-error role="alert" hidden></p>
 <form data-budget class="setup-stack"><div class="setup-fields"><label class="setup-field">Daily messages<input name="daily" type="number" min="-1" step="1" required aria-describedby="outreach-budget-help" disabled></label><label class="setup-field">Daily research jobs<input name="research" type="number" min="-1" step="1" required aria-describedby="outreach-budget-help" disabled></label></div>
 <p class="fine-print" id="outreach-budget-help">−1 = no limit · 0 = paused. Message attempts, replies and follow-ups share the daily budget, which resets at midnight UTC.</p>
 <details class="setup-details"><summary>Contacts and sending delay</summary><div class="setup-fields"><label class="setup-field">Contacts per job<input name="contacts" type="number" min="-1" step="1" required disabled></label><label class="setup-field">Cancellation window (seconds)<input name="grace" type="number" min="0" max="180" step="1" required disabled></label></div><p class="fine-print">Initial requests wait this long before sending. Cancel a request from Outreach while its window is open.</p></details>
 <div class="form-actions"><button class="btn btn-primary" type="submit" disabled>Save changes</button><button class="btn" type="button" data-pause disabled>Pause outreach</button><button class="btn btn-quiet" type="button" data-uncapped disabled>Remove limits</button><span class="fine-print" data-budget-status role="status">No unsaved changes</span></div></form>
 <div class="setup-inline"><a class="btn btn-sm" href="#connections?group=gmail">Email connection</a><a class="btn btn-sm" href="#connections?group=browser">Social accounts</a></div>
 <details class="setup-details"><summary>Environment switch</summary><p class="fine-print">Outreach is on by default. Set <code>APPLYPILOT_OUTREACH_ENABLED=false</code> and restart ApplyPilot to turn it off.</p></details>
 </div></section>`;
	const form = $("[data-budget]", root),
		error = $("[data-error]", root),
		budgetStatus = $("[data-budget-status]", root),
		pauseButton = $("[data-pause]", root);
	const active = () => !disposed && root.contains(form);
	const dirty = () => !!tracker?.dirty();
	const showError = (e) => {
		if (active()) {
			error.textContent = e.message;
			error.hidden = false;
		}
	};
	function sync() {
		if (!active()) return;
		const allowed = !!current?.can_edit && !readOnly && !current?.read_only;
		for (const input of form.querySelectorAll("input"))
			input.disabled = !allowed || busy;
		pauseButton.disabled = !allowed || busy || current?.env_enabled === false;
		pauseButton.textContent =
			current?.daily_send_limit === 0
				? "Resume without a daily limit"
				: "Pause outreach";
		$("[data-uncapped]", form).disabled = !allowed || busy;
		tracker?.busy(busy);
		if (!allowed) {
			const submit = $('[type="submit"]', form);
			submit.disabled = true;
			submit.title = current?.problem || "Outreach settings are unavailable.";
			const guidance = $(".form-guidance", form);
			if (guidance && !readOnly && !current?.read_only)
				guidance.textContent = current
					? "Resolve the setting issue above to make changes."
					: "Loading settings…";
		}
		onChange(dirty());
	}
	function update(value) {
		if (!active() || !value) return;
		current = value;
		const off = value.env_enabled === false || value.enabled === false;
		const status = off
			? "Off"
			: value.problem
				? "Needs attention"
				: value.daily_send_limit === 0
					? "Paused"
					: "On";
		const badge = $("[data-mode]", root);
		badge.textContent = status;
		badge.className = "badge " + (status === "On" ? "green" : "orange");
		$("[data-status]", root).textContent = off
			? value.problem || "Outreach is turned off in the environment."
			: value.problem ||
				(status === "Paused"
					? "Sending is paused. Resume here when ready."
					: "Outreach is on. Existing account sessions are reused; no separate authorization is needed.");
		$("[data-channels]", root).textContent =
			`Channels: ${(value.channels || []).map(channelName).join(", ") || "None configured"}. Connection details are available below.`;
		if (!dirty() && !busy) {
			form.elements.daily.value = value.daily_send_limit;
			form.elements.research.value = value.daily_research_limit ?? -1;
			form.elements.contacts.value = value.contacts_per_job ?? -1;
			form.elements.grace.value = value.send_grace_seconds;
			form.dataset.revision = value.revision;
			tracker?.destroy();
			tracker = trackForm(form, {
				readOnly,
				onChange: (isDirty) => {
					budgetStatus.textContent = isDirty
						? "Unsaved changes"
						: "No unsaved changes";
					onChange(isDirty);
				},
			});
		}
		sync();
	}
	async function save(toggle = false) {
		if (
			!active() ||
			busy ||
			!current?.can_edit ||
			readOnly ||
			current?.read_only ||
			(!toggle && (!dirty() || !form.checkValidity()))
		)
			return;
		const paused = current.daily_send_limit === 0;
		const body = {
			revision: form.dataset.revision,
			daily_send_limit: toggle
				? paused
					? -1
					: 0
				: Number(form.elements.daily.value),
		};
		if (!toggle) {
			body.send_grace_seconds = Number(form.elements.grace.value);
			body.daily_research_limit = Number(form.elements.research.value);
			body.contacts_per_job = Number(form.elements.contacts.value);
		}
		busy = true;
		error.hidden = true;
		sync();
		try {
			await api("settings", { method: "POST", body });
			const fresh = await api("settings");
			if (!active()) return;
			tracker?.reset();
			busy = false;
			update(fresh);
			notify(
				toggle
					? paused
						? "Outreach resumed without a daily limit."
						: "Outreach paused."
					: "Outreach settings saved.",
			);
		} catch (e) {
			showError(e);
		} finally {
			busy = false;
			sync();
		}
	}
	$("[data-uncapped]", form).onclick = () => {
		form.elements.daily.value = -1;
		form.elements.research.value = -1;
		form.elements.contacts.value = -1;
		form.elements.daily.dispatchEvent(new Event("input", { bubbles: true }));
		budgetStatus.textContent = "Limits removed. Save changes to apply.";
	};
	form.onsubmit = (e) => {
		e.preventDefault();
		save();
	};
	pauseButton.onclick = () => save(true);
	return {
		update,
		dirty,
		dispose: () => {
			disposed = true;
			tracker?.destroy();
			form.onsubmit = null;
			pauseButton.onclick = null;
			$("[data-uncapped]", form).onclick = null;
		},
	};
}
