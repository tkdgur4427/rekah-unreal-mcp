"""LSP Client for clangd communication.

This module manages the clangd subprocess and handles JSON-RPC communication.
The key feature is manually sending textDocument/didOpen to work around
Claude Code's LSP Tool bug where didOpen is not sent automatically.
"""

import asyncio
import json
from pathlib import Path
from typing import Optional, Dict, Any, List

from rekah_mcp.lsp.protocol import JSONRPCProtocol


class LSPClient:
    """Manages clangd subprocess and JSON-RPC communication.

    This client:
    1. Spawns clangd as a subprocess using asyncio
    2. Communicates via JSON-RPC over stdin/stdout
    3. Manually sends didOpen to enable file tracking (key bug workaround)
    """

    def __init__(
        self,
        project_dir: str,
        compile_commands_dir: Optional[str] = None,
    ):
        """Initialize LSP client.

        Args:
            project_dir: Unreal Engine project root directory
            compile_commands_dir: Directory containing compile_commands.json
                                  (defaults to project_dir if not specified)
        """
        self.project_dir = Path(project_dir)
        self.compile_commands_dir = Path(compile_commands_dir or project_dir)
        self.process: Optional[asyncio.subprocess.Process] = None
        self.request_id = 0
        self.pending_requests: Dict[int, asyncio.Future] = {}
        self.open_files: set = set()
        self.protocol = JSONRPCProtocol()
        self._reader_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start clangd process and initialize LSP connection."""
        if self.process is not None:
            return  # Already running

        args = [
            "clangd",
            "--log=error",
            "--pretty",
            "--background-index",
            f"--compile-commands-dir={self.compile_commands_dir}",
            "-j=2",
        ]

        # Start clangd subprocess using asyncio
        self.process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.project_dir),
        )

        # Start background reader task
        self._reader_task = asyncio.create_task(self._read_stdout())

        # Initialize LSP protocol
        root_uri = self._path_to_uri(str(self.project_dir))

        await self._send_request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": {
                    "textDocument": {
                        "hover": {"contentFormat": ["plaintext", "markdown"]},
                        "definition": {"linkSupport": True},
                        "references": {},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "callHierarchy": {},
                    },
                    "workspace": {
                        "symbol": {"symbolKind": {"valueSet": list(range(1, 27))}}
                    },
                },
            },
        )

        await self._send_notification("initialized", {})

    async def stop(self):
        """Stop clangd process gracefully."""
        if self.process is None:
            return

        # Cancel reader task
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        try:
            await self._send_request("shutdown", {})
            await self._send_notification("exit", {})
        except Exception:
            pass  # Process may already be dead

        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            self.process.kill()

        self.process = None
        self.open_files.clear()
        self.protocol.clear()
        self.pending_requests.clear()

    async def ensure_file_open(self, file_path: str) -> bool:
        """Ensure file is opened in clangd (key: sends didOpen).

        This is the core workaround for Claude Code's LSP bug.
        clangd needs didOpen to track files and provide accurate results.

        Args:
            file_path: Absolute path to the file

        Returns:
            True if file was newly opened, False if already open
        """
        uri = self._path_to_uri(file_path)

        if uri in self.open_files:
            return False

        # Read file content
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            raise RuntimeError(f"Failed to read file {file_path}: {e}")

        # Send didOpen notification (THE KEY BUG WORKAROUND)
        await self._send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": self._get_language_id(file_path),
                    "version": 1,
                    "text": content,
                }
            },
        )

        self.open_files.add(uri)

        # Wait briefly for clangd to process
        await asyncio.sleep(0.1)
        return True

    # ═══════════════════════════════════════════════════════════════════
    # P0: Core LSP Methods
    # ═══════════════════════════════════════════════════════════════════

    async def find_definition(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """Find definition of symbol at position."""
        await self.ensure_file_open(file_path)

        result = await self._send_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": self._path_to_uri(file_path)},
                "position": {"line": line - 1, "character": character - 1},
            },
        )

        return self._normalize_locations(result)

    async def find_references(
        self,
        file_path: str,
        line: int,
        character: int,
        include_declaration: bool = True,
    ) -> List[Dict[str, Any]]:
        """Find all references to symbol at position."""
        await self.ensure_file_open(file_path)

        result = await self._send_request(
            "textDocument/references",
            {
                "textDocument": {"uri": self._path_to_uri(file_path)},
                "position": {"line": line - 1, "character": character - 1},
                "context": {"includeDeclaration": include_declaration},
            },
        )

        return self._normalize_locations(result)

    async def get_hover(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> Optional[str]:
        """Get hover information (type, documentation)."""
        await self.ensure_file_open(file_path)

        result = await self._send_request(
            "textDocument/hover",
            {
                "textDocument": {"uri": self._path_to_uri(file_path)},
                "position": {"line": line - 1, "character": character - 1},
            },
        )

        if result and "contents" in result:
            return self._extract_hover_content(result["contents"])
        return None

    # ═══════════════════════════════════════════════════════════════════
    # P1: Extended LSP Methods
    # ═══════════════════════════════════════════════════════════════════

    async def document_symbol(self, file_path: str) -> List[Dict[str, Any]]:
        """Get all symbols in a document."""
        await self.ensure_file_open(file_path)

        result = await self._send_request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": self._path_to_uri(file_path)}},
        )

        return self._normalize_symbols(result)

    async def workspace_symbol(self, query: str) -> List[Dict[str, Any]]:
        """Search for symbols across the workspace."""
        result = await self._send_request("workspace/symbol", {"query": query})
        return self._normalize_symbols(result)

    async def go_to_implementation(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """Find implementations of an interface or abstract method."""
        await self.ensure_file_open(file_path)

        result = await self._send_request(
            "textDocument/implementation",
            {
                "textDocument": {"uri": self._path_to_uri(file_path)},
                "position": {"line": line - 1, "character": character - 1},
            },
        )

        return self._normalize_locations(result)

    # ═══════════════════════════════════════════════════════════════════
    # P2: Call Hierarchy Methods
    # ═══════════════════════════════════════════════════════════════════

    async def prepare_call_hierarchy(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """Prepare call hierarchy item at position."""
        await self.ensure_file_open(file_path)

        result = await self._send_request(
            "textDocument/prepareCallHierarchy",
            {
                "textDocument": {"uri": self._path_to_uri(file_path)},
                "position": {"line": line - 1, "character": character - 1},
            },
        )

        return result if result else []

    async def incoming_calls(
        self, call_hierarchy_item: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Find all callers of a function."""
        result = await self._send_request(
            "callHierarchy/incomingCalls", {"item": call_hierarchy_item}
        )
        return self._normalize_call_hierarchy(result, "from")

    async def outgoing_calls(
        self, call_hierarchy_item: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Find all callees of a function."""
        result = await self._send_request(
            "callHierarchy/outgoingCalls", {"item": call_hierarchy_item}
        )
        return self._normalize_call_hierarchy(result, "to")

    # ═══════════════════════════════════════════════════════════════════
    # Internal: Communication (Async)
    # ═══════════════════════════════════════════════════════════════════

    async def _send_request(self, method: str, params: dict) -> Any:
        """Send JSON-RPC request and wait for response."""
        self.request_id += 1
        request_id = self.request_id

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        # Create future for response
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self.pending_requests[request_id] = future

        # Send request
        await self._write_message(request)

        # Wait for response with timeout
        try:
            result = await asyncio.wait_for(future, timeout=30.0)
            return result
        except asyncio.TimeoutError:
            self.pending_requests.pop(request_id, None)
            raise TimeoutError(f"LSP request timed out: {method}")

    async def _send_notification(self, method: str, params: dict):
        """Send JSON-RPC notification (no response expected)."""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._write_message(notification)

    async def _write_message(self, message: dict):
        """Write JSON-RPC message to clangd stdin."""
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("clangd process not running")

        data = self.protocol.encode(message)
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def _read_stdout(self):
        """Background task: read stdout and dispatch responses."""
        try:
            while True:
                if self.process is None or self.process.stdout is None:
                    break

                # Read data asynchronously
                data = await self.process.stdout.read(4096)
                if not data:
                    break

                messages = self.protocol.feed(data)
                for msg in messages:
                    self._handle_message(msg)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            # Log error but don't crash
            print(f"LSP reader error: {e}")

    def _handle_message(self, message: Dict[str, Any]):
        """Handle incoming JSON-RPC message."""
        if "id" in message and "method" not in message:
            # This is a response
            request_id = message["id"]
            if request_id in self.pending_requests:
                future = self.pending_requests.pop(request_id)
                if "error" in message:
                    error = message["error"]
                    future.set_exception(
                        RuntimeError(
                            f"LSP error: {error.get('message', 'Unknown error')}"
                        )
                    )
                else:
                    result = message.get("result")
                    future.set_result(result)

        # Notifications from server are ignored for now

    # ═══════════════════════════════════════════════════════════════════
    # Internal: Utilities
    # ═══════════════════════════════════════════════════════════════════

    def _path_to_uri(self, file_path: str) -> str:
        """Convert file path to LSP URI format."""
        path = file_path.replace("\\", "/")
        if len(path) >= 2 and path[1] == ":":
            path = "/" + path
        return f"file://{path}"

    def _uri_to_path(self, uri: str) -> str:
        """Convert LSP URI to file path."""
        path = uri.replace("file://", "")
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path

    def _get_language_id(self, file_path: str) -> str:
        """Determine language ID from file extension."""
        ext = Path(file_path).suffix.lower()
        return {
            ".cpp": "cpp",
            ".cc": "cpp",
            ".cxx": "cpp",
            ".c": "c",
            ".h": "cpp",
            ".hpp": "cpp",
            ".hxx": "cpp",
        }.get(ext, "cpp")

    def _normalize_locations(self, result) -> List[Dict[str, Any]]:
        """Normalize LSP location results."""
        if result is None:
            return []
        if isinstance(result, dict):
            result = [result]

        locations = []
        for loc in result:
            uri = loc.get("uri", loc.get("targetUri", ""))
            range_data = loc.get("range", loc.get("targetRange", {}))
            start = range_data.get("start", {})

            locations.append(
                {
                    "file": self._uri_to_path(uri),
                    "line": start.get("line", 0) + 1,
                    "character": start.get("character", 0) + 1,
                }
            )
        return locations

    def _extract_hover_content(self, contents) -> str:
        """Extract readable content from hover response."""
        if isinstance(contents, str):
            return contents
        if isinstance(contents, dict):
            return contents.get("value", str(contents))
        if isinstance(contents, list):
            return "\n".join(self._extract_hover_content(c) for c in contents)
        return str(contents)

    def _normalize_symbols(self, result) -> List[Dict[str, Any]]:
        """Normalize LSP symbol results."""
        if result is None:
            return []

        symbols = []
        for sym in result:
            if "selectionRange" in sym:
                symbols.append(
                    {
                        "name": sym.get("name", ""),
                        "kind": self._symbol_kind_to_string(sym.get("kind", 0)),
                        "file": None,
                        "line": sym["selectionRange"]["start"]["line"] + 1,
                        "character": sym["selectionRange"]["start"]["character"] + 1,
                        "children": self._normalize_symbols(sym.get("children", [])),
                    }
                )
            elif "location" in sym:
                loc = sym["location"]
                symbols.append(
                    {
                        "name": sym.get("name", ""),
                        "kind": self._symbol_kind_to_string(sym.get("kind", 0)),
                        "file": self._uri_to_path(loc["uri"]),
                        "line": loc["range"]["start"]["line"] + 1,
                        "character": loc["range"]["start"]["character"] + 1,
                        "children": [],
                    }
                )

        return symbols

    def _normalize_call_hierarchy(
        self,
        result,
        direction: str,
    ) -> List[Dict[str, Any]]:
        """Normalize call hierarchy results."""
        if result is None:
            return []

        calls = []
        for item in result:
            call_item = item.get(direction, {})
            selection_range = call_item.get("selectionRange", {}).get("start", {})

            calls.append(
                {
                    "name": call_item.get("name", ""),
                    "kind": self._symbol_kind_to_string(call_item.get("kind", 0)),
                    "file": self._uri_to_path(call_item.get("uri", "")),
                    "line": selection_range.get("line", 0) + 1,
                    "character": selection_range.get("character", 0) + 1,
                    "call_sites": [
                        {
                            "line": r["start"]["line"] + 1,
                            "character": r["start"]["character"] + 1,
                        }
                        for r in item.get("fromRanges", [])
                    ],
                }
            )

        return calls

    def _symbol_kind_to_string(self, kind: int) -> str:
        """Convert LSP SymbolKind to human-readable string."""
        kinds = {
            1: "File",
            2: "Module",
            3: "Namespace",
            4: "Package",
            5: "Class",
            6: "Method",
            7: "Property",
            8: "Field",
            9: "Constructor",
            10: "Enum",
            11: "Interface",
            12: "Function",
            13: "Variable",
            14: "Constant",
            15: "String",
            16: "Number",
            17: "Boolean",
            18: "Array",
            19: "Object",
            20: "Key",
            21: "Null",
            22: "EnumMember",
            23: "Struct",
            24: "Event",
            25: "Operator",
            26: "TypeParameter",
        }
        return kinds.get(kind, f"Unknown({kind})")
