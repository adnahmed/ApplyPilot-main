"""Narrow stdio MCP used by browser agents to guard Submit/Send."""

from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP

from applypilot.apply import circuit_breaker
from applypilot.apply.answers import save_generated_answer
from applypilot.apply.guard import (
    attempt_status,
    mark_side_effect_intent as guard_mark_intent,
    mark_submitted as guard_mark_submitted,
    terminal_result,
)


def build_server(attempt_id: str) -> FastMCP:
    from applypilot.reasoning.learning_store import tool_catalog_lifespan

    server = FastMCP("ApplyPilot Submission Guard",
                     lifespan=lambda app: tool_catalog_lifespan(app, "applypilot_control"))

    def recorded_terminal() -> dict | None:
        return terminal_result(attempt_status(attempt_id))

    @server.tool()
    def status() -> dict:
        """Return the persisted reservation state."""
        return attempt_status(attempt_id)

    @server.tool()
    async def submit_email_application(route_id: str) -> dict:
        """Send the captured application email once with its reservation-bound documents.

        Use the route_id returned by inspect_application_route/open_application_page.
        A committed result proves application delivery; uncertain sends are only reconciled.
        """
        if terminal := recorded_terminal():
            return terminal
        from applypilot.apply.email_application import submit_email_application as submit
        return await submit(attempt_id, route_id)

    @server.tool()
    def mark_side_effect_intent() -> dict:
        """Call only after required answers/uploads are complete, immediately before final Submit/Send."""
        attempt = attempt_status(attempt_id)
        if terminal := terminal_result(attempt):
            return terminal
        if attempt["channel"] == "email":
            raise RuntimeError("Use submit_email_application(route_id); the email adapter owns Send intent.")
        if attempt["browser_runtime"] == "browseros":
            raise RuntimeError("Use submit_application(page, ref); the owning adapter records intent and clicks once.")
        domain = circuit_breaker.domain_from_target(attempt["target"])
        if domain and (breaker := circuit_breaker.target_status(attempt["target"]))["open"]:
            raise RuntimeError(f"domain circuit open: {breaker.get('reason')}")
        return guard_mark_intent(attempt_id)

    @server.tool()
    def mark_submitted(proof_kind: str, proof: str) -> dict:
        """Commit only after visible, explicit submission confirmation."""
        attempt = attempt_status(attempt_id)
        if attempt["browser_runtime"] == "browseros" and attempt["channel"] != "email":
            from applypilot.apply.submission_evidence import KIND
            if proof_kind != KIND:
                raise RuntimeError("Use capture_submission(page) to capture the browser confirmation. Agent prose is not submission proof.")
        if terminal := recorded_terminal():
            return terminal
        attempt = guard_mark_submitted(
            attempt_id,
            proof_kind=proof_kind,
            proof=proof,
        )
        return attempt

    @server.tool()
    def record_application_observation(detail: str) -> dict:
        """Record the observed page/outcome; existing submission evidence decides what can be retried."""
        from applypilot.apply.guard import defer_application
        attempt = attempt_status(attempt_id)
        if terminal := terminal_result(attempt):
            return terminal
        attempt = defer_application(attempt_id, detail, detail=detail)
        return terminal_result(attempt) or attempt


    @server.tool()
    def save_answer(question: str, answer: str) -> dict:
        """Persist a generated required-field answer with non-canonical provenance."""
        attempt = attempt_status(attempt_id)
        if terminal := terminal_result(attempt):
            return terminal
        return save_generated_answer(attempt["job_url"], question, answer)

    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt-id", "--reservation-id", dest="attempt_id", required=True)
    args = parser.parse_args()
    server = build_server(args.attempt_id)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
