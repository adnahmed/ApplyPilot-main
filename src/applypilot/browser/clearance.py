"""Clearance solution data types for Cloudflare/bot-challenge bypass."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ClearanceCookie:
    """A single cookie returned by the challenge solver."""

    name: str
    value: str = field(repr=False)
    domain: str
    path: str = "/"
    secure: bool = True
    http_only: bool = True
    same_site: str | None = None
    expires: float = -1  # Unix timestamp; -1 = session cookie

    def to_browser_os_dict(self) -> dict:
        """Format for BrowserOS ``Storage.setCookies`` CDP call."""
        d: dict = {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "secure": self.secure,
            "httpOnly": self.http_only,
        }
        if self.same_site:
            d["sameSite"] = self.same_site
        if self.expires > 0:
            d["expires"] = self.expires
        return d

    def to_playwright_dict(self) -> dict:
        """Format for Playwright ``context.addCookies()``."""
        d: dict = {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "secure": self.secure,
            "httpOnly": self.http_only,
        }
        if self.same_site:
            d["sameSite"] = self.same_site
        if self.expires > 0:
            d["expires"] = self.expires
        return d


@dataclass(frozen=True, slots=True)
class ClearanceSolution:
    """Result of a successful Cloudflare/bot-challenge solve.

    Carries everything the browser adapters need to apply the clearance
    without the LLM ever seeing raw cookie values.
    """

    cookies: list[ClearanceCookie] = field(default_factory=list, repr=False)
    user_agent: str = ""
    # Populated only when the solver exposes real UA Client Hint metadata.
    # Never fabricate — None means "use legacy UA string only".
    user_agent_metadata: dict | None = None
    final_url: str = ""
    solver_session_id: str | None = None
    html: str = field(default="", repr=False)
    status: int | None = None
