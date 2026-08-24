"""Entry point. Phase 1 will wire FastMCP server + tool registration here.

Planned shape (do not implement yet — see SPEC.md):

    def main() -> None:
        # parse args: --dsn, --read-only, --approval-mode, --audit-log PATH
        # build ConnectionManager(dsn)
        # register tools per docs/TOOLS.md
        # server.run(transport="stdio")
"""
