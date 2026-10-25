// Small, local SVG charts: no remote scripts or build step. Raw integer counts,
// labelled axes, series toggles, native SVG titles, and a complete table fallback.
const escape = (value) =>
	String(value ?? "").replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);
const palette = {
	discovered: "var(--green)",
	submitted: "var(--blue)",
	outreach: "var(--orange)",
};
const names = {
	discovered: "Discovered",
	submitted: "Submitted",
	outreach: "Outreach sent",
};
let chartId = 0;
function plot(values, visible, title) {
	const id = "chart-" + ++chartId,
		W = 740,
		H = 190,
		left = 55,
		right = 22,
		top = 18,
		bottom = 30,
		w = W - left - right,
		h = H - top - bottom;
	const max = Math.max(
		1,
		...values.flatMap((row) => visible.map((key) => Number(row[key]) || 0)),
	);
	const magnitude = 10 ** Math.floor(Math.log10(max / 4)),
		step = Math.max(1, Math.ceil(max / 4 / magnitude) * magnitude),
		yMax = 4 * step;
	const x = (i) =>
			left + (values.length <= 1 ? w / 2 : (i * w) / (values.length - 1)),
		y = (v) => top + h - (v / yMax) * h;
	let content = `<title id="${id}">${escape(title)} by UTC day</title>`;
	for (let i = 0; i <= 4; i++) {
		const value = (yMax * i) / 4,
			cy = y(value);
		content += `<line class="chart-grid" x1="${left}" x2="${W - right}" y1="${cy}" y2="${cy}"/><text class="chart-text" x="${left - 10}" y="${cy + 4}" text-anchor="end">${Number.isInteger(value) ? value.toLocaleString() : value.toFixed(1)}</text>`;
	}
	for (const i of new Set([
		0,
		Math.round((values.length - 1) / 2),
		values.length - 1,
	])) {
		if (!values[i]) continue;
		const label = new Date(values[i].date + "T00:00:00Z").toLocaleDateString(
			undefined,
			{ month: "short", day: "numeric", timeZone: "UTC" },
		);
		content += `<text class="chart-text" x="${x(i)}" y="${H - 6}" text-anchor="middle">${escape(label)}</text>`;
	}
	for (const key of visible) {
		const points = values
			.map(
				(row, i) => `${x(i).toFixed(2)},${y(Number(row[key]) || 0).toFixed(2)}`,
			)
			.join(" ");
		content += `<polyline class="chart-line" points="${points}" stroke="${palette[key]}" ${key === "outreach" ? 'stroke-dasharray="4 4"' : ""}/>`;
		for (const [i, row] of values.entries())
			content += `<circle class="chart-point" cx="${x(i)}" cy="${y(Number(row[key]) || 0)}" r="4" fill="${palette[key]}"><title>${escape(row.date)} · ${names[key]}: ${Number(row[key]) || 0}</title></circle>`;
	}
	return `<section class="chart-panel"><h3>${escape(title)}</h3><p>Daily counts · this panel has its own count axis</p><svg viewBox="0 0 ${W} ${H}" role="img" aria-labelledby="${id}">${content}</svg></section>`;
}
export function lineChart(
	container,
	values,
	visible = ["discovered", "submitted", "outreach"],
) {
	if (!container) return;
	const outcomes = visible.filter((k) => k !== "discovered");
	container.innerHTML =
		(visible.includes("discovered")
			? plot(values, ["discovered"], "New discoveries")
			: "") +
		(outcomes.length ? plot(values, outcomes, "Submissions & outreach") : "");
}
export function chartTable(values) {
	return `<details class="chart-access"><summary>View accessible chart data · UTC</summary><div class="table-scroll"><table><caption>Daily activity counts · UTC</caption><thead><tr><th scope="col">Day (UTC)</th><th scope="col">Discovered</th><th scope="col">Submitted</th><th scope="col">Outreach sent</th></tr></thead><tbody>${values.map((row) => `<tr><td>${escape(row.date)}</td><td>${Number(row.discovered) || 0}</td><td>${Number(row.submitted) || 0}</td><td>${Number(row.outreach) || 0}</td></tr>`).join("")}</tbody></table></div></details>`;
}
