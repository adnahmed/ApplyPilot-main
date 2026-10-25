"""BrowserOS native tool inventory.

The connected browser supplies the authoritative schemas and descriptions. This
inventory is only the runtime's fallback list, never a filter on live tools.
Application-specific operations belong to browseros_mcp and browser_controls.
"""

BROWSEROS_TOOLS = frozenset({
    "name_session",
    "tabs",
    "tab_groups",
    "windows",
    "navigate",
    "snapshot",
    "diff",
    "read",
    "grep",
    "wait",
    "act",
    "screenshot",
    "evaluate",
    "run",
    "upload",
    "download",
    "pdf",
    "history",
    "save_skill",
    "mark_skill_run",
})
