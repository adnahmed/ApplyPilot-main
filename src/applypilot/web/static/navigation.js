/* One navigation contract across desktop and touch widths. Ordinary links, not
 * an ARIA menu. Closing the mobile disclosure removes it from focus and AT. */
export function mountNavigation({ localGet, localSet }) {
	const sidebar = document.querySelector("#sidebar");
	const toggle = document.querySelector("#nav-toggle");
	const closeButton = document.querySelector("#nav-close");
	const searchToggle = document.querySelector("#search-toggle");
	const search = document.querySelector("#global-search");
	const searchForm = document.querySelector("#global-search-form");
	const mobile = matchMedia("(max-width:760px)");
	let open = false;
	document.documentElement.classList.toggle(
		"nav-collapsed",
		localGet("ap-nav-collapsed", "false") === "true",
	);
	function sync() {
		const collapsed =
			document.documentElement.classList.contains("nav-collapsed");
		sidebar.hidden = mobile.matches && !open;
		sidebar.inert = mobile.matches && !open;
		sidebar.classList.toggle("open", mobile.matches && open);
		closeButton.hidden = !mobile.matches;
		toggle.setAttribute(
			"aria-expanded",
			String(mobile.matches ? open : !collapsed),
		);
		toggle.setAttribute(
			"aria-label",
			mobile.matches
				? open
					? "Close navigation"
					: "Open navigation"
				: collapsed
					? "Expand navigation"
					: "Collapse navigation",
		);
		if (!mobile.matches) {
			searchForm.hidden = false;
			searchToggle.setAttribute("aria-expanded", "false");
		} else
			searchForm.hidden = searchToggle.getAttribute("aria-expanded") !== "true";
	}
	function close(restore = true) {
		open = false;
		sync();
		if (restore && mobile.matches) toggle.focus({ preventScroll: true });
	}
	function revealSearch() {
		searchForm.hidden = false;
		searchToggle.setAttribute("aria-expanded", "true");
		search.focus({ preventScroll: true });
	}
	toggle.addEventListener("click", () => {
		if (mobile.matches) {
			open = !open;
			sync();
			if (open) sidebar.querySelector("a")?.focus();
		} else {
			const collapsed =
				document.documentElement.classList.toggle("nav-collapsed");
			localSet("ap-nav-collapsed", String(collapsed));
			sync();
		}
	});
	closeButton.addEventListener("click", () => close());
	searchToggle.addEventListener("click", () => {
		if (searchToggle.getAttribute("aria-expanded") === "true") {
			searchToggle.setAttribute("aria-expanded", "false");
			sync();
		} else revealSearch();
	});
	document.addEventListener("keydown", (e) => {
		if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
			e.preventDefault();
			close(false);
			revealSearch();
		}
		if (e.key === "Escape") {
			if (mobile.matches && open) {
				e.preventDefault();
				close();
			} else if (
				mobile.matches &&
				searchForm.contains(document.activeElement)
			) {
				searchToggle.setAttribute("aria-expanded", "false");
				sync();
				searchToggle.focus();
			}
		}
	});
	// A non-modal disclosure must not obscure focus after it leaves the panel.
	document.addEventListener("focusin", (e) => {
		if (
			mobile.matches &&
			open &&
			!sidebar.contains(e.target) &&
			e.target !== toggle
		)
			close(false);
	});
	document.addEventListener("click", (e) => {
		if (
			mobile.matches &&
			open &&
			!sidebar.contains(e.target) &&
			!toggle.contains(e.target)
		)
			close(false);
	});
	mobile.addEventListener("change", () => {
		open = false;
		sync();
	});
	for (const link of sidebar.querySelectorAll("a[data-view]")) {
		const label =
			link.querySelector("span:nth-child(2)")?.textContent.trim() ||
			link.dataset.view;
		link.setAttribute("aria-label", label);
		link.title = label;
	}
	document.querySelector("#search-shortcut").textContent =
		/Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ K" : "Ctrl K";
	sync();
	return { close, sync, revealSearch };
}
