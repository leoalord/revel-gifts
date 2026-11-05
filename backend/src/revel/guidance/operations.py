"""Operation names guidance may mention, split by whether they are live.

Guidance must never present an unimplemented operation as live: the lint test requires every
line that mentions a provisional operation to carry a ``<!-- PROVISIONAL`` marker. Move a name
to the implemented sets only after its owning contract is merged and the operation is deployed.
"""

PROVISIONAL_MARKER = "<!-- PROVISIONAL"

IMPLEMENTED_MCP_TOOLS = frozenset(
    {
        "ping",
        "get_setup",
        "configure_inbox_checks",
        "whoami",
        # F5 #9/#28/#29, consolidated by RG-59: revel/mcp/profile_tools.py
        "get_profile",
        "add_facts",
        "update_fact",
        "remove_fact",
        "set_contribution_policy",
        # F6 #15, consolidated by RG-59: revel/mcp/connection_tools.py
        "set_handle",
        "request_connection",
        "list_connections",
        "respond_to_connection_request",
        "end_connection",
        # F7 #19/#20 (PR #33), consolidated by RG-59: revel/mcp/question_tools.py
        "submit_question",
        "get_updates",
        "update_assignment",
        "submit_answer",
        "get_question",
    }
)

# RG-59 (Build Task #31) tool consolidation: names no longer registered. Served guidance must
# not mention them, except CHANGELOG entries for versions before RETIRED_IN_VERSION.
RETIRED_IN_VERSION = "0.6.0"
RETIRED_QUESTION_TOOLS = frozenset(
    {
        # -> update_assignment (actions claim, renew, release, report_user_contact, decline)
        "claim_assignment",
        "renew_assignment",
        "release_assignment",
        "report_user_contact",
        "decline_assignment",
        # -> get_question
        "get_question_status",
        "get_answer",
    }
)
RETIRED_PROFILE_TOOLS = frozenset(
    {
        "get_my_profile",  # -> get_profile (no handle)
        "get_contribution_policy",  # -> get_profile (no handle), its policy
        "set_fact_audience",  # -> update_fact (action set_audience)
        "review_pending_fact",  # -> update_fact (action approve); reject -> remove_fact
    }
)
RETIRED_CONNECTION_TOOLS = frozenset(
    {
        "list_connection_requests",  # -> list_connections (include requests)
        "change_connection_tier",  # -> respond_to_connection_request (action change_tier)
        "revoke_connection",  # -> end_connection (action revoke)
        "cancel_connection_request",  # -> end_connection (action cancel)
    }
)
RETIRED_MCP_TOOLS = RETIRED_QUESTION_TOOLS | RETIRED_PROFILE_TOOLS | RETIRED_CONNECTION_TOOLS

IMPLEMENTED_HTTP = frozenset(
    {
        "GET /.well-known/oauth-protected-resource",
        "GET /.well-known/oauth-protected-resource/mcp",
        "GET /guidance",
        "GET /api/v1/setup",
        "PUT /api/v1/setup",
        "POST /api/v1/setup/host-task",
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
        # F5 #29
        "PUT /api/v1/profile/policy",
        "POST /api/v1/profile/facts/{fact_id}/approve",
        "POST /api/v1/profile/facts/{fact_id}/reject",
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

# RG-23/RG-43 skill renames (0.7.0): old skill name -> new name. The old website pages
# permanently redirect (308) to the new ones (``revel.guidance.web``); the old MCP prompt names
# and ``revel://guidance/<old>`` resources are gone (no aliases). Served guidance must not name
# the old skills, except CHANGELOG entries for versions before RENAMED_IN_VERSION.
RENAMED_IN_VERSION = "0.7.0"
RENAMED_SKILLS: dict[str, str] = {
    "revel-profile-discovery": "revel-manage-profile",
    "revel-giver-workflow": "revel-find-a-gift",
}
