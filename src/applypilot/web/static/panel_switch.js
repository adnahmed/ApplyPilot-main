/* Native button navigation: no incomplete ARIA tab keyboard contract. */
export function panelSwitch(
	root,
	groups,
	{ initial, label = "Workspace views", onChange = () => {} } = {},
) {
	const bar = document.createElement("div");
	bar.className = "view-switch";
	bar.setAttribute("role", "group");
	bar.setAttribute("aria-label", label);
	const entries = [];
	for (const group of groups) {
		const section = document.createElement("section");
		section.id = "panel-" + group.key;
		section.className = "workspace-panel";
		section.setAttribute("aria-label", group.label);
		for (const node of group.nodes.filter(Boolean)) section.append(node);
		const button = document.createElement("button");
		button.className = "btn";
		button.type = "button";
		button.textContent = group.label;
		button.dataset.panel = group.key;
		button.setAttribute("aria-controls", section.id);
		entries.push({ key: group.key, section, button });
		bar.append(button);
		root.append(section);
		button.onclick = () => show(group.key);
	}
	root.prepend(bar);
	function show(key, { focus = false } = {}) {
		const next = entries.find((e) => e.key === key) || entries[0];
		for (const entry of entries) {
			entry.section.hidden = entry !== next;
			entry.button.setAttribute("aria-pressed", String(entry === next));
		}
		if (focus) next.button.focus({ preventScroll: true });
		onChange(next.key);
	}
	show(initial);
	return { show };
}
