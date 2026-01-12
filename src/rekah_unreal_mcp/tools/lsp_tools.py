"""LSP-related MCP tools for Unreal Engine C++ development.

This module re-implements Claude Code's built-in LSP Tool functionality as MCP tools,
working around the textDocument/didOpen bug by managing clangd directly.

Tools (11 total):
- Setup: setup_lsp, lsp_status
- P0: goToDefinition, findReferences, hover
- P1: documentSymbol, workspaceSymbol, goToImplementation
- P2: prepareCallHierarchy, incomingCalls, outgoingCalls
"""

from mcp.server.fastmcp import FastMCP
from pathlib import Path
from typing import Optional
import shutil
import json

# Global LSP client instance (None until setup_lsp is called)
_lsp_client: Optional["LSPClient"] = None
_lsp_setup_error: Optional[str] = None


def get_lsp_client():
    """Get LSP client singleton. Returns None if not setup."""
    return _lsp_client


def _check_lsp_setup() -> Optional[str]:
    """Check if LSP is properly setup. Returns error message if not."""
    if _lsp_client is None:
        return (
            "⚠️ LSP not initialized!\n"
            "Please call 'setup_lsp' tool first with your Unreal Engine project directory.\n"
            "Example: setup_lsp(project_dir=\"D:/MyUnrealProject\")"
        )
    if _lsp_setup_error:
        return f"⚠️ LSP setup failed: {_lsp_setup_error}"
    return None


async def _ensure_client_ready() -> Optional[str]:
    """Ensure LSP client is setup and clangd is running.

    Returns:
        None if ready, error message string if not ready.
    """
    # Check setup first
    setup_error = _check_lsp_setup()
    if setup_error:
        return setup_error

    # Start clangd if not running
    if _lsp_client.process is None:
        try:
            await _lsp_client.start()
        except Exception as e:
            return f"⚠️ Failed to start clangd: {e}"

    return None


def register_lsp_tools(mcp: FastMCP):
    """Register all LSP-related MCP tools (11 tools total)."""

    # ═══════════════════════════════════════════════════════════════════
    # Setup Tools (MUST call setup_lsp first)
    # ═══════════════════════════════════════════════════════════════════

    @mcp.tool()
    async def setup_lsp(
        project_dir: str,
        compile_commands_dir: Optional[str] = None,
    ) -> str:
        """Initialize LSP client for Unreal Engine C++ development.

        MUST be called before using any other LSP tools.

        Args:
            project_dir: Absolute path to Unreal Engine project root
                         (where .uproject file is located)
            compile_commands_dir: Optional path to compile_commands.json directory.
                                  Defaults to project_dir if not specified.

        Returns:
            Setup status message

        Example:
            setup_lsp(project_dir="D:/BttUnrealEngine")
        """
        global _lsp_client, _lsp_setup_error

        from rekah_unreal_mcp.lsp.client import LSPClient

        project_path = Path(project_dir)

        # Validate project directory
        if not project_path.exists():
            _lsp_setup_error = f"Project directory not found: {project_dir}"
            return f"❌ {_lsp_setup_error}"

        # Check for compile_commands.json
        cc_dir = Path(compile_commands_dir) if compile_commands_dir else project_path
        cc_path = cc_dir / "compile_commands.json"

        if not cc_path.exists():
            _lsp_setup_error = f"compile_commands.json not found at: {cc_path}"
            return (
                f"❌ {_lsp_setup_error}\n\n"
                "To generate compile_commands.json, run:\n"
                "  UnrealBuildTool -Mode=GenerateClangDatabase -Project=YourProject.uproject\n"
                "Or use the Unreal Editor: Tools > Generate Clang Database"
            )

        # Check clangd availability
        if not shutil.which("clangd"):
            _lsp_setup_error = "clangd not found in PATH"
            return (
                f"❌ {_lsp_setup_error}\n\n"
                "Please install clangd:\n"
                "  - Windows: choco install llvm or download from https://releases.llvm.org/\n"
                "  - Verify: clangd --version"
            )

        # Stop existing client if any
        if _lsp_client is not None:
            try:
                await _lsp_client.stop()
            except Exception:
                pass

        # Create new LSP client
        try:
            _lsp_client = LSPClient(
                project_dir=str(project_path),
                compile_commands_dir=str(cc_dir),
            )
            _lsp_setup_error = None

            return (
                f"✅ LSP initialized successfully!\n"
                f"  Project: {project_dir}\n"
                f"  compile_commands.json: {cc_path}\n"
                f"\n"
                f"You can now use LSP tools:\n"
                f"  - goToDefinition, findReferences, hover\n"
                f"  - documentSymbol, workspaceSymbol, goToImplementation\n"
                f"  - prepareCallHierarchy, incomingCalls, outgoingCalls"
            )
        except Exception as e:
            _lsp_setup_error = str(e)
            return f"❌ Failed to initialize LSP: {e}"

    @mcp.tool()
    async def lsp_status() -> str:
        """Check current LSP setup status.

        Returns:
            Current LSP configuration and status
        """
        if _lsp_client is None:
            return (
                "📊 LSP Status: NOT INITIALIZED\n\n"
                "Call 'setup_lsp' first to initialize.\n"
                'Example: setup_lsp(project_dir="D:/BttUnrealEngine")'
            )

        status_lines = [
            "📊 LSP Status: INITIALIZED",
            f"  Project: {_lsp_client.project_dir}",
            f"  clangd running: {'Yes' if _lsp_client.process else 'No'}",
            f"  Open files: {len(_lsp_client.open_files)}",
        ]

        if _lsp_setup_error:
            status_lines.append(f"  ⚠️ Last error: {_lsp_setup_error}")

        return "\n".join(status_lines)

    # ═══════════════════════════════════════════════════════════════════
    # P0: Core Features (Essential)
    # ═══════════════════════════════════════════════════════════════════

    @mcp.tool()
    async def goToDefinition(
        file_path: str,
        line: int,
        character: int,
    ) -> str:
        """Find where a symbol is defined.

        Args:
            file_path: Absolute path to the source file
            line: Line number (1-based, as shown in editors)
            character: Character offset (1-based, as shown in editors)

        Returns:
            Definition location(s) as formatted string
        """
        error = await _ensure_client_ready()
        if error:
            return error

        locations = await _lsp_client.find_definition(file_path, line, character)

        if not locations:
            return f"No definition found at {file_path}:{line}:{character}"

        result_lines = ["Definition location(s):"]
        for loc in locations:
            result_lines.append(f"  {loc['file']}:{loc['line']}:{loc['character']}")

        return "\n".join(result_lines)

    @mcp.tool()
    async def findReferences(
        file_path: str,
        line: int,
        character: int,
        include_declaration: bool = True,
    ) -> str:
        """Find all references to a symbol.

        Args:
            file_path: Absolute path to the source file
            line: Line number (1-based)
            character: Character offset (1-based)
            include_declaration: Whether to include the declaration in results

        Returns:
            Reference locations as formatted string
        """
        error = await _ensure_client_ready()
        if error:
            return error

        locations = await _lsp_client.find_references(
            file_path, line, character, include_declaration
        )

        if not locations:
            return f"No references found at {file_path}:{line}:{character}"

        result_lines = [f"References ({len(locations)} found):"]
        for loc in locations:
            result_lines.append(f"  {loc['file']}:{loc['line']}:{loc['character']}")

        return "\n".join(result_lines)

    @mcp.tool()
    async def hover(
        file_path: str,
        line: int,
        character: int,
    ) -> str:
        """Get hover information (documentation, type info) for a symbol.

        Args:
            file_path: Absolute path to the source file
            line: Line number (1-based)
            character: Character offset (1-based)

        Returns:
            Symbol type and documentation information
        """
        error = await _ensure_client_ready()
        if error:
            return error

        hover_info = await _lsp_client.get_hover(file_path, line, character)

        if hover_info is None:
            return f"No hover information at {file_path}:{line}:{character}"

        return f"Hover information:\n{hover_info}"

    # ═══════════════════════════════════════════════════════════════════
    # P1: Extended Features
    # ═══════════════════════════════════════════════════════════════════

    @mcp.tool()
    async def documentSymbol(file_path: str) -> str:
        """Get all symbols (functions, classes, variables) in a document.

        Args:
            file_path: Absolute path to the source file

        Returns:
            Formatted list of all symbols in the document
        """
        error = await _ensure_client_ready()
        if error:
            return error

        symbols = await _lsp_client.document_symbol(file_path)

        if not symbols:
            return f"No symbols found in {file_path}"

        def format_symbol(sym, indent=0):
            prefix = "  " * indent
            lines = [f"{prefix}{sym['kind']}: {sym['name']} (line {sym['line']})"]
            for child in sym.get("children", []):
                lines.extend(format_symbol(child, indent + 1))
            return lines

        result_lines = [f"Symbols in {Path(file_path).name}:"]
        for sym in symbols:
            result_lines.extend(format_symbol(sym))

        return "\n".join(result_lines)

    @mcp.tool()
    async def workspaceSymbol(query: str) -> str:
        """Search for symbols across the entire workspace.

        Args:
            query: Symbol name to search for (partial match supported)

        Returns:
            Matching symbols across the project
        """
        error = await _ensure_client_ready()
        if error:
            return error

        symbols = await _lsp_client.workspace_symbol(query)

        if not symbols:
            return f"No symbols matching '{query}' found in workspace"

        result_lines = [f"Symbols matching '{query}' ({len(symbols)} found):"]
        for sym in symbols[:50]:  # Limit to 50 results
            result_lines.append(
                f"  {sym['kind']}: {sym['name']} - {sym['file']}:{sym['line']}"
            )

        if len(symbols) > 50:
            result_lines.append(f"  ... and {len(symbols) - 50} more")

        return "\n".join(result_lines)

    @mcp.tool()
    async def goToImplementation(
        file_path: str,
        line: int,
        character: int,
    ) -> str:
        """Find implementations of an interface or abstract method.

        Args:
            file_path: Absolute path to the source file
            line: Line number (1-based)
            character: Character offset (1-based)

        Returns:
            Implementation locations
        """
        error = await _ensure_client_ready()
        if error:
            return error

        locations = await _lsp_client.go_to_implementation(file_path, line, character)

        if not locations:
            return f"No implementations found at {file_path}:{line}:{character}"

        result_lines = [f"Implementations ({len(locations)} found):"]
        for loc in locations:
            result_lines.append(f"  {loc['file']}:{loc['line']}:{loc['character']}")

        return "\n".join(result_lines)

    # ═══════════════════════════════════════════════════════════════════
    # P2: Call Hierarchy Analysis
    # ═══════════════════════════════════════════════════════════════════

    @mcp.tool()
    async def prepareCallHierarchy(
        file_path: str,
        line: int,
        character: int,
    ) -> str:
        """Get call hierarchy item at a position (functions/methods).

        This prepares the call hierarchy for subsequent incomingCalls/outgoingCalls.

        Args:
            file_path: Absolute path to the source file
            line: Line number (1-based)
            character: Character offset (1-based)

        Returns:
            Call hierarchy item information (JSON format for use with other calls)
        """
        error = await _ensure_client_ready()
        if error:
            return error

        items = await _lsp_client.prepare_call_hierarchy(file_path, line, character)

        if not items:
            return f"No callable symbol at {file_path}:{line}:{character}"

        result_lines = ["Call hierarchy item(s):"]
        for item in items:
            result_lines.append(f"  Name: {item.get('name', 'unknown')}")
            result_lines.append(f"  Kind: {item.get('kind', 'unknown')}")
            result_lines.append(f"  URI: {item.get('uri', 'unknown')}")
            result_lines.append("")

        # Return JSON for use with incomingCalls/outgoingCalls
        result_lines.append("Raw data (for incomingCalls/outgoingCalls):")
        result_lines.append(json.dumps(items[0] if items else {}, indent=2))

        return "\n".join(result_lines)

    @mcp.tool()
    async def incomingCalls(
        file_path: str,
        line: int,
        character: int,
    ) -> str:
        """Find all functions/methods that call the function at a position.

        Args:
            file_path: Absolute path to the source file
            line: Line number (1-based)
            character: Character offset (1-based)

        Returns:
            List of callers with their locations
        """
        error = await _ensure_client_ready()
        if error:
            return error

        # First prepare call hierarchy
        items = await _lsp_client.prepare_call_hierarchy(file_path, line, character)
        if not items:
            return f"No callable symbol at {file_path}:{line}:{character}"

        # Get incoming calls
        callers = await _lsp_client.incoming_calls(items[0])

        if not callers:
            return f"No incoming calls found for symbol at {file_path}:{line}:{character}"

        result_lines = [f"Incoming calls ({len(callers)} callers):"]
        for caller in callers:
            result_lines.append(f"  {caller['kind']}: {caller['name']}")
            result_lines.append(f"    Location: {caller['file']}:{caller['line']}")
            if caller.get("call_sites"):
                for site in caller["call_sites"]:
                    result_lines.append(f"    Call site: line {site['line']}")

        return "\n".join(result_lines)

    @mcp.tool()
    async def outgoingCalls(
        file_path: str,
        line: int,
        character: int,
    ) -> str:
        """Find all functions/methods called by the function at a position.

        Args:
            file_path: Absolute path to the source file
            line: Line number (1-based)
            character: Character offset (1-based)

        Returns:
            List of callees with their locations
        """
        error = await _ensure_client_ready()
        if error:
            return error

        # First prepare call hierarchy
        items = await _lsp_client.prepare_call_hierarchy(file_path, line, character)
        if not items:
            return f"No callable symbol at {file_path}:{line}:{character}"

        # Get outgoing calls
        callees = await _lsp_client.outgoing_calls(items[0])

        if not callees:
            return f"No outgoing calls found for symbol at {file_path}:{line}:{character}"

        result_lines = [f"Outgoing calls ({len(callees)} callees):"]
        for callee in callees:
            result_lines.append(f"  {callee['kind']}: {callee['name']}")
            result_lines.append(f"    Location: {callee['file']}:{callee['line']}")
            if callee.get("call_sites"):
                for site in callee["call_sites"]:
                    result_lines.append(f"    Call site: line {site['line']}")

        return "\n".join(result_lines)
