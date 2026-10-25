/* One authenticated status stream per visible tab. Ordinary data uses HTTP. */
export function connectLive({
	onStatus,
	onDataChanged,
	onAutomationChanged,
	onSessionExpired,
	onState,
}) {
	let source = null,
		disposed = false,
		healthy = false,
		changeTimer = null,
		lastChange = 0;
	const enabled = () => !disposed && !document.hidden;
	function state(value) {
		healthy = value === "live";
		document.documentElement.dataset.liveTransport = value;
		onState?.(value);
	}
	function close() {
		if (source) {
			source.close();
			source = null;
		}
		clearTimeout(changeTimer);
		changeTimer = null;
		state(disposed ? "off" : "paused");
	}
	function open() {
		if (!enabled()) {
			close();
			return;
		}
		if (source) return;
		if (!("EventSource" in window)) {
			state("fallback");
			return;
		}
		state("connecting");
		let current;
		try {
			current = new EventSource("/api/live/status");
			source = current;
		} catch {
			state("fallback");
			return;
		}
		const valid = () => source === current && enabled();
		current.onopen = () => {
			if (valid()) state("connecting");
		};
		current.addEventListener("status", (event) => {
			if (!valid()) return;
			try {
				const value = JSON.parse(event.data);
				onStatus?.(value);
				state("live");
			} catch {
				state("fallback");
			}
		});
		current.addEventListener("data_changed", () => {
			if (!valid() || changeTimer !== null) return;
			changeTimer = setTimeout(
				() => {
					changeTimer = null;
					if (valid()) {
						lastChange = Date.now();
						onDataChanged?.();
					}
				},
				Math.max(250, 1000 - (Date.now() - lastChange)),
			);
		});
		current.addEventListener("automation_changed", () => {
			if (!valid()) return;
			(onAutomationChanged || onDataChanged)?.();
			state("live");
		});
		current.addEventListener("session-expired", () => {
			if (valid()) {
				close();
				onSessionExpired?.();
			}
		});
		current.onerror = () => {
			if (valid()) state("reconnecting");
		};
	}
	const visibility = () => (enabled() ? open() : close());
	document.addEventListener("visibilitychange", visibility);
	open();
	return {
		get healthy() {
			return healthy;
		},
		close() {
			disposed = true;
			close();
			document.removeEventListener("visibilitychange", visibility);
		},
	};
}
