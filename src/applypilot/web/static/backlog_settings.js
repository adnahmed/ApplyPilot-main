const buffers = {
	score: "Enriched jobs awaiting scoring",
	qualified: "Qualified jobs awaiting tailoring",
	cover: "Resumes awaiting cover letters",
	documents: "Approved documents awaiting rendering",
	apply: "Runnable application packages",
};
export function backlogFields(settings) {
	if (!settings?.buffers) return "";
	return `<fieldset><legend>Preparation capacity</legend><label class="setup-field">Capacity policy<select name="buffer-mode"><option value="automatic">Automatic — use available execution capacity</option><option value="watermarks">Manual queue watermarks</option></select></label><p class="fine-print">Automatic mode uses execution capacity without fixed queue watermarks. Manual mode pauses each producer at its chosen upper watermark.</p><div class="setup-fields" data-manual-watermarks>${Object.entries(
		buffers,
	)
		.map(
			([key, label]) =>
				`<fieldset><legend>${label}</legend><label class="setup-field">Resume at<input name="buffer-${key}-low" type="number" min="0" max="99999" step="1" required></label><label class="setup-field">Pause at<input name="buffer-${key}-high" type="number" min="1" max="100000" step="1" required></label></fieldset>`,
		)
		.join("")}</div></fieldset>`;
}
export function fillBacklog(form, settings) {
	const mode = form.elements["buffer-mode"];
	if (mode) {
		mode.value = settings?.mode || "automatic";
		const update = () => {
			const manual = mode.value === "watermarks";
			const fields = form.querySelector("[data-manual-watermarks]");
			if (fields) {
				fields.hidden = !manual;
				for (const input of fields.querySelectorAll("input")) input.disabled = !manual;
			}
		};
		mode.onchange = update;
		update();
	}
	for (const key of Object.keys(buffers))
		for (const bound of ["low", "high"]) {
			const input = form.elements[`buffer-${key}-${bound}`];
			if (input) input.value = settings?.buffers?.[key]?.[bound] ?? "";
		}
}
export function readBacklog(form) {
	if (!form.elements["buffer-score-low"]) return null;
	return {
		mode: form.elements["buffer-mode"]?.value || "automatic",
		buffers: Object.fromEntries(
			Object.keys(buffers).map((key) => [
				key,
				Object.fromEntries(
					["low", "high"].map((bound) => [
						bound,
						Number(form.elements[`buffer-${key}-${bound}`].value),
					]),
				),
			]),
		),
	};
}
export function validBacklog(form) {
	const settings = readBacklog(form);
	return (
		!settings || settings.mode === "automatic" ||
		Object.values(settings.buffers).every(
			({ low, high }) =>
				Number.isInteger(low) &&
				Number.isInteger(high) &&
				low >= 0 &&
				low < high &&
				high <= 100000,
		)
	);
}
