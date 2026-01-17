"""MCP server entry point for Unreal Engine development tools"""

from mcp.server.fastmcp import FastMCP

from rekah_mcp.tools.tools_utils import register_lsp_tools

mcp = FastMCP("rekah-unreal")


def main():
    """main entry point for the MCP server"""
    register_lsp_tools(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
