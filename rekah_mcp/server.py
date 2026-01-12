"""MCP server entry point for Unreal Engine development tools."""

from mcp.server.fastmcp import FastMCP

from rekah_mcp.tools.hello import register_hello_tools
from rekah_mcp.tools.lsp_tools import register_lsp_tools

mcp = FastMCP("rekah-unreal")


def main():
    """Main entry point for the MCP server."""
    register_hello_tools(mcp)
    register_lsp_tools(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
