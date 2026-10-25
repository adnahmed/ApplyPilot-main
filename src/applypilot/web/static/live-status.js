/* Display producer records independently; these values never authorize actions. */
export const statusTime = (value) =>
	typeof value === "number"
		? value < 1e12
			? value * 1000
			: value
		: Date.parse(value || "");
export function statusComponent(status, name, now = Date.now()) {
	const record = status?.components?.[name];
	const matching = !!record && record.runtime_id === status.runtime_id;
	const at = matching ? statusTime(record.updated_at) : NaN;
	return {
		record: matching ? record : null,
		payload: matching ? record.payload || {} : {},
		at,
		current:
			matching && Number.isFinite(at) && now - at >= -5000 && now - at < 16000,
	};
}
export function runtimeStatus(status, control) {
	const part = statusComponent(status, "runtime"),
		value = part.payload;
	return {
		...value,
		runtime_id: status?.runtime_id,
		at: Number.isFinite(part.at) ? new Date(part.at).toISOString() : null,
		age_seconds: Math.max(0, (Date.now() - part.at) / 1000),
		stale: !part.current,
		running: part.current && value.running === true,
		pipeline: value.pipeline || value.engine || {},
		workers: value.workers || [],
		services: value.services || {},
		control,
	};
}
export function statusView(status) {
	const runtime = statusComponent(status, "runtime"),
		execution = statusComponent(status, "execution");
	const preparation = statusComponent(status, "preparation"),
		applications = statusComponent(status, "applications");
	const selectionAt = statusTime(applications.payload.checked_at),
		selectionAge = Date.now() - selectionAt;
	applications.selectionCurrent =
		applications.current &&
		applications.payload.selection_available === true &&
		Number.isFinite(selectionAge) &&
		selectionAge >= -5000 &&
		selectionAge <= 30000;
	const stages = {};
	for (const [part, rows] of [
		[runtime, runtime.payload.services],
		[preparation, preparation.payload.stages],
	]) {
		for (const [name, row] of Object.entries(rows || {})) {
			stages[name] = { ...row, sample_at: part.record?.updated_at };
		}
	}
	for (const [name, row] of Object.entries(execution.payload.stages || {})) {
		const previous = stages[name] || {};
		if (execution.at < statusTime(previous.sample_at)) continue;
		const bufferPaused = previous.buffer?.paused === true;
		stages[name] = {
			...previous,
			...row,
			sample_at: execution.record?.updated_at,
			state:
				row.active_tasks > 0
					? "running"
					: row.queued_tasks > 0
						? "queued"
						: previous.state || "waiting",
			reason: bufferPaused
				? `New work paused. ${previous.buffer.reason}`
				: row.active_tasks > 0
					? "Task executing."
					: row.queued_tasks > 0
						? "Waiting for an execution slot or dependency."
						: previous.reason || "No active task reported.",
		};
	}
	const workers = (runtime.payload.workers || []).map((worker) => {
		const active =
			(worker.status === "applying" || worker.execution_active === true) &&
			runtime.payload.running === true;
		const waiting =
			worker.status === "applying" && Number(worker.model_wait_since) > 0;
		return {
			...worker,
			key: "application:" + worker.worker_id,
			kind: "application",
			worker: "Application " + worker.worker_id,
			stage: "applications",
			state: !runtime.current
				? "unverified"
				: runtime.payload.running === false
					? "stopped"
					: active
						? waiting
							? "waiting_provider"
							: "running"
						: worker.status === "done"
							? "stopped"
							: worker.status === "idle"
								? "idle"
								: "waiting",
			phase: waiting
				? "waiting_provider"
				: worker.status_reason || worker.status,
			reason: worker.last_action || worker.status_reason || worker.status,
			fresh: runtime.current,
			active: runtime.current && active,
			executing: runtime.current && worker.execution_active === true,
			sample_at: runtime.record?.updated_at,
			started_at: worker.start_time,
			reported_applied: worker.jobs_applied,
			reported_failed: worker.jobs_failed,
			last_job_title: worker.job_title,
			runtime_id: status.runtime_id,
		};
	});
	for (const worker of runtime.payload.worker_activity || [])
		workers.push({
			...worker,
			kind: worker.kind || "service",
			sample_at: runtime.record?.updated_at,
			runtime_id: status.runtime_id,
			fresh: runtime.current,
			active:
				runtime.current &&
				runtime.payload.running === true &&
				worker.active === true,
			state: !runtime.current
				? "unverified"
				: runtime.payload.running === false
					? "stopped"
					: worker.state,
		});
	return {
		runtime,
		execution,
		preparation,
		applications,
		stages,
		workers,
		current: [runtime, execution, preparation, applications].some(
			(part) => part.current,
		),
		at: Math.max(
			0,
			...[runtime, execution, preparation, applications]
				.map((part) => part.at)
				.filter(Number.isFinite),
		),
	};
}
