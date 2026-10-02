"""Run the editor-facing streamable HTTP MCP server."""

from app.mcp_server.server import create_server


def main() -> None:
    create_server().run(transport="streamable-http", host="0.0.0.0", port=8001, path="/mcp")


if __name__ == "__main__":
    main()
