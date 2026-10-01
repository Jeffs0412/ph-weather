"""Philippines weather MCP server."""

from weather.server import mcp, run


def main() -> None:
    run()


__all__ = ["main", "mcp"]
