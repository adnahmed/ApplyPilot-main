export function updatesPaused() {
	return document.documentElement.dataset.updatesPaused === "true";
}
/* Small shared interaction primitives. No blocking alert/confirm/prompt calls. */
let confirmations = Promise.resolve();
export function confirmAction(
	message,
	{
		title = "Confirm action",
		accept = "Continue",
		cancel = "Cancel",
		danger = false,
	} = {},
) {
	const show = () =>
		new Promise((resolve) => {
			const previous = document.activeElement,
				dialog = document.createElement("dialog");
			dialog.className = "action-dialog";
			dialog.setAttribute("aria-labelledby", "action-dialog-title");
			const heading = document.createElement("h2");
			heading.id = "action-dialog-title";
			heading.textContent = title;
			const description = document.createElement("p");
			description.textContent = message;
			description.id = "action-dialog-description";
			dialog.setAttribute("aria-describedby", description.id);
			const actions = document.createElement("div");
			actions.className = "form-actions";
			const no = document.createElement("button");
			no.type = "button";
			no.className = "btn";
			no.textContent = cancel;
			const yes = document.createElement("button");
			yes.type = "button";
			yes.className = danger ? "btn btn-danger" : "btn btn-primary";
			yes.textContent = accept;
			actions.append(no, yes);
			dialog.append(heading, description, actions);
			document.body.append(dialog);
			no.onclick = () => dialog.close("cancel");
			yes.onclick = () => dialog.close("accept");
			dialog.addEventListener("cancel", () => {
				dialog.returnValue = "cancel";
			});
			dialog.addEventListener(
				"close",
				() => {
					const accepted = dialog.returnValue === "accept";
					dialog.remove();
					if (previous?.isConnected) previous.focus({ preventScroll: true });
					resolve(accepted);
				},
				{ once: true },
			);
			dialog.showModal();
			no.focus();
		});
	const result = confirmations.then(show, show);
	confirmations = result.then(() => {});
	return result;
}
function values(form) {
	return JSON.stringify(
		[...form.querySelectorAll("input,select,textarea")]
			.filter((e) => !["submit", "button", "reset"].includes(e.type))
			.map((e) => [
				e.name || e.id || JSON.stringify(e.dataset),
				e.type === "checkbox" || e.type === "radio"
					? e.checked
					: e.type === "file"
						? [...e.files].map((f) => [f.name, f.size, f.lastModified])
						: e._revealedOriginal !== undefined &&
								e.value === e._revealedOriginal
							? ""
							: e.value,
			]),
	);
}
let formSequence = 0;
export function trackForm(
	form,
	{
		readOnly = false,
		onChange = () => {},
		buttons,
		valid = () => form.checkValidity(),
	} = {},
) {
	if (!form) return null;
	let baseline = values(form),
		busy = false;
	const controls = buttons || [
		...form.querySelectorAll('button[type="submit"]'),
	];
	const feedback = document.createElement("p");
	feedback.className = "form-guidance";
	feedback.id = "form-guidance-" + ++formSequence;
	feedback.setAttribute("role", "status");
	if (controls.length)
		(form.querySelector(".form-actions") || form).append(feedback);
	for (const button of controls) {
		button.setAttribute(
			"aria-describedby",
			[button.getAttribute("aria-describedby"), feedback.id]
				.filter(Boolean)
				.join(" "),
		);
	}
	const errors = new Map();
	function removeDescription(element, id) {
		const ids = (element.getAttribute("aria-describedby") || "")
			.split(/\s+/)
			.filter((value) => value && value !== id);
		if (ids.length) element.setAttribute("aria-describedby", ids.join(" "));
		else element.removeAttribute("aria-describedby");
	}
	function fieldFeedback(event) {
		const field = event.target;
		if (!field.matches("input,select,textarea") || !field.willValidate) return;
		let error = errors.get(field);
		if (field.validity.valid) {
			field.removeAttribute("aria-invalid");
			if (error) error.hidden = true;
			return;
		}
		if (!error) {
			error = document.createElement("span");
			error.className = "field-error";
			error.id = feedback.id + "-error-" + errors.size;
			errors.set(field, error);
			field.insertAdjacentElement("afterend", error);
			field.setAttribute(
				"aria-describedby",
				[field.getAttribute("aria-describedby"), error.id]
					.filter(Boolean)
					.join(" "),
			);
		}
		error.hidden = false;
		error.textContent = field.validationMessage;
		field.setAttribute("aria-invalid", "true");
	}
	function correctField(event) {
		if (errors.has(event.target)) fieldFeedback(event);
	}
	form.addEventListener("focusout", fieldFeedback);
	form.addEventListener("input", correctField);
	const tracker = {
		dirty: () => values(form) !== baseline,
		busy: (value) => {
			busy = value;
			update();
		},
		reset: () => {
			baseline = values(form);
			update();
		},
		refresh: () => update(),
		destroy: () => {
			form.removeEventListener("input", update);
			form.removeEventListener("change", update);
			form.removeEventListener("focusout", fieldFeedback);
			form.removeEventListener("input", correctField);
			feedback.remove();
			for (const button of controls) removeDescription(button, feedback.id);
			for (const [field, error] of errors) {
				removeDescription(field, error.id);
				error.remove();
				field.removeAttribute("aria-invalid");
			}
		},
	};
	function update() {
		const dirty = tracker.dirty();
		form.dataset.dirty = String(dirty);
		for (const button of controls) {
			button.disabled = readOnly || busy || !dirty || !valid();
			button.setAttribute("aria-busy", String(busy));
			button.title = readOnly
				? "Read-only workspace"
				: busy
					? "Saving changes"
					: !dirty
						? "No changes to save"
						: !valid()
							? "Complete the required fields"
							: "";
		}
		const invalid = [...form.elements].find(
			(e) => e.willValidate && !e.validity.valid,
		);
		const message = readOnly
			? "Changes are disabled in this read-only workspace."
			: busy
				? "Saving your changes…"
				: !dirty
					? "Make a change to enable saving."
					: invalid
						? invalid.validationMessage
						: !valid()
							? "Check the field values and limits before saving."
							: "Changes are ready to save.";
		if (feedback.textContent !== message) feedback.textContent = message;
		onChange(dirty);
	}
	form.addEventListener("input", update);
	form.addEventListener("change", update);
	form._formState = tracker;
	update();
	return tracker;
}
