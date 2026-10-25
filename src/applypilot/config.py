"""ApplyPilot configuration: paths, platform detection, user data."""

import os
import platform
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

# User data directory — all user-specific files live here
APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot")).expanduser().resolve()

# Core paths
DB_PATH = APP_DIR / "main.pgstore"
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
ENV_PATH = APP_DIR / ".env"
IMPACT_LEDGER_PATH = APP_DIR / "impact_ledger.json"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
TRACKING_DIR = APP_DIR / "tracking"
LOG_DIR = APP_DIR / "logs"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"
SESSIONS_DIR = APP_DIR / "chrome-sessions"

# Optional documents (profile photo, certs, ID, etc.)
FILES_DIR = APP_DIR / "files"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"
LOADED_CONFIG_PATHS: set[str] = set()


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Prefers the platform-specific Chrome for Testing installed under APP_DIR.
    The fallback extension worker requires a build that permits --load-extension.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).expanduser().is_file():
        return str(Path(env_path).expanduser())

    system = platform.system()
    cft_root = APP_DIR / "chrome-for-testing"
    if system == "Windows":
        cft_candidates = [cft_root / "chrome-win64/chrome.exe", cft_root / "chrome-win32/chrome.exe"]
    elif system == "Darwin":
        arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        cft_candidates = [cft_root / f"chrome-mac-{arch}/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"]
    else:
        cft_candidates = [cft_root / "chrome-linux64/chrome"]
    for candidate in cft_candidates:
        if candidate.is_file():
            return str(candidate)

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.is_file():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def ensure_dirs():
    """Create all required directories."""
    for d in [APP_DIR, TAILORED_DIR, COVER_LETTER_DIR, TRACKING_DIR, LOG_DIR, CHROME_WORKER_DIR, APPLY_WORKER_DIR, SESSIONS_DIR, FILES_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_profile() -> dict:
    """Load user profile from ~/.applypilot/profile.json."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `applypilot init` first."
        )
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8-sig"))


def _load_yaml_mapping(path: Path, what: str) -> dict:
    """Load a YAML file and guarantee a dict mapping (never None / non-mapping).

    Raises a useful path/key error instead of returning ``None`` or a scalar so
    malformed config fails loudly at the point of use.
    """
    import yaml
    if not path.exists():
        raise FileNotFoundError(f"{what} not found at {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed YAML in {path} ({what}): {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(
            f"{what} at {path} must be a mapping at the top level, got {type(data).__name__}"
        )
    LOADED_CONFIG_PATHS.add(str(path.resolve()))
    return data


def load_search_config() -> dict:
    """Read search configuration; preference files use planned discovery/admission."""
    if SEARCH_CONFIG_PATH.exists():
        data = _load_yaml_mapping(SEARCH_CONFIG_PATH, "search config")
    else:
        example = CONFIG_DIR / "searches.example.yaml"
        data = _load_yaml_mapping(example, "search config example") if example.exists() else {}
    if "preferences" in data:
        from applypilot.discovery.intent import compile_search_intent

        compile_search_intent(data)
    return data


def load_compiled_intent(raw_config: dict | None = None, *, profile: Mapping | None = None):
    """Compile the search config into a frozen :class:`SearchIntent`.

    This is the single entry point callers use to freeze the intent + applicant
    snapshot for a run.  Tasks must never re-read the mutable profile/config
    separately. An explicit profile mapping avoids profile file access, including
    when empty. Search config is never authoritative for applicant facts.
    """
    from applypilot.discovery.intent import compile_search_intent
    from applypilot.profile import build_applicant_snapshot

    if raw_config is None:
        raw_config = load_search_config()
    intent = compile_search_intent(raw_config)
    snapshot = build_applicant_snapshot(load_profile() if profile is None else profile)
    return intent, snapshot


_DISCOVERY_RUNTIME_PATH = APP_DIR / "discovery_runtime.yaml"


def load_discovery_runtime(
    raw_config: dict | None = None, *, legacy_defaults: Mapping | None = None,
) -> dict:
    """Load the operator/runtime policy file (separate from search intent).

    Returns validated defaults without rewriting the file. Explicit mappings are
    authoritative; file-backed loads also honor the legacy FreeHire opt-out.
    """
    # Explicit mappings are complete inputs, including {}; never reread a file
    # behind an already compiled cycle. Malformed policy must fail closed.
    data = raw_config if raw_config is not None else (
        _load_yaml_mapping(_DISCOVERY_RUNTIME_PATH, "discovery runtime")
        if _DISCOVERY_RUNTIME_PATH.exists() else {}
    )
    policy = validate_discovery_runtime(data, legacy_defaults=legacy_defaults)
    if raw_config is None and os.environ.get("FREEHIRE_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        policy["authorized_sources"]["freehire"] = False
    return policy


def read_discovery_config(path: Path | None = None) -> dict:
    """Read-only input for the planner; never rewrites or initializes app-home."""
    if path is not None:
        return _load_yaml_mapping(Path(path), "search config")
    selected = SEARCH_CONFIG_PATH if SEARCH_CONFIG_PATH.exists() else CONFIG_DIR / "searches.example.yaml"
    return _load_yaml_mapping(selected, "search config") if selected.exists() else {}


def validate_discovery_runtime(data: Mapping, *, legacy_defaults: Mapping | None = None) -> dict:
    """Validate workload caps (-1 = uncapped) and explicit access grants without IO.

    Request defaults are lowered only by providers supporting those inputs.
    In particular, legacy defaults.location is never a global index country.
    Authorization keys are exact providers or jobspy:<board>; values must be
    booleans. Credential availability is a separate adapter preflight concern.
    """
    import math
    from copy import deepcopy
    from applypilot.discovery.capabilities import CAPABILITIES, JOBSPY_BOARDS
    from applypilot.limits import work_limit

    if not isinstance(data, Mapping):
        raise ValueError("discovery runtime must be a mapping")
    data = dict(data)
    # Preserve saved resource pauses as workload policy; legacy worker owns concurrency.
    for legacy, budget in (("worker_ceiling", "request_budget"),
                           ("discovery_http_slots", "http_budget"),
                           ("discovery_browser_slots", "browser_budget"),
                           ("discovery_browser_operations", "browser_budget"),
                           ("discovery_model_slots", "model_budget")):
        if data.pop(legacy, None) == 0:
            data[budget] = 0
    for legacy in ("capacity", "application_browser_slots", "application_browser_operations"):
        data.pop(legacy, None)
    unknown = set(data) - set(_DEFAULT_RUNTIME)
    if unknown:
        raise ValueError(f"Unknown discovery runtime keys: {sorted(unknown)}")
    result = deepcopy(_DEFAULT_RUNTIME)
    result.update(data)
    for key in ("retry_attempts", "exploration_allocations"):
        value = result[key]
        if type(value) is not int or value < 0:
            raise ValueError(f"runtime.{key} must be a nonnegative integer")
    for key in ("evidence_budget", "request_budget", "http_budget", "browser_budget",
                "model_budget", "probe_limit", "max_pages_per_task"):
        work_limit(result[key], f"runtime.{key}")
    for key in ("freshness_seconds", "lease_seconds", "retry_base_seconds",
                "cooldown_seconds", "paid_budget", "paid_request_cost"):
        value = result[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"runtime.{key} must be finite and nonnegative")
    if result["lease_seconds"] <= 0:
        raise ValueError("runtime.lease_seconds must be positive")
    if result["rollout_mode"] not in ("shadow", "live"):
        raise ValueError("runtime.rollout_mode must be shadow or live")
    buffer = result["buffer"]
    defaults = _DEFAULT_RUNTIME["buffer"]
    obsolete = {"low_watermark", "high_watermark", "ready_target"}
    if not isinstance(buffer, Mapping) or set(buffer) - set(defaults) - obsolete:
        raise ValueError("runtime.buffer contains unknown settings")
    # Search Reset used global discovery watermarks here. Keep accepting old
    # files during migration, but do not let downstream stock pause discovery.
    buffer = {**defaults, **{key: value for key, value in buffer.items() if key not in obsolete}}
    age = buffer["revalidate_after_days"]
    if type(age) is not int or not 1 <= age <= 90:
        raise ValueError("runtime.buffer.revalidate_after_days must be within 1..90")
    result["buffer"] = buffer
    from applypilot.backlog import validate_backlog
    result["flow_control"] = validate_backlog(result["flow_control"])
    grants = result["authorized_sources"]
    known = set(CAPABILITIES) | {f"jobspy:{board}" for board in JOBSPY_BOARDS}
    if not isinstance(grants, Mapping) or any(k not in known or type(v) is not bool for k, v in grants.items()):
        raise ValueError("runtime.authorized_sources must map known source keys to explicit booleans")
    result["authorized_sources"] = dict(grants)
    for key in ("inventory_files", "native_targets"):
        if not isinstance(result[key], Mapping):
            raise ValueError(f"runtime.{key} must be a mapping")
        values = {}
        for source, items in result[key].items():
            if source not in known or not isinstance(items, (list, tuple)) or any(not isinstance(v, str) or not v.strip() for v in items):
                raise ValueError(f"runtime.{key} requires known sources and lists of nonempty strings")
            values[source] = list(items)
        result[key] = values
    request = {"distance": 100, "hours_old": 72, "results_per_site": -1}
    if legacy_defaults is not None:
        if not isinstance(legacy_defaults, Mapping):
            raise ValueError("search defaults must be a mapping")
        request.update({k: legacy_defaults[k] for k in request if k in legacy_defaults})
    supplied = data.get("request_defaults", {})
    if not isinstance(supplied, Mapping) or set(supplied) - set(request):
        raise ValueError("runtime.request_defaults supports distance, hours_old, results_per_site only")
    request.update(supplied)
    for key, value in request.items():
        if key == "results_per_site":
            work_limit(value, "runtime.request_defaults.results_per_site")
            continue
        if type(value) is not int or value < 0:
            raise ValueError(f"runtime.request_defaults.{key} must be a nonnegative integer")
    result["request_defaults"] = request
    admission = result["admission"]
    from applypilot.discovery.admission_policy import POLICY_KEYS
    allowed = POLICY_KEYS
    if (not isinstance(admission, Mapping) or set(admission) - allowed
            or any(type(value) is not bool for value in admission.values())):
        raise ValueError("runtime.admission requires explicit booleans for supported allocation policies")
    result["admission"] = {key: admission.get(key, False) for key in sorted(allowed)}
    inputs = result["source_inputs"]
    if not isinstance(inputs, Mapping) or any(key not in known or not isinstance(value, Mapping)
                                              for key, value in inputs.items()):
        raise ValueError("runtime.source_inputs must map known source names to input settings")

    def input_value(value, key=""):
        # Runtime snapshots are persisted for reproducibility; store credential
        # environment-variable NAMES, never secrets in this input configuration.
        if any(part in key.lower() for part in ("password", "secret", "token", "credential")) and not key.endswith("_env"):
            raise ValueError("source_inputs credentials must reference an environment variable")
        if key.endswith("_env") and (
            not isinstance(value, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None
        ):
            raise ValueError("source_inputs *_env values must be environment-variable names")
        if isinstance(value, Mapping):
            return {str(k): input_value(v, str(k)) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [input_value(v, key) for v in value]
        if value is None or type(value) in (str, bool, int):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        raise ValueError("source_inputs values must be finite JSON data")

    # Merge defaults per source, not by replacing the entire portfolio. Explicit
    # zero/false values are never interpreted as missing settings.
    result["source_inputs"] = {
        name: {**_DEFAULT_RUNTIME["source_inputs"].get(name, {}), **input_value(value)}
        for name, value in {**_DEFAULT_RUNTIME["source_inputs"], **inputs}.items()
    }
    for name in ("web_search", "twitter", "linkedin_posts"):
        work_limit(result["source_inputs"][name].get("limit", -1), f"source_inputs.{name}.limit")
    research = result["research"]
    if not isinstance(research, Mapping) or set(research) - {"enabled", "companies", "workers"}:
        raise ValueError("runtime.research supports enabled and companies")
    research = dict(research)
    # Preserve a saved zero-worker pause while retiring the separate worker pool.
    if research.pop("workers", None) == 0:
        research["enabled"] = False
    research = {**_DEFAULT_RUNTIME["research"], **research}
    if type(research["enabled"]) is not bool:
        raise ValueError("runtime.research.enabled must be a boolean")
    work_limit(research["companies"], "runtime.research.companies")
    result["research"] = research
    recovery = result["recovery"]
    if not isinstance(recovery, Mapping) or set(recovery) - set(_DEFAULT_RUNTIME["recovery"]):
        raise ValueError("runtime.recovery has unsupported settings")
    recovery = {**_DEFAULT_RUNTIME["recovery"], **recovery}
    if type(recovery["enabled"]) is not bool:
        raise ValueError("recovery.enabled must be a boolean")
    for key in ("max_attempts_per_task", "timeout_seconds"):
        if type(recovery[key]) is not int or recovery[key] < 0:
            raise ValueError(f"recovery.{key} must be a nonnegative integer")
    if recovery["allowed_providers"] is not None:
        if (not isinstance(recovery["allowed_providers"], (tuple, list))
                or any(v not in known for v in recovery["allowed_providers"])):
            raise ValueError("recovery.allowed_providers must be a list or null")
        recovery["allowed_providers"] = list(recovery["allowed_providers"])
    result["recovery"] = recovery
    if recovery["enabled"] and recovery["timeout_seconds"] >= result["lease_seconds"]:
        raise ValueError("recovery.timeout_seconds must be shorter than the task lease")
    return result




_DEFAULT_RUNTIME: dict = {
    "flow_control": {},
    "evidence_budget": -1,
    "freshness_seconds": 3600,
    "buffer": {"revalidate_after_days": 7},
    "lease_seconds": 300,
    "retry_attempts": 3,
    "rollout_mode": "live",
    "exploration_allocations": 10,
    "request_budget": -1,
    "http_budget": -1,
    "browser_budget": -1,
    "model_budget": -1,
    "paid_budget": 0.0,
    "paid_request_cost": 0.0,
    "retry_base_seconds": 60,
    "cooldown_seconds": 900,
    "probe_limit": -1,
    "max_pages_per_task": -1,
    "authorized_sources": {},
    "inventory_files": {},
    "native_targets": {},
    "request_defaults": {},
    "admission": {},
    "source_inputs": {
        "web_search": {"mode": "search", "backend": "auto", "limit": -1},
        "twitter": {"mode": "auto", "limit": -1},
        "linkedin_posts": {"mode": "auto", "limit": -1},
    },
    "research": {"enabled": True, "companies": -1},
    "recovery": {"enabled": True, "max_attempts_per_task": 1, "timeout_seconds": 45,
                 "allowed_providers": None},
}


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_synonyms_config() -> dict:
    """Load synonyms.yaml (alias tables for discovery sources)."""
    import yaml
    path = CONFIG_DIR / "synonyms.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def load_no_signup_domains() -> list[str]:
    """Load no-signup domains from sites.yaml.

    These are major job boards / ATS platforms where the agent must
    NEVER create accounts (ban risk, pointless, or session-based).
    """
    cfg = load_sites_config()
    return cfg.get("no_signup_domains", [])


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 8,                           # was 7; per 2026-04-23 funnel spec
    "max_job_age_days": 14,                   # stale-job cutoff (discovered_at)
    "max_in_flight_per_company": -1,          # unlimited unless explicitly configured
    "in_flight_window_days": 30,              # window for in-flight count
    "max_tailored_per_company": -1,           # cap per company at tailor stage (-1 = unlimited)
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}


_LOADED_ENV_VALUES = {}

def load_env():
    """Load environment variables from ~/.applypilot/.env if it exists."""
    from dotenv import load_dotenv
    before = dict(os.environ)
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, encoding="utf-8-sig", interpolate=False)
        LOADED_CONFIG_PATHS.add(str(ENV_PATH.resolve()))
    # find_dotenv() searches from the caller module, not necessarily CWD.
    # Be explicit so source checkouts and installed wheels behave alike.
    # Real process/shell values still win; workspace settings precede CWD.
    cwd_env = Path.cwd() / ".env"
    if cwd_env.resolve() != ENV_PATH.resolve() and cwd_env.is_file():
        load_dotenv(cwd_env, encoding="utf-8-sig", interpolate=False)
        LOADED_CONFIG_PATHS.add(str(cwd_env.resolve()))
    _LOADED_ENV_VALUES.update({k: v for k, v in os.environ.items() if k not in before})


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run", "track"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + browser and Claude CLI
    """
    load_env()

    has_llm = any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "LLM_URL"))
    if not has_llm:
        return 1

    has_claude = shutil.which("claude") is not None
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    runtime = os.environ.get("APPLYPILOT_BROWSER_RUNTIME", "auto").strip().lower()
    has_browseros_config = bool(
        os.environ.get("BROWSEROS_MCP_URL", "http://127.0.0.1:9012/mcp").strip()
    ) and runtime != "playwright"

    if has_claude and (has_chrome or has_browseros_config):
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "LLM_URL")):
        missing.append("LLM API key — run [bold]applypilot init[/bold] or set GEMINI_API_KEY")
    if required >= 3:
        runtime = os.environ.get("APPLYPILOT_BROWSER_RUNTIME", "auto").strip().lower()
        if not shutil.which("claude"):
            missing.append("Claude Code CLI — install from [bold]https://claude.ai/code[/bold]")
        if runtime == "playwright":
            try:
                get_chrome_path()
            except FileNotFoundError:
                missing.append("Chrome/Chromium — install or set CHROME_PATH")
        elif not os.environ.get("BROWSEROS_MCP_URL", "http://127.0.0.1:9012/mcp").strip():
            missing.append("BrowserOS Neo MCP URL or Chrome/Chromium fallback")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Per-company application limits (open-pipeline cap)
# ---------------------------------------------------------------------------

COMPANY_LIMITS_PATH_NAME = "company_limits.yaml"

_company_limits_cache: dict | None = None


def _load_company_limits() -> dict:
    """Load and cache ~/.applypilot/company_limits.yaml.

    Returns an empty dict if the file doesn't exist or fails to parse.
    Cache can be reset by setting `_company_limits_cache = None`.
    """
    global _company_limits_cache
    if _company_limits_cache is not None:
        return _company_limits_cache

    path = APP_DIR / COMPANY_LIMITS_PATH_NAME
    if not path.exists():
        _company_limits_cache = {}
        return _company_limits_cache

    import logging
    import yaml

    log = logging.getLogger(__name__)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"expected mapping, got {type(data).__name__}")
        overrides = data.get("overrides", {}) or {}
        if isinstance(overrides, dict):
            data["overrides"] = {str(k).lower(): (v or {}) for k, v in overrides.items()}
        _company_limits_cache = data
    except Exception as e:
        log.warning("Failed to parse %s (%s); using defaults.", path, e)
        _company_limits_cache = {}

    return _company_limits_cache


def get_company_limit(company: str) -> tuple[int, int]:
    """Return (max_in_flight, window_days) for the given company.

    Resolution order:
      1. overrides.<company-lowercased> (YAML)
      2. defaults.* (YAML)
      3. DEFAULTS["max_in_flight_per_company"] / ["in_flight_window_days"]

    If a per-company entry omits either key, the missing value falls back
    to the YAML defaults block (or DEFAULTS if not specified there).

    A cap of -1 means "unlimited". A cap of 0 means "explicitly blocked".
    Both are passed through as-is; interpretation lives in the caller.
    """
    limits = _load_company_limits()

    yaml_defaults = limits.get("defaults", {}) or {}
    default_cap = yaml_defaults.get("max_in_flight", DEFAULTS["max_in_flight_per_company"])
    default_window = yaml_defaults.get("window_days", DEFAULTS["in_flight_window_days"])

    overrides = limits.get("overrides", {}) or {}
    co = (company or "").lower().strip()
    if co:
        if co in overrides:
            entry = overrides[co] or {}
            cap = entry.get("max_in_flight", default_cap)
            window = entry.get("window_days", default_window)
            return int(cap), int(window)

    return int(default_cap), int(default_window)
