"""Opportunity-specific market salary enrichment.

Uses the posted range when it has an explicit currency/period; otherwise queries
FreeHire's aggregate salary insights with a conservative role scope. Results are
cached per job and fail open so compensation research never blocks scoring.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import re
from pathlib import Path

from applypilot import config
from applypilot.config import load_profile
from applypilot.discovery.freehire import _fetch_json_path
from applypilot.profile import selected_work_country

log = logging.getLogger(__name__)

DEFAULT_CACHE_HOURS = 168
DEFAULT_MIN_SAMPLES = 5

# Broaden geography/seniority, but never drop the role category: a country-wide
# salary band across unrelated professions is not useful compensation evidence.
SCOPE_ATTEMPTS = (
    {"country": True, "seniority": True, "name": "country-category-seniority"},
    {"country": True, "seniority": False, "name": "country-category"},
    {"country": False, "seniority": True, "name": "global-category-seniority"},
    {"country": False, "seniority": False, "name": "global-category"},
)

_CATEGORY_PATTERNS = (
    (re.compile(r"\bback[ -]?end\b", re.I), "backend"),
    (re.compile(r"\bfront[ -]?end\b", re.I), "frontend"),
    (re.compile(r"\bfull[ -]?stack\b", re.I), "fullstack"),
    (re.compile(r"\b(?:android|ios|mobile)\b", re.I), "mobile"),
    (re.compile(r"\bsite reliability\b|\bsre\b", re.I), "sre"),
    (re.compile(r"\bdevops\b", re.I), "devops"),
    (re.compile(r"\bdata engineer(?:ing)?\b", re.I), "data_engineering"),
    (re.compile(r"\bdata scien(?:ce|tist)\b", re.I), "data_science"),
    (re.compile(r"\bdata analy(?:st|tics)\b", re.I), "data_analytics"),
    (re.compile(r"\b(?:machine learning|ml engineer)\b", re.I), "ml_ai"),
    (re.compile(r"\b(?:ai engineer|artificial intelligence engineer)\b", re.I), "ai_engineering"),
    (re.compile(r"\b(?:quality assurance|qa engineer|test automation)\b", re.I), "qa"),
    (re.compile(r"\b(?:cybersecurity|security engineer)\b", re.I), "security"),
    (re.compile(r"\b(?:software engineer|software developer|application developer|web developer)\b", re.I),
     "software_engineering"),
)

_SENIORITY_PATTERNS = (
    (re.compile(r"\b(?:chief|cto|cio|ciso|c[- ]level)\b", re.I), "c_level"),
    (re.compile(r"\bprincipal\b", re.I), "principal"),
    (re.compile(r"\bstaff\b", re.I), "staff"),
    (re.compile(r"\blead\b", re.I), "lead"),
    (re.compile(r"\bsenior\b|\bsr\.?\b", re.I), "senior"),
    (re.compile(r"\b(?:mid|middle)[ -]?level\b", re.I), "middle"),
    (re.compile(r"\bjunior\b|\bjr\.?\b", re.I), "junior"),
    (re.compile(r"\bintern(?:ship)?\b", re.I), "intern"),
)


def _cache_path(job_dir: Path) -> Path:
    return job_dir / "salary_market.json"


def _read_cached(path: Path, country: str, category: str, seniority: str, cache_hours: int) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(str(data["fetched_at"]).replace("Z", "+00:00"))
        if datetime.now(UTC) - fetched > timedelta(hours=cache_hours):
            return None
        request = data.get("request") or {}
        if request != {"country": country, "category": category, "seniority": seniority}:
            return None
        selected = data.get("selected")
        if isinstance(selected, dict):
            return {**selected, "cache": "hit"}
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return None


def _write_cache(path: Path, country: str, category: str, seniority: str,
                 selected: dict, attempts: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "fetched_at": datetime.now(UTC).isoformat(),
            "request": {"country": country, "category": category, "seniority": seniority},
            "selected": selected,
            "attempts": attempts,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.debug("Failed to write salary cache: %s", exc)


def _unavailable(country: str, category: str, seniority: str,
                 attempts: list[dict], reason: str) -> dict:
    return {
        "status": "unavailable",
        "source": "freehire-salary-insights",
        "country": country,
        "category": category,
        "seniority": seniority,
        "attempts": attempts,
        "reason": reason,
    }


def _profile_scope(profile: Mapping | None) -> tuple[str, str]:
    profile = profile or {}
    compensation = profile.get("compensation") or {}
    if not isinstance(compensation, Mapping):
        compensation = {}
    category = str(
        compensation.get("market_salary_category")
        or profile.get("salary_insights_category")
        or ""
    ).strip()
    seniority = str(
        compensation.get("market_salary_seniority")
        or profile.get("salary_insights_seniority")
        or ""
    ).strip()
    return category, seniority


def _infer_title_scope(title: object) -> tuple[str, str]:
    text = str(title or "").strip()
    category = next((value for pattern, value in _CATEGORY_PATTERNS if pattern.search(text)), "")
    seniority = next((value for pattern, value in _SENIORITY_PATTERNS if pattern.search(text)), "")
    return category, seniority


def _posted_compensation(job: Mapping) -> dict | None:
    payload = job.get("discovery_normalized_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = None
    if not isinstance(payload, Mapping):
        return None
    rows = payload.get("compensation") or []
    if not isinstance(rows, list):
        return None
    selected_id = str(job.get("discovery_selected_arrangement") or "").strip()
    candidates = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        arrangement = str(row.get("arrangement_id") or "").strip()
        if arrangement and selected_id and arrangement != selected_id:
            continue
        low, high = row.get("min_value"), row.get("max_value")
        currency = str(row.get("currency") or "").strip().upper()
        period = str(row.get("period") or "").strip().lower()
        if low is None or not currency or not period:
            continue
        try:
            low = float(low)
            high = float(high) if high is not None else None
        except (TypeError, ValueError):
            continue
        if high is not None and high < low:
            continue
        candidates.append((low, high, currency, period))
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        return None
    low, high, currency, period = unique[0]
    return {"posted_min": low, "posted_max": high,
            "posted_currency": currency, "posted_period": period}


def resolve_market_salary(
    job_dir: Path,
    *,
    country: str = "",
    category: str = "",
    seniority: str = "",
    posted_min: float | None = None,
    posted_max: float | None = None,
    posted_currency: str = "",
    posted_period: str = "",
    min_samples: int = DEFAULT_MIN_SAMPLES,
    cache_hours: int = DEFAULT_CACHE_HOURS,
    profile_path: Path | None = None,
    profile: Mapping | None = None,
) -> dict:
    """Resolve one defensible salary reference, preferring the posting itself."""
    if posted_min is not None and posted_currency and posted_period:
        low = float(posted_min)
        high = float(posted_max) if posted_max is not None else None
        p25 = round(low + 0.25 * (high - low)) if high is not None and high >= low else round(low)
        return {
            "status": "available",
            "source": "posted-range",
            "value": p25,
            "currency": posted_currency,
            "period": posted_period,
            "p25": p25,
            "p50": None,
            "p75": None,
            "sample_size": None,
            "country": country,
            "category": category,
            "seniority": seniority,
        }

    if profile is None:
        if profile_path is not None:
            profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
        else:
            profile = load_profile()
    configured_category, configured_seniority = _profile_scope(profile)
    category = str(category or configured_category).strip()
    seniority = str(seniority or configured_seniority).strip()
    country = str(country or "").strip().upper()

    if not category:
        return _unavailable(country, category, seniority, [], "role category unavailable")

    cache_file = _cache_path(Path(job_dir))
    cached = _read_cached(cache_file, country, category, seniority, cache_hours)
    if cached:
        return cached

    attempts: list[dict] = []
    for scope in SCOPE_ATTEMPTS:
        if scope["country"] and not country:
            continue
        if scope["seniority"] and not seniority:
            continue
        name = scope["name"]
        params = {"category": category}
        if scope["country"]:
            params["country"] = country
        if scope["seniority"]:
            params["seniority"] = seniority
        try:
            response = _fetch_json_path("insights/salary", params=params, auth=False, max_retries=1, timeout=5.0)
            bands = response.get("data") if isinstance(response, dict) else None
            if not isinstance(bands, list):
                attempts.append({"scope": name, "status": "invalid_response"})
                continue
            eligible = [
                band for band in bands if isinstance(band, dict)
                and band.get("p25") is not None
                and int(band.get("sample_size") or 0) >= min_samples
            ]
            attempts.append({"scope": name, "status": "ok", "count": len(bands),
                             "eligible": len(eligible)})
            if not eligible:
                continue
            eligible.sort(key=lambda band: (
                0 if not seniority or band.get("seniority") in (None, "", seniority) else 1,
                -int(band.get("sample_size") or 0),
            ))
            band = eligible[0]
            selected = {
                "status": "available",
                "source": "freehire-salary-insights",
                "scope": name,
                "value": round(float(band["p25"])),
                "currency": str(band.get("currency") or ""),
                "period": str(band.get("period") or ""),
                "p25": float(band["p25"]),
                "p50": float(band["p50"]) if band.get("p50") is not None else None,
                "p75": float(band["p75"]) if band.get("p75") is not None else None,
                "sample_size": int(band.get("sample_size") or 0),
                "country": str(band.get("country") or country),
                "category": str(band.get("category") or category),
                "seniority": str(band.get("seniority") or seniority),
                "fetched_at": datetime.now(UTC).isoformat(),
                "cache": "miss",
            }
            _write_cache(cache_file, country, category, seniority, selected, attempts)
            return selected
        except Exception as exc:  # fail-open: salary research must not block scoring
            attempts.append({"scope": name, "status": "error", "error": type(exc).__name__})
            log.debug("FreeHire salary insights error for %s: %s", name, exc)

    return _unavailable(country, category, seniority, attempts, "no eligible salary band")


def resolve_job_market_salary(job: Mapping, *, profile: Mapping | None = None) -> dict:
    """Resolve and cache market salary from the exact job/scoring context."""
    profile = load_profile() if profile is None else profile
    explicit_category, explicit_seniority = _profile_scope(profile)
    inferred_category, inferred_seniority = _infer_title_scope(job.get("title"))
    posted = _posted_compensation(job) or {}
    key = hashlib.sha256(str(job.get("url") or job.get("application_url") or job.get("title") or "job").encode()).hexdigest()[:24]
    job_dir = config.APP_DIR / "market_salary" / key
    return resolve_market_salary(
        job_dir,
        country=selected_work_country(job) or "",
        category=explicit_category or inferred_category,
        seniority=explicit_seniority or inferred_seniority,
        profile=profile,
        **posted,
    )
