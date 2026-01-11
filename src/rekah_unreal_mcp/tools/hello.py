"""Hello tool for MCP connection testing."""

from mcp.server.fastmcp import FastMCP


def register_hello_tools(mcp: FastMCP):
    """Register hello-related MCP tools for testing."""

    @mcp.tool()
    def say_hello(name: str = "World") -> str:
        """Say hello - a simple tool for testing MCP connection.

        Args:
            name: Name to greet (default: "World")

        Returns:
            A greeting message
        """
        return f"Hello, {name}! MCP connection is working correctly."
