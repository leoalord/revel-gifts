"""Operation names guidance may mention, split by whether they are live.

Guidance must never present an unimplemented operation as live: the lint test requires every
line that mentions a provisional operation to carry a ``<!-- PROVISIONAL`` marker. Move a name
to the implemented sets only after its owning contract is merged and the operation is deployed.
"""

PROVISIONAL_MARKER = "<!-- PROVISIONAL"

IMPLEMENTED_MCP_TOOLS = frozenset({"ping", "whoami"})
IMPLEMENTED_HTTP = frozenset(
    {
        "GET /.well-known/oauth-protected-resource",
        "GET /.well-known/oauth-protected-resource/mcp",
        "GET /guidance",
        "GET /openapi.json",
    }
)

# Operation -> owning feature. None of these are merged yet.
PROVISIONAL_OPERATIONS: dict[str, str] = {
    # F5 #9: docs/contracts/f5-profiles.md v1 (provisional)
    "get_contribution_policy": "F5",
    "add_facts": "F5",
    "get_my_profile": "F5",
    "set_fact_audience": "F5",
    "remove_fact": "F5",
    "GET /api/v1/profile/policy": "F5",
    "POST /api/v1/profile/facts": "F5",
    "GET /api/v1/profile": "F5",
    "GET /api/v1/profile/summary": "F5",
    "PATCH /api/v1/profile/facts/{id}/audience": "F5",
    "DELETE /api/v1/profile/facts/{id}": "F5",
    # F5 follow-up (get_profile) and F6 #15 (PR #23 @ 93da408, in review)
    "get_profile": "F5/F6",
    "set_handle": "F6",
    "request_connection": "F6",
    "list_connection_requests": "F6",
    "respond_to_connection_request": "F6",
    "list_connections": "F6",
    "change_connection_tier": "F6",
    "revoke_connection": "F6",
    "cancel_connection_request": "F6",
    # F7 #19/#20 (no contract published yet; names agreed with the F7 orchestrator, may change)
    "submit_question": "F7",
    "get_answer": "F7",
    "get_updates": "F7",
    "claim_assignment": "F7",
    "renew_assignment": "F7",
    "release_assignment": "F7",
    "decline_assignment": "F7",
    "report_user_contact": "F7",
    "submit_answer": "F7",
}
