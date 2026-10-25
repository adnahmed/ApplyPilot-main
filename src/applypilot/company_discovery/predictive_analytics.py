"""Company research analytics informed by LiamKozma/job-swarm.

Independent implementation of the ideas in trajectory_engine.py and
ghost_engine.py: profile-facet alignment, dated semantic trajectories, regime
simulation, posting-arrival intensity, role archetypes, and diverse selection.
Reference: https://github.com/LiamKozma/job-swarm . These are research signals;
semantic volatility and predicted roles never establish a vacancy or hiring odds.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import hashlib
from statistics import median
from threading import RLock
from urllib.parse import urlsplit
import warnings

import numpy as np


EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_ENCODE_LOCK = RLock()
_EMBEDDINGS = OrderedDict()


def _date(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _unit(vector):
    vector = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else np.zeros_like(vector)


@lru_cache(maxsize=1)
def _model():
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(EMBEDDING_MODEL, device="cpu", trust_remote_code=False)
        model.max_seq_length = 256
        return model
    except Exception as exc:
        raise RuntimeError(f"Predictive company embeddings unavailable ({EMBEDDING_MODEL}): {exc}") from exc


def _embedding(text):
    text = str(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    words = text.split()
    if not words:
        raise ValueError("Predictive company embeddings require nonempty text")
    chunks = [" ".join(words[start:start + 140]) for start in range(0, len(words), 140)]
    with _ENCODE_LOCK:
        if digest in _EMBEDDINGS:
            _EMBEDDINGS.move_to_end(digest)
            return _EMBEDDINGS[digest]
        try:
            vectors = _model().encode(chunks, batch_size=16, convert_to_numpy=True, show_progress_bar=False)
        except Exception as exc:
            raise RuntimeError(f"Predictive company embedding failed: {exc}") from exc
        vectors = np.asarray(vectors, dtype=np.float64)
        if vectors.ndim != 2 or not np.isfinite(vectors).all():
            raise ValueError("Predictive company embedder returned invalid vectors")
        result = _unit(vectors.mean(axis=0))
        if not np.any(result):
            raise ValueError("Predictive company embedder returned an empty vector")
        _EMBEDDINGS[digest] = result
        if len(_EMBEDDINGS) > 2048:
            _EMBEDDINGS.popitem(last=False)
        return result


def _text(row):
    return "\n".join(str(row.get(key) or "").strip() for key in ("title", "text")).strip()


def _host(value):
    value = str(value or "").strip()
    return (urlsplit(value if "://" in value else "https://" + value).hostname or "").lower().removeprefix("www.")


def _own_postings(postings):
    return [row for row in postings if row.get("is_target") is True or (
        _host(row.get("website")) and _host(row.get("website")) == _host(row.get("company_website")))]


def _trajectory(documents, vectors, now):
    by_day = defaultdict(list)
    for document, vector in zip(documents, vectors):
        stamp = _date(document.get("event_date"))
        if document.get("authored") is True and stamp and stamp <= now:
            by_day[stamp.date()].append(vector)
    days = sorted(by_day)
    result = {"regime": "insufficient_history", "escalation": None, "hurdle_probability": None,
              "dated_authored_days": len(days), "minimum_days": 5,
              "interpretation": "Semantic change only; not a probability of hiring or a verified business problem."}
    if len(days) < 5:
        return result
    states = [_unit(np.mean(by_day[day], axis=0)) for day in days]
    shifts = np.array([(1.0 - np.clip(states[i] @ states[i + 1], -1.0, 1.0)) /
                       np.sqrt((days[i + 1] - days[i]).days) for i in range(len(days) - 1)])
    result["shift_series"] = shifts.round(6).tolist()
    if float(shifts.std()) < 1e-8:
        result.update(regime="steady_state", fit_status="constant_history")
        return result
    from hmmlearn.hmm import GaussianHMM
    fitted = []
    scale = float(shifts.std())
    observations = (shifts / scale).reshape(-1, 1)
    for seed in range(5):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                model = GaussianHMM(n_components=2, covariance_type="diag", n_iter=200,
                                    random_state=seed, min_covar=1e-4).fit(observations)
                likelihood = float(model.score(observations))
            if np.isfinite(likelihood) and np.isfinite(model.transmat_).all():
                fitted.append((likelihood, model))
        except (ValueError, FloatingPointError, np.linalg.LinAlgError):
            continue
    if not fitted:
        result.update(regime="unestimable", fit_status="regime_fit_failed")
        return result
    model = max(fitted, key=lambda item: item[0])[1]
    means = model.means_.ravel() * scale
    variances = np.maximum(model.covars_.reshape(2, -1)[:, 0] * scale ** 2, 1e-12)
    high = int(np.argmax(variances))
    current = int(model.predict(observations)[-1])
    distinct = bool(variances[high] > 1.5 * variances[1 - high])
    hurdle = distinct and current == high
    rng = np.random.default_rng(42)
    regimes = np.full(4000, current, dtype=int)
    values = np.full(4000, shifts[-1], dtype=float)
    average = np.zeros(4000)
    for _ in range(90):
        values += means[regimes] / 90.0 + np.sqrt(variances[regimes] / 90.0) * rng.standard_normal(4000)
        average += values / 90.0
        regimes = (rng.random(4000) >= model.transmat_[regimes, 0]).astype(int)
    escalation = float(np.mean(average > means[high])) if distinct else None
    result.update(regime="hurdle_state" if hurdle else "steady_state", fit_status="fitted",
                  escalation=round(escalation, 6) if escalation is not None else None,
                  hurdle_probability=round(0.6 * float(hurdle) + 0.4 * escalation, 6)
                  if escalation is not None else None,
                  regime_means=means.round(6).tolist(), regime_variances=variances.round(8).tolist(),
                  transition_matrix=model.transmat_.round(6).tolist(),
                  simulation={"method": "Euler-Maruyama semantic-shift scenario", "paths": 4000,
                              "steps": 90, "time_step": 1 / 90, "calibrated_hiring_probability": False})
    return result


def _hawkes(postings, snapshots, now):
    observed = sorted(stamp for row in snapshots if (stamp := _date(row.get("observed_at"))) and stamp <= now)
    if not observed or (now - observed[0]).total_seconds() < 7 * 86400:
        return {"status": "insufficient_observation_window", "minimum_days": 7}
    start = max(observed[0], now - timedelta(days=120))
    # First inventory is a left-censored baseline, not a burst of new roles.
    arrivals = {}
    for row in postings:
        stamp = _date(row.get("first_seen_at"))
        if row.get("url") and stamp and start.date() < stamp.date() and stamp <= now:
            arrivals[str(row["url"])] = stamp
    if len(arrivals) < 6:
        return {"status": "insufficient_arrivals", "arrivals": len(arrivals), "minimum_arrivals": 6}
    times = np.array(sorted((stamp - start).total_seconds() / 86400 for stamp in arrivals.values()))
    duration = (now - start).total_seconds() / 86400
    beta = 1 / 7
    difference = times[:, None] - times[None, :]
    kernel = np.where(difference > 0, beta * np.exp(-beta * np.maximum(difference, 0)), 0)
    edge = 1 - np.exp(-beta * (duration - times))
    baseline, branching = len(times) / duration * 0.7, 0.3
    for _ in range(50):
        intensity = baseline + branching * kernel.sum(axis=1)
        background = baseline / np.maximum(intensity, 1e-12)
        baseline = float(background.sum() / duration)
        branching = float(np.clip((1 - background).sum() / max(float(edge.sum()), 1e-9), 0, 0.95))
    intensity_now = baseline + branching * float(np.sum(beta * np.exp(-beta * (duration - times))))
    return {"status": "estimated", "arrivals": len(times), "observation_days": round(duration, 2),
            "baseline_per_day": round(baseline, 6), "intensity_per_day": round(intensity_now, 6),
            "branching_ratio": round(branching, 6), "burst_ratio": round(intensity_now / max(baseline, 1e-9), 4),
            "interpretation": "Observed posting arrivals; first discovery is not necessarily the publication date."}


def _hiring(postings, snapshots, now):
    groups = defaultdict(dict)
    for row in snapshots:
        stamp = _date(row.get("observed_at"))
        if row.get("complete") is not True or not row.get("scope") or not stamp or stamp > now:
            continue
        if (now - stamp).days > 120:
            continue
        if not isinstance(row.get("total"), (int, float)) or row["total"] < 0:
            continue
        key = (str(row["scope"]), str(row.get("intent_fingerprint") or ""))
        previous = groups[key].get(stamp.date())
        if previous is None or _date(previous["observed_at"]) < stamp:
            groups[key][stamp.date()] = row
    scopes = []
    for (scope, fingerprint), daily in sorted(groups.items()):
        rows = [daily[day] for day in sorted(daily) if (now.date() - day).days <= 28]
        item = {"scope": scope, "intent_fingerprint": fingerprint, "observed_days": len(rows),
                "status": "insufficient_history", "minimum_days": 5,
                "hawkes": _hawkes([row for row in postings if str(row.get("scope") or "") == scope],
                                  list(daily.values()), now)}
        if rows:
            item.update(latest_total=rows[-1]["total"], latest_relevant=rows[-1].get("relevant"),
                        observed_at=rows[-1]["observed_at"], source_url=rows[-1].get("source_url"))
        if len(rows) >= 5:
            days = [_date(row["observed_at"]).date().toordinal() for row in rows]
            slope = median((rows[j]["total"] - rows[i]["total"]) / (days[j] - days[i])
                           for i in range(len(rows)) for j in range(i + 1, len(rows)))
            item.update(status="estimated", slope_per_day=round(slope, 6),
                        direction="growing" if slope > 0 else "contracting" if slope < 0 else "stable",
                        first_total=rows[0]["total"], source="complete_board_observations")
        scopes.append(item)
    persistent = []
    for row in postings:
        first, last = _date(row.get("first_seen_at")), _date(row.get("last_seen_at"))
        if row.get("active") is True and first and last and first <= last <= now and (last - first).days >= 30:
            persistent.append({"url": row.get("url"), "title": row.get("title"),
                               "observed_days": (last - first).days, "last_seen_at": last.isoformat()})
    return {"scopes": scopes, "persistent_roles": persistent[:20], "repost_candidates": _reposts(postings, now),
            "interpretation": "Board contraction and long-lived postings do not establish a hiring freeze or fake vacancy."}


def _reposts(postings, now):
    candidates = []
    for current in postings:
        first = _date(current.get("first_seen_at"))
        if current.get("active") is not True or not first or not 0 <= (now - first).days <= 14:
            continue
        for previous in postings:
            last = _date(previous.get("last_seen_at"))
            if (previous.get("active") is not False or not last or not 4 <= (first - last).days <= 120 or
                    not current.get("url") or not previous.get("url") or current["url"] == previous["url"] or
                    not str(current.get("title") or "").strip() or
                    str(current.get("title")).casefold() != str(previous.get("title")).casefold() or
                    not _text(current) or not _text(previous)):
                continue
            similarity = float(_embedding(_text(current)) @ _embedding(_text(previous)))
            if similarity >= 0.92:
                candidates.append({"url": current["url"], "previous_url": previous["url"],
                                   "title": current.get("title"), "similarity": round(similarity, 6),
                                   "gap_days": (first - last).days, "kind": "possible_repost",
                                   "interpretation": "Similar role returned after an inactive listing; reason is unknown."})
    return sorted(candidates, key=lambda row: (-row["similarity"], row["url"], row["previous_url"]))[:20]


def _predicted_roles(postings, own, state, now):
    if any(row.get("active") is True and row.get("relevant") is not False for row in own):
        return [], "existing_or_unassessed_target_openings"
    # Role taxonomy is learned from actual observed vacancies, including other employers.
    corpus = {}
    for row in postings:
        seen = _date(row.get("last_seen_at"))
        if (row.get("active") is True and row.get("url") and _text(row) and seen and
                0 <= (now - seen).total_seconds() <= 60 * 86400):
            corpus[str(row["url"])] = row
    rows = [corpus[key] for key in sorted(corpus)]
    if len(rows) < 40:
        return [], "insufficient_posting_corpus"
    from sklearn.cluster import KMeans
    vectors = np.array([_embedding(_text(row)) for row in rows])
    clusters = min(24, max(2, len(rows) // 15), len(np.unique(vectors.round(6), axis=0)))
    model = KMeans(n_clusters=clusters, random_state=42, n_init=5).fit(vectors)
    centers = np.array([_unit(vector) for vector in model.cluster_centers_])
    similarities = centers @ state
    results = []
    for index in np.argsort(-similarities, kind="stable")[:3]:
        if similarities[index] < 0.35:
            continue
        members = np.flatnonzero(model.labels_ == index)
        nearest = sorted(members, key=lambda i: (-float(vectors[i] @ centers[index]), str(rows[i]["url"])))[:3]
        results.append({"kind": "prospective_role_hypothesis", "similarity": round(float(similarities[index]), 6),
                        "titles": list(dict.fromkeys(str(rows[i].get("title") or "") for i in nearest)),
                        "example_urls": [str(rows[i]["url"]) for i in nearest], "corpus_postings": len(members),
                        "interpretation": "Research hypothesis from similar work; no vacancy or hiring intent is established."})
    return results, "estimated" if results else "no_supported_archetype"


def analyze(documents, profile: str, facets: list[str], postings: list[dict], snapshots: list[dict], *, now=None):
    """Analyze one employer; mark corpus postings with website/company_website.

    An own posting marked relevant=False is known not to match. Missing relevance
    blocks speculative role predictions while existing openings await assessment.
    Snapshots must be complete per-board inventories; mixed scopes never form a
    count trend. Optional intent_fingerprint also separates search preferences.
    """
    now = _date(now) if now is not None else datetime.now(timezone.utc)
    if now is None or not str(profile or "").strip():
        raise ValueError("Predictive company analysis requires a profile and a valid clock")
    documents = [dict(row) for row in documents if _text(row)]
    if not documents:
        raise ValueError("Predictive company analysis requires observed company documents")
    documents.sort(key=lambda row: (_date(row.get("event_date")) or _date(row.get("observed_at")) or
                                   datetime.min.replace(tzinfo=timezone.utc), str(row.get("id") or row.get("url"))))
    vectors = [_embedding(_text(row)) for row in documents]
    authored = [vector for row, vector in zip(documents, vectors) if row.get("authored") is True]
    state = _unit(np.mean((authored or vectors)[-3:], axis=0))
    pooled = float(state @ _embedding(profile))
    facets = [str(value).strip() for value in facets if str(value).strip()]
    facet_alignment = sorted(float(state @ _embedding(value)) for value in facets)
    alignment = 0.5 * pooled + 0.5 * float(np.mean(facet_alignment[-2:])) if facet_alignment else pooled
    if not authored:
        alignment *= 0.85
    dates = [stamp for row in documents if (stamp := _date(row.get("event_date"))) and stamp <= now]
    recency = 0.5 ** ((now - max(dates)).total_seconds() / 86400 / 90) if dates else 0.3
    trajectory = _trajectory(documents, vectors, now)
    own = _own_postings(postings)
    predicted, prediction_status = _predicted_roles(postings, own, state, now)
    return {**trajectory, "embedding_model": EMBEDDING_MODEL, "alignment": round(alignment, 6),
            "score_basis": "heuristic",
            "recency": round(recency, 6),
            "prescore": round(0.65 * max(alignment, 0) + 0.15 * (trajectory["hurdle_probability"] or 0) +
                              0.20 * recency, 6),
            "predicted_roles": predicted, "predicted_role_status": prediction_status,
            "hiring": _hiring(own, snapshots, now), "state_vector": state.round(8).tolist()}


def select_diverse(candidates: list[dict], limit: int, exploration_slots: int = 1) -> list[dict]:
    """Deterministic MMR selection plus a bounded middle-score exploration slot."""
    limit = max(0, int(limit))
    if not limit:
        return []
    pool = {}
    for item in sorted(candidates, key=lambda row: (-float(row.get("prescore") or 0), str(row.get("website") or ""))):
        key = _host(item.get("website"))
        if key:
            pool.setdefault(key, dict(item))
    remaining, selected = list(pool.values()), []
    explore = min(max(0, int(exploration_slots)), limit // 4 or (1 if limit >= 2 else 0))
    reserved = [row for row in remaining if 0.30 <= float(row.get("prescore") or 0) < 0.45][:explore]
    remaining = [row for row in remaining if row not in reserved]

    def value(row):
        vector = _unit(row.get("state_vector") or [])
        redundancy = max((float(vector @ other) for entry in selected
                          if len(other := _unit(entry.get("state_vector") or [])) == len(vector) and len(vector)),
                         default=0)
        return float(row.get("prescore") or 0) - 0.15 * max(0, redundancy)

    while remaining and len(selected) < limit - len(reserved):
        choice = min(remaining, key=lambda row: (-value(row), str(row.get("website"))))
        remaining.remove(choice)
        selected.append({**choice, "chosen_by": "score"})
    selected.extend({**row, "chosen_by": "explore"} for row in reserved[:limit - len(selected)])
    return selected


def calibrate(candidates, outcomes):
    """Promote reply ranking only after chronological, employer-disjoint evaluation.

    Outcomes need an explicit replied/rejected label and the saved report at the
    time of that decision. Closed or missing outcomes are not negative examples.
    """
    result = [{**row, "score_basis": "heuristic"} for row in candidates]
    metadata = {"promoted": False, "score_basis": "heuristic", "minimum_employers": 40,
                "minimum_holdout": 10, "reason": "insufficient_labeled_employers"}

    def features(row):
        source = row.get("analytics") or row
        try:
            values = [float(source["alignment"]), float(source.get("hurdle_probability") or 0),
                      float(source["recency"])]
            return values if all(np.isfinite(value) for value in values) else None
        except (KeyError, ValueError, TypeError):
            return None

    latest = {}
    for outcome in outcomes:
        website, stamp = _host(outcome.get("website")), _date(outcome.get("observed_at"))
        if website and stamp and (website not in latest or stamp > latest[website][0]):
            latest[website] = (stamp, outcome)
    observations = []
    for website, (stamp, outcome) in latest.items():
        label = outcome.get("outcome")
        report = outcome.get("report") or {}
        vector = features(report)
        if label in {"replied", "rejected"} and vector is not None:
            observations.append((stamp, website, vector, int(label == "replied")))
    observations.sort(key=lambda row: (row[0], row[1]))
    metadata["labeled_employers"] = len(observations)
    if len(observations) < 40:
        return result, metadata
    holdout_size = max(10, len(observations) // 4)
    split = len(observations) - holdout_size
    # Equal-timestamp decisions stay together; alphabetical order is not time.
    while split > 0 and observations[split - 1][0] == observations[split][0]:
        split -= 1
    train, holdout = observations[:split], observations[split:]
    metadata.update(training_employers=len(train), holdout_employers=len(holdout))
    if len(train) < 20 or any(len({row[3] for row in batch}) < 2 for batch in (train, holdout)):
        metadata["reason"] = "insufficient_chronological_class_coverage"
        return result, metadata
    from sklearn.linear_model import LogisticRegression
    x_train, y_train = np.array([row[2] for row in train]), np.array([row[3] for row in train])
    x_holdout, y_holdout = np.array([row[2] for row in holdout]), np.array([row[3] for row in holdout])
    model = LogisticRegression(C=0.5, random_state=42, solver="liblinear", max_iter=1000)
    model.fit(x_train, y_train)
    prediction = model.predict_proba(x_holdout)[:, 1]
    heuristic = 0.65 * np.maximum(x_holdout[:, 0], 0) + 0.15 * x_holdout[:, 1] + 0.20 * x_holdout[:, 2]
    model_loss = float(np.mean((prediction - y_holdout) ** 2))
    baseline_loss = float(np.mean((float(y_train.mean()) - y_holdout) ** 2))
    heuristic_loss = float(np.mean((heuristic - y_holdout) ** 2))
    metadata.update(method="logistic_reply_ranking", model_brier=round(model_loss, 6),
                    baseline_brier=round(baseline_loss, 6), heuristic_brier=round(heuristic_loss, 6),
                    holdout_start=holdout[0][0].isoformat(), reason="heldout_improvement_not_demonstrated")
    if not (model_loss + 1e-6 < baseline_loss and model_loss + 1e-6 < heuristic_loss):
        return result, metadata
    model.fit(np.array([row[2] for row in observations]), np.array([row[3] for row in observations]))
    for row in result:
        vector = features(row)
        if vector is not None:
            probability = round(float(model.predict_proba([vector])[0, 1]), 6)
            row.update(heuristic_prescore=row.get("prescore"), prescore=probability,
                       calibrated_reply_probability=probability, score_basis="outcome_calibrated")
    metadata.update(promoted=True, score_basis="outcome_calibrated", reason="heldout_brier_improved",
                    interpretation="Reply ranking learned from explicit outcomes; not a hiring probability.")
    return result, metadata
