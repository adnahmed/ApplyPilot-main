# Browser Automation Setup

Standard sequence for skills that use BrowserOS neo MCP tools to fetch web pages.

## Tab Setup

```
1. open_application_page() → open or recover the assigned owned page
2. retain numeric page_id; ApplyPilot supplies the session handle
3. snapshot(page=page_id) → inspect current refs
4. read(page=page_id) → extract content
Read-only tasks: use tabs(action="new", url=job.application_url), then snapshot/read.
```

## Context Window Safety

**Avoid `read` on large or dynamic pages** (job boards, search results, listing pages, dashboards). It returns the entire page and can blow out the context window, making the conversation unrecoverable.

Instead, use targeted extraction:
- `evaluate` with a selector to extract only the content you need
- `snapshot` to get structured element refs
- `read` is safe only for simple pages with a single article/posting

## Error Handling

- If `tabs(action="list")` returns no tabs or an error, ask the user to confirm Chrome is open with the BrowserOS neo extension active.
- If `navigate` fails or the page doesn't load, ask the user to paste the content directly.
- If `read` returns empty or unusable content, try `snapshot` as a fallback, then ask the user to paste if that also fails.
- Do not retry a failing page more than once. Move on and ask the user for the content.
