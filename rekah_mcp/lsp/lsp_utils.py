"""lsp utilities for clangd communication

this module contains:
- JSONRPCProtocol: json-rpc message parsing and formatting
- LSPClient: clangd subprocess and communication management
- LSPManager: shared clangd instance singleton manager

LSP uses JSON-RPC 2.0 over stdio with Content-Length headers:
  Content-Length: <length>\r\n
  \r\n
  <JSON payload>
"""

import asyncio
import json
from pathlib import Path
from typing import Optional, Dict, Any, List

from rekah_mcp.utils.singleton_utils import SingletonInstance


# ═══════════════════════════════════════════════════════════════════════════
# JSONRPCProtocol
# ═══════════════════════════════════════════════════════════════════════════


class JSONRPCProtocol:
    """handles JSON-RPC message parsing and formatting for LSP"""

    def __init__(self):
        self.buffer = b""

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        """feed data and return complete messages

        Args:
            data: raw bytes from clangd stdout

        Returns:
            list of parsed JSON-RPC messages (may be empty if incomplete)
        """
        self.buffer += data
        messages = []

        while True:
            message = self._try_parse_message()
            if message is None:
                break
            messages.append(message)

        return messages

    def _try_parse_message(self) -> Optional[Dict[str, Any]]:
        """try to parse a complete JSON-RPC message from buffer

        Returns:
            parsed message dict, or None if incomplete
        """
        # find header end (double CRLF)
        header_end = self.buffer.find(b"\r\n\r\n")
        if header_end == -1:
            return None

        # parse Content-Length header
        header = self.buffer[:header_end].decode("utf-8")
        content_length = None

        for line in header.split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":")[1].strip())
                break

        if content_length is None:
            # malformed message, skip this header
            self.buffer = self.buffer[header_end + 4 :]
            return None

        # check if we have complete content
        content_start = header_end + 4
        content_end = content_start + content_length

        if len(self.buffer) < content_end:
            # not enough data yet
            return None

        # extract and parse content
        content = self.buffer[content_start:content_end].decode("utf-8")
        self.buffer = self.buffer[content_end:]

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # invalid JSON, skip
            return None

    def encode(self, message: Dict[str, Any]) -> bytes:
        """encode a JSON-RPC message with Content-Length header

        Args:
            message: JSON-RPC message dict

        Returns:
            encoded bytes ready to send to clangd stdin
        """
        content = json.dumps(message)
        content_bytes = content.encode("utf-8")
        header = f"Content-Length: {len(content_bytes)}\r\n\r\n"
        return header.encode("utf-8") + content_bytes

    def clear(self):
        """clear the internal buffer"""
        self.buffer = b""


# ═══════════════════════════════════════════════════════════════════════════
# LSPClient
# ═══════════════════════════════════════════════════════════════════════════


class LSPClient:
    """manages clangd subprocess and JSON-RPC communication

    this client:
    1. spawns clangd as a subprocess using asyncio
    2. communicates via JSON-RPC over stdin/stdout
    3. manually sends didOpen to enable file tracking (key bug workaround)
    """

    def __init__(
        self,
        project_dir: str,
        compile_commands_dir: Optional[str] = None,
    ):
        """initialize LSP client

        Args:
            project_dir: Unreal Engine project root directory
            compile_commands_dir: directory containing compile_commands.json
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
        # file indexing tracking
        self._file_ready_events: Dict[str, asyncio.Event] = {}
        self._indexed_files: set = set()
        # background indexing status
        self._indexing_in_progress: bool = False
        self._indexing_percentage: Optional[int] = None
        self._indexing_message: str = ""

    async def start(self):
        """start clangd process and initialize LSP connection"""
        if self.process is not None:
            return  # already running

        args = [
            "clangd",
            "--log=error",
            "--pretty",
            "--background-index",
            f"--compile-commands-dir={self.compile_commands_dir}",
            "-j=2",
        ]

        # start clangd subprocess using asyncio
        self.process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.project_dir),
        )

        # start background reader task
        self._reader_task = asyncio.create_task(self._read_stdout())

        # initialize LSP protocol
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
                    "window": {
                        "workDoneProgress": True,
                    },
                },
            },
        )

        await self._send_notification("initialized", {})

    async def stop(self):
        """stop clangd process gracefully"""
        if self.process is None:
            return

        # cancel reader task
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
            pass  # process may already be dead

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
        """ensure file is opened in clangd (key: sends didOpen)

        this is the core workaround for Claude Code's LSP bug.
        clangd needs didOpen to track files and provide accurate results.

        Args:
            file_path: absolute path to the file

        Returns:
            True if file was newly opened, False if already open
        """
        uri = self._path_to_uri(file_path)

        if uri in self.open_files:
            return False

        # read file content
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            raise RuntimeError(f"Failed to read file {file_path}: {e}")

        # send didOpen notification (THE KEY BUG WORKAROUND)
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

        # wait briefly for clangd to process
        await asyncio.sleep(0.1)
        return True

    async def wait_for_file(self, file_path: str, timeout: float = 30.0) -> bool:
        """wait for file indexing to complete

        opens the file and waits for clangd to send publishDiagnostics,
        which indicates the file has been processed.

        Args:
            file_path: absolute path to the file
            timeout: maximum wait time in seconds

        Returns:
            True if file was indexed, False if timeout
        """
        uri = self._path_to_uri(file_path)

        # check if already indexed
        if uri in self._indexed_files:
            return True

        # create event for this file
        event = asyncio.Event()
        self._file_ready_events[uri] = event

        try:
            # open file to trigger indexing
            await self.ensure_file_open(file_path)

            # wait for diagnostics
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True

        except asyncio.TimeoutError:
            return False

        finally:
            # cleanup
            self._file_ready_events.pop(uri, None)

    def is_file_indexed(self, file_path: str) -> bool:
        """check if file has been indexed"""
        uri = self._path_to_uri(file_path)
        return uri in self._indexed_files

    @property
    def is_indexing(self) -> bool:
        """check if background indexing is in progress"""
        return self._indexing_in_progress

    @property
    def indexing_status(self) -> str:
        """get indexing status message"""
        if not self._indexing_in_progress:
            return "idle"
        if self._indexing_percentage is not None:
            return f"indexing ({self._indexing_percentage}%)"
        return "indexing"

    # ═══════════════════════════════════════════════════════════════════
    # core LSP methods
    # ═══════════════════════════════════════════════════════════════════

    async def find_definition(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """find definition of symbol at position"""
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
        """find all references to symbol at position"""
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
        """get hover information (type, documentation)"""
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
    # extended LSP methods
    # ═══════════════════════════════════════════════════════════════════

    async def document_symbol(self, file_path: str) -> List[Dict[str, Any]]:
        """get all symbols in a document"""
        await self.ensure_file_open(file_path)

        result = await self._send_request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": self._path_to_uri(file_path)}},
        )

        return self._normalize_symbols(result)

    async def workspace_symbol(self, query: str) -> List[Dict[str, Any]]:
        """search for symbols across the workspace"""
        result = await self._send_request("workspace/symbol", {"query": query})
        return self._normalize_symbols(result)

    async def go_to_implementation(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """find implementations of an interface or abstract method"""
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
    # call hierarchy methods
    # ═══════════════════════════════════════════════════════════════════

    async def prepare_call_hierarchy(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """prepare call hierarchy item at position"""
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
        """find all callers of a function"""
        result = await self._send_request(
            "callHierarchy/incomingCalls", {"item": call_hierarchy_item}
        )
        return self._normalize_call_hierarchy(result, "from")

    async def outgoing_calls(
        self, call_hierarchy_item: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """find all callees of a function"""
        result = await self._send_request(
            "callHierarchy/outgoingCalls", {"item": call_hierarchy_item}
        )
        return self._normalize_call_hierarchy(result, "to")

    # ═══════════════════════════════════════════════════════════════════
    # internal: communication (async)
    # ═══════════════════════════════════════════════════════════════════

    async def _send_request(self, method: str, params: dict) -> Any:
        """send JSON-RPC request and wait for response"""
        self.request_id += 1
        request_id = self.request_id

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        # create future for response
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self.pending_requests[request_id] = future

        # send request
        await self._write_message(request)

        # wait for response with timeout
        try:
            result = await asyncio.wait_for(future, timeout=30.0)
            return result
        except asyncio.TimeoutError:
            self.pending_requests.pop(request_id, None)
            raise TimeoutError(f"LSP request timed out: {method}")

    async def _send_notification(self, method: str, params: dict):
        """send JSON-RPC notification (no response expected)"""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._write_message(notification)

    async def _write_message(self, message: dict):
        """write JSON-RPC message to clangd stdin"""
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("clangd process not running")

        data = self.protocol.encode(message)
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def _read_stdout(self):
        """background task: read stdout and dispatch responses"""
        try:
            while True:
                if self.process is None or self.process.stdout is None:
                    break

                # read data asynchronously
                data = await self.process.stdout.read(4096)
                if not data:
                    break

                messages = self.protocol.feed(data)
                for msg in messages:
                    self._handle_message(msg)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            # log error but don't crash
            print(f"LSP reader error: {e}")

    def _handle_message(self, message: Dict[str, Any]):
        """handle incoming JSON-RPC message"""
        if "id" in message and "method" not in message:
            # this is a response
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

        elif "method" in message:
            # this is a notification from server
            method = message["method"]
            params = message.get("params", {})

            if method == "textDocument/publishDiagnostics":
                # file indexing complete signal
                uri = params.get("uri", "")
                if uri:
                    self._indexed_files.add(uri)
                    if uri in self._file_ready_events:
                        self._file_ready_events[uri].set()

            elif method == "$/progress":
                # background indexing progress
                value = params.get("value", {})
                kind = value.get("kind", "")
                title = value.get("title", "")

                if "index" in title.lower() or "background" in title.lower():
                    if kind == "begin":
                        self._indexing_in_progress = True
                        self._indexing_percentage = 0
                        self._indexing_message = value.get("message", "Starting...")
                    elif kind == "report":
                        self._indexing_percentage = value.get("percentage")
                        self._indexing_message = value.get("message", "")
                    elif kind == "end":
                        self._indexing_in_progress = False
                        self._indexing_percentage = 100
                        self._indexing_message = "Complete"

    # ═══════════════════════════════════════════════════════════════════
    # internal: utilities
    # ═══════════════════════════════════════════════════════════════════

    def _path_to_uri(self, file_path: str) -> str:
        """convert file path to LSP URI format"""
        path = file_path.replace("\\", "/")
        if len(path) >= 2 and path[1] == ":":
            path = "/" + path
        return f"file://{path}"

    def _uri_to_path(self, uri: str) -> str:
        """convert LSP URI to file path"""
        path = uri.replace("file://", "")
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path

    def _get_language_id(self, file_path: str) -> str:
        """determine language ID from file extension"""
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
        """normalize LSP location results"""
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
        """extract readable content from hover response"""
        if isinstance(contents, str):
            return contents
        if isinstance(contents, dict):
            return contents.get("value", str(contents))
        if isinstance(contents, list):
            return "\n".join(self._extract_hover_content(c) for c in contents)
        return str(contents)

    def _normalize_symbols(self, result) -> List[Dict[str, Any]]:
        """normalize LSP symbol results"""
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
        """normalize call hierarchy results"""
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
        """convert LSP SymbolKind to human-readable string"""
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


# ═══════════════════════════════════════════════════════════════════════════
# LSPManager
# ═══════════════════════════════════════════════════════════════════════════


class LSPManager(SingletonInstance):
    """singleton manager for shared clangd instance

    this manager ensures:
    1. only one clangd process runs regardless of agent count
    2. all LSP requests are serialized via asyncio.Lock
    3. file open state is shared across all callers
    4. setup is idempotent (safe to call multiple times)
    """

    def __init__(self):
        """initialize manager state (only once)"""
        if hasattr(self, '_initialized') and self._initialized:
            return

        self._client: Optional[LSPClient] = None
        self._request_lock = asyncio.Lock()
        self._setup_error: Optional[str] = None
        self._project_dir: Optional[str] = None
        self._compile_commands_dir: Optional[str] = None
        self._initialized = True

    @property
    def is_setup(self) -> bool:
        """check if LSP has been setup"""
        return self._client is not None

    @property
    def is_running(self) -> bool:
        """check if clangd process is running"""
        return self._client is not None and self._client.process is not None

    @property
    def setup_error(self) -> Optional[str]:
        """get last setup error if any"""
        return self._setup_error

    @property
    def project_dir(self) -> Optional[str]:
        """get configured project directory"""
        return self._project_dir

    @property
    def open_files_count(self) -> int:
        """get number of open files"""
        if self._client is None:
            return 0
        return len(self._client.open_files)

    async def setup(
        self,
        project_dir: str,
        compile_commands_dir: Optional[str] = None,
    ) -> str:
        """initialize shared clangd instance (idempotent)

        safe to call multiple times - will reuse existing instance
        if already initialized with same project.

        Args:
            project_dir: Unreal Engine project root
            compile_commands_dir: directory with compile_commands.json

        Returns:
            status message
        """
        async with self._request_lock:
            # already setup with same project?
            if self._client is not None:
                if self._project_dir == project_dir:
                    return (
                        f"✅ LSP already initialized (shared instance)\n"
                        f"  Project: {project_dir}\n"
                        f"  Open files: {self.open_files_count}"
                    )
                else:
                    # different project - stop existing client
                    await self._stop_client()

            # validate project directory
            project_path = Path(project_dir)
            if not project_path.exists():
                self._setup_error = f"Project directory not found: {project_dir}"
                return f"❌ {self._setup_error}"

            # check compile_commands.json
            cc_dir = Path(compile_commands_dir) if compile_commands_dir else project_path
            cc_path = cc_dir / "compile_commands.json"

            if not cc_path.exists():
                self._setup_error = f"compile_commands.json not found at: {cc_path}"
                return (
                    f"❌ {self._setup_error}\n\n"
                    "To generate compile_commands.json, run:\n"
                    "  UnrealBuildTool -Mode=GenerateClangDatabase -Project=YourProject.uproject"
                )

            # check clangd
            import shutil
            if not shutil.which("clangd"):
                self._setup_error = "clangd not found in PATH"
                return (
                    f"❌ {self._setup_error}\n\n"
                    "Please install clangd:\n"
                    "  Windows: choco install llvm\n"
                    "  Verify: clangd --version"
                )

            # create client
            try:
                self._client = LSPClient(
                    project_dir=str(project_path),
                    compile_commands_dir=str(cc_dir),
                )
                self._project_dir = project_dir
                self._compile_commands_dir = str(cc_dir)
                self._setup_error = None

                return (
                    f"✅ LSP initialized (shared instance)\n"
                    f"  Project: {project_dir}\n"
                    f"  compile_commands.json: {cc_path}"
                )
            except Exception as e:
                self._setup_error = str(e)
                return f"❌ Failed to initialize LSP: {e}"

    async def ensure_running(self) -> Optional[str]:
        """ensure clangd is running. Returns error message if failed."""
        if self._client is None:
            return (
                "⚠️ LSP not initialized!\n"
                "Please call 'setup_lsp' tool first.\n"
                'Example: setup_lsp(project_dir="D:/BttUnrealEngine")'
            )

        if self._setup_error:
            return f"⚠️ LSP setup failed: {self._setup_error}"

        # check if process is actually alive (not just not None)
        process_dead = (
            self._client.process is None or
            self._client.process.returncode is not None
        )

        if process_dead:
            # stop any dead process state
            try:
                await self._client.stop()
            except Exception:
                pass

            # start fresh
            try:
                await self._client.start()
            except Exception as e:
                return f"⚠️ Failed to start clangd: {e}"

        return None

    async def _stop_client(self):
        """stop existing client"""
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception:
                pass
            self._client = None
            self._project_dir = None

    # ═══════════════════════════════════════════════════════════════════
    # LSP operations (all thread-safe via _request_lock)
    # ═══════════════════════════════════════════════════════════════════

    async def find_definition(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """find definition of symbol at position"""
        async with self._request_lock:
            return await self._client.find_definition(file_path, line, character)

    async def find_references(
        self,
        file_path: str,
        line: int,
        character: int,
        include_declaration: bool = True,
    ) -> List[Dict[str, Any]]:
        """find all references to symbol at position"""
        async with self._request_lock:
            return await self._client.find_references(
                file_path, line, character, include_declaration
            )

    async def get_hover(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> Optional[str]:
        """get hover information"""
        async with self._request_lock:
            return await self._client.get_hover(file_path, line, character)

    async def document_symbol(self, file_path: str) -> List[Dict[str, Any]]:
        """get all symbols in a document"""
        async with self._request_lock:
            return await self._client.document_symbol(file_path)

    async def workspace_symbol(self, query: str) -> List[Dict[str, Any]]:
        """search for symbols across workspace"""
        async with self._request_lock:
            return await self._client.workspace_symbol(query)

    async def go_to_implementation(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """find implementations of interface/abstract method"""
        async with self._request_lock:
            return await self._client.go_to_implementation(file_path, line, character)

    async def prepare_call_hierarchy(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """prepare call hierarchy at position"""
        async with self._request_lock:
            return await self._client.prepare_call_hierarchy(file_path, line, character)

    async def incoming_calls(
        self,
        call_hierarchy_item: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """find all callers of a function"""
        async with self._request_lock:
            return await self._client.incoming_calls(call_hierarchy_item)

    async def outgoing_calls(
        self,
        call_hierarchy_item: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """find all callees of a function"""
        async with self._request_lock:
            return await self._client.outgoing_calls(call_hierarchy_item)

    async def wait_for_file(self, file_path: str, timeout: float = 30.0) -> bool:
        """wait for file indexing to complete

        Args:
            file_path: absolute path to the file
            timeout: maximum wait time in seconds

        Returns:
            True if file was indexed, False if timeout
        """
        async with self._request_lock:
            return await self._client.wait_for_file(file_path, timeout)

    def is_file_indexed(self, file_path: str) -> bool:
        """check if file has been indexed"""
        if self._client is None:
            return False
        return self._client.is_file_indexed(file_path)

    @property
    def is_indexing(self) -> bool:
        """check if background indexing is in progress"""
        if self._client is None:
            return False
        return self._client.is_indexing

    @property
    def indexing_status(self) -> str:
        """get indexing status message"""
        if self._client is None:
            return "not initialized"
        return self._client.indexing_status


# ═══════════════════════════════════════════════════════════════════════════
# module-level singleton accessor
# ═══════════════════════════════════════════════════════════════════════════


def get_lsp_manager() -> LSPManager:
    """get the global LSPManager singleton

    this is the primary entry point for all LSP operations.
    the manager ensures only one clangd instance exists.
    """
    return LSPManager.instance()
