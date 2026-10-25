"""Required CloakBrowser runtime, with actionable diagnostics instead of lost causes."""
from __future__ import annotations
from importlib import metadata
from pathlib import Path

REQUIRED = "0.5.6"


class BrowserSetupError(Exception):
    def __init__(self, code, message, *, phase, causes=()):
        from applypilot.runtime_info import runtime_info
        self.details = {"error": message, "code": code, "component": "social_connect", "phase": phase,
                        "causes": list(causes), "runtime": runtime_info(), "details_redacted": True,
                        "credentials_saved": False, "profile_retained": True}
        super().__init__(message)


def failure(exc, phase):
    """Classify privately; never serialize browser stderr, cookies or OAuth URLs."""
    if isinstance(exc, BrowserSetupError):
        return exc
    text = str(exc).lower()
    kind = type(exc).__name__
    if isinstance(exc, (ImportError, metadata.PackageNotFoundError)):
        code, message = "cloakbrowser_dependency_missing", (
            "The running Python environment cannot import the required CloakBrowser runtime. "
            "Launch scripts/Start-ApplyPilot.ps1 from this checkout; it synchronizes dependencies "
            "and checks the same interpreter before opening ApplyPilot. No Chrome fallback was used.")
    elif "license" in text or "license" in kind.lower():
        code, message = "cloakbrowser_license", "CloakBrowser rejected its configured license/channel. No paid license was activated. Review that SDK configuration; the profile was retained."
    elif any(x in text for x in ("checksum", "signature", "sha256", "verification failed")):
        code, message = "cloakbrowser_integrity", "Browser download verification failed. The unverified executable was not accepted. Retry the SDK's verified download; do not disable verification."
    elif any(x in text for x in ("processsingleton", "singletonlock", "user data directory is already in use", "profile in use")):
        code, message = "cloakbrowser_profile_busy", "This managed browser profile is already open. Close its other sign-in window; existing profiles and cookies were not deleted."
    elif any(x in text for x in ("xserver", "missing x server", "display", "headful browser")):
        code, message = "cloakbrowser_no_desktop", "CloakBrowser needs an interactive desktop for manual sign-in. Start the host dashboard in your Windows desktop session, not a headless service."
    elif any(x in text for x in ("certificate", "ssl", "err_cert")):
        code, message = "cloakbrowser_certificate", "TLS verification failed while preparing or navigating the browser. Check the network trust configuration without disabling certificate checks."
    elif any(x in text for x in ("unsupported platform", "unsupported architecture", "exec format", "winerror 193")):
        code, message = "cloakbrowser_platform", "The browser executable is not compatible with this operating system/architecture. No alternate browser was launched."
    elif isinstance(exc, PermissionError) or any(x in text for x in ("access is denied", "permission denied", "winerror 5")):
        code, message = "cloakbrowser_file_access", "The desktop process cannot access the browser executable or profile. Check filesystem permissions or security-software quarantine; the profile was retained."
    elif isinstance(exc, FileNotFoundError) or "executable doesn't exist" in text:
        code, message = "cloakbrowser_executable_missing", "The required browser executable is missing. The desktop launcher prepares the SDK's verified binary before opening the dashboard."
    elif any(x in text for x in ("timeout", "timed out")) or "timeout" in kind.lower():
        code, message = "cloakbrowser_" + phase + "_timeout", "CloakBrowser timed out during " + phase + ". No session was reported saved. The private profile remains available for another manual attempt."
    elif "targetclosed" in kind.lower() or "target page, context or browser has been closed" in text:
        code, message = "cloakbrowser_closed", "CloakBrowser closed before an authenticated social session was captured. Its profile was retained; no successful connection is assumed."
    else:
        code, message = "cloakbrowser_" + phase + "_failed", "CloakBrowser failed during " + phase + ". The result includes its exception type and active runtime identity; the profile was retained and no connection was claimed."
    return BrowserSetupError(code, message, phase=phase, causes=[kind])


def sdk():
    try:
        version = metadata.version("cloakbrowser")
        if version != REQUIRED:
            raise BrowserSetupError("cloakbrowser_version_mismatch", "This interpreter does not have the project's pinned CloakBrowser version. Use scripts/Start-ApplyPilot.ps1 to synchronize the locked environment.", phase="import")
        import cloakbrowser
        if not callable(getattr(cloakbrowser, "launch_persistent_context", None)) or not callable(getattr(cloakbrowser, "ensure_binary", None)):
            raise ImportError("Required CloakBrowser API missing")
        return cloakbrowser
    except Exception as exc:
        raise failure(exc, "import") from None


def prepare(progress=lambda **kw: None):
    module = sdk()
    progress(message="Preparing the required CloakBrowser binary with upstream verification", phase="browser_prepare")
    try:
        path = Path(module.ensure_binary())
        if not path.is_file():
            raise FileNotFoundError("Browser binary missing after preparation")
        return module
    except Exception as exc:
        raise failure(exc, "prepare") from None
