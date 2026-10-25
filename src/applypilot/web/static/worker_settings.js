import { trackForm, updatesPaused } from "./ux.js";

const fields = {
	APPLYPILOT_RESOURCE_WORKERS: "Shared workers",
	APPLYPILOT_BROWSER_WORKERS: "Browser slots",
	APPLYPILOT_PREPARATION_MAX_PENDING: "Queued and active preparation tasks",
};

export function mountWorkerSettings(root, { api, notify, readOnly, onChange }) {
	let disposed = false,
		tracker = null,
		revision = null,
		taskId = null,
		timer = null,
		loadedValues = {};
	root.innerHTML = `<section class="card"><div class="card-head"><h2>Worker limits</h2></div><div class="card-content"><form class="setup-stack"><div class="setup-fields">${Object.entries(
		fields,
	)
		.map(
			([key, label]) =>
				`<label class="setup-field">${label}<input name="${key}" type="number" min="1" step="1" required disabled></label>`,
		)
		.join(
			"",
		)}</div><p>Browser slots cannot exceed shared workers. Saving drains active work and restarts managed workers with the new limits.</p><div class="form-actions"><button class="btn btn-primary" type="submit" disabled>Save worker limits</button><button class="btn" type="button" data-reload>Reload settings</button><span role="status" data-status>Loading settings…</span></div></form></div></section>`;
	const form = root.querySelector("form"),
		status = root.querySelector("[data-status]");
	function busy(value) {
		for (const input of form.querySelectorAll("input"))
			input.disabled = readOnly || value;
		root.querySelector("[data-reload]").disabled = value;
		tracker?.busy(value);
	}
	async function load() {
		busy(true);
		try {
			const [env, runtime] = await Promise.all([
				api("setup/connections"),
				api("runtime").catch(() => null),
			]);
			if (disposed) return;
			const workers =
				Number(
					env.fields.find((f) => f.key === "APPLYPILOT_RESOURCE_WORKERS")
						?.value,
				) ||
				runtime?.execution?.workers ||
				4;
			const defaults = {
				APPLYPILOT_RESOURCE_WORKERS: workers,
				APPLYPILOT_BROWSER_WORKERS:
					runtime?.execution?.browser_workers || Math.min(2, workers),
				APPLYPILOT_PREPARATION_MAX_PENDING: Math.max(4, workers * 2),
			};
			for (const key of Object.keys(fields))
				form.elements[key].value =
					env.fields.find((f) => f.key === key)?.value || defaults[key];
			loadedValues = Object.fromEntries(
				Object.keys(fields).map((key) => [key, form.elements[key].value]),
			);
			revision = env.revision;
			tracker?.destroy();
			tracker = trackForm(form, {
				readOnly,
				valid: () =>
					form.checkValidity() &&
					Number(form.elements.APPLYPILOT_BROWSER_WORKERS.value) <=
						Number(form.elements.APPLYPILOT_RESOURCE_WORKERS.value),
				onChange,
			});
			busy(false);
			onChange(false);
			status.textContent = "";
		} catch (error) {
			if (!disposed) {
				status.textContent = error.message;
				busy(!revision);
				root.querySelector("[data-reload]").disabled = false;
			}
		}
	}
	async function poll() {
		if (disposed || !taskId) return;
		try {
			if (document.hidden || updatesPaused()) return;
			const value = await api("setup/tasks");
			if (disposed) return;
			const task = value.tasks?.find((row) => row.id === taskId);
			if (!task) return;
			if (task.state === "completed") {
				taskId = null;
				await load();
				if (!disposed) {
					status.textContent = "Saved";
					notify("Worker limits saved.");
				}
			} else if (
				["failed", "timed_out", "cancelled", "interrupted"].includes(task.state)
			) {
				taskId = null;
				busy(false);
				status.textContent =
					task.message || "Worker limits could not be saved.";
				notify(status.textContent, true);
			}
		} catch (error) {
			if (!disposed) status.textContent = error.message;
		} finally {
			if (!disposed && taskId) timer = setTimeout(poll, 2500);
		}
	}
	form.onsubmit = async (event) => {
		event.preventDefault();
		if (
			readOnly ||
			taskId ||
			!tracker?.dirty() ||
			!form.reportValidity() ||
			Number(form.elements.APPLYPILOT_BROWSER_WORKERS.value) >
				Number(form.elements.APPLYPILOT_RESOURCE_WORKERS.value)
		)
			return;
		busy(true);
		status.textContent = "Saving…";
		try {
			const values = Object.fromEntries(
				Object.keys(fields)
					.filter((key) => form.elements[key].value !== loadedValues[key])
					.map((key) => [key, form.elements[key].value]),
			);
			const result = await api("setup/connections", {
				method: "POST",
				body: { revision, values, clear: [] },
			});
			if (disposed) return;
			if (result.task_id) {
				taskId = result.task_id;
				status.textContent = "Waiting for active work to finish…";
				poll();
			} else {
				await load();
				if (!disposed) {
					status.textContent = "Saved";
					notify("Worker limits saved.");
				}
			}
		} catch (error) {
			if (!disposed) {
				busy(false);
				status.textContent = error.message;
				notify(error.message, true);
			}
		}
	};
	root.querySelector("[data-reload]").onclick = load;
	load();
	return {
		dispose() {
			disposed = true;
			clearTimeout(timer);
			tracker?.destroy();
		},
	};
}
