const esc = (value) =>
	String(value ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const words = (value) => String(value || "").replaceAll("_", " ");
const when = (value) => {
	const date =
		typeof value === "number" ? new Date(value * 1000) : new Date(value);
	return value && !isNaN(date) ? date.toLocaleString() : "not scheduled";
};

export function recoveryView(value) {
	if (!value?.available)
		return `<h3>Recovery</h3><p>${esc(value?.reason || "Recovery report unavailable; retry timing is unknown.")}</p>`;
	const items = (value.items || []).slice(0, 8);
	return `<h3>Automatic recovery</h3><p>${esc(value.pending)} waiting · ${esc(value.running)} running · ${esc(value.completed)} completed · checked ${esc(when(value.at))}</p>${items.length ? `<ul>${items.map((item) => `<li><strong>${esc(words(item.stage))}: ${esc(words(item.reason_code))}</strong> · ${/verif|submission|intent/.test(item.reason_code || "") ? "Checking submission" : "Automatic retry"} · next retry ${esc(when(item.next_retry_at))}${item.job_url ? `<small> ${esc(item.job_url)}</small>` : ""}</li>`).join("")}</ul>` : "<p>No recovery work is queued in this report.</p>"}`;
}
