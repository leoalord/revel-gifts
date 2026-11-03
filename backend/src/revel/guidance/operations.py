"""Operation names guidance may mention, split by whether they are live.

Guidance must never present an unimplemented operation as live: the lint test requires every
line that mentions a provisional operation to carry a ``<!-- PROVISIONAL`` marker. Move a name
to the implemented sets only after its owning contract is merged and the operation is deployed.
"""

PROVISIONAL_MARKER = "<!-- PROVISIONAL"

IMPLEMENTED_MCP_TOOLS = frozenset(
    {
        "ping",
        "whoami",
        # F5 #9, merged (PR #25, 763c4e5): docs/contracts/f5-profiles.md
        "get_contribution_policy",
        "add_facts",
        "get_my_profile",
        "set_fact_audience",
        "remove_fact",
        # F5 #28, merged (PR #34): docs/contracts/f5-profiles.md "Shared profile reads"
        "get_profile",
        # F6 #15, merged: revel/mcp/connection_tools.py
        "set_handle",
        "request_connection",
        "list_connection_requests",
        "respond_to_connection_request",
        "list_connections",
        "change_connection_tier",
        "revoke_connection",
        "cancel_connection_request",
        # F7 #19/#20, merged (PR #33): revel/mcp/question_tools.py
        "submit_question",
        "get_updates",
        "claim_assignment",
        "renew_assignment",
        "release_assignment",
        "report_user_contact",
        "decline_assignment",
        "submit_answer",
        "get_question_status",
        "get_answer",
    }
)
IMPLEMENTED_HTTP = frozenset(
    {
        "GET /.well-known/oauth-protected-resource",
        "GET /.well-known/oauth-protected-resource/mcp",
        "GET /guidance",
        "GET /openapi.json",
        # F5 #9, merged
        "GET /api/v1/profile/policy",
        "POST /api/v1/profile/facts",
        "GET /api/v1/profile",
        "GET /api/v1/profile/summary",
        "PATCH /api/v1/profile/facts/{fact_id}/audience",
        "DELETE /api/v1/profile/facts/{fact_id}",
        # F5 #28, merged
        "GET /api/v1/profiles/{handle}",
        # F7 #19/#20, merged (bearer; revel/api/questions.py)
        "POST /api/questions",
        "GET /api/updates",
        "POST /api/assignments/{assignment_id}/claim",
        "POST /api/assignments/{assignment_id}/renew",
        "POST /api/assignments/{assignment_id}/release",
        "POST /api/assignments/{assignment_id}/user-contact",
        "POST /api/assignments/{assignment_id}/decline",
        "POST /api/assignments/{assignment_id}/answer",
        "GET /api/questions/{question_id}",
        "GET /api/questions/{question_id}/answer",
    }
)

# Name -> owning feature, for operations (or error codes) guidance mentions before they are
# merged. Empty since 0.4.2: everything named is merged (F5 #9/#28, F6 #15, F7 #19/#20/#36).
PROVISIONAL_OPERATIONS: dict[str, str] = {}
