"""LSP Manager - Shared clangd instance management.

This module provides a singleton LSPManager that ensures only one clangd
instance is used across all agents/requests, solving the multi-agent
inefficiency problem.

Benefits:
- Memory: ~2GB instead of ~8GB (4 agents)
- Caching: Shared index, better accuracy over time
- File state: didOpen called once per file globally
"""

import asyncio
from threading import Lock
from typing import Optional, Dict, Any, List, Set
from pathlib import Path

from rekah_mcp.lsp.client import LSPClient


class LSPManager:
    """Thread-safe singleton manager for shared clangd instance.

    This manager ensures:
    1. Only one clangd process runs regardless of agent count
    2. All LSP requests are serialized via asyncio.Lock
    3. File open state is shared across all callers
    4. Setup is idempotent (safe to call multiple times)
    """

    _instance: Optional['LSPManager'] = None
    _creation_lock = Lock()  # For thread-safe singleton creation

    def __new__(cls):
        """Thread-safe singleton pattern."""
        with cls._creation_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        """Initialize manager state (only once)."""
        if self._initialized:
            return

        self._client: Optional[LSPClient] = None
        self._request_lock = asyncio.Lock()
        self._setup_error: Optional[str] = None
        self._project_dir: Optional[str] = None
        self._compile_commands_dir: Optional[str] = None
        self._initialized = True

    @property
    def is_setup(self) -> bool:
        """Check if LSP has been setup."""
        return self._client is not None

    @property
    def is_running(self) -> bool:
        """Check if clangd process is running."""
        return self._client is not None and self._client.process is not None

    @property
    def setup_error(self) -> Optional[str]:
        """Get last setup error if any."""
        return self._setup_error

    @property
    def project_dir(self) -> Optional[str]:
        """Get configured project directory."""
        return self._project_dir

    @property
    def open_files_count(self) -> int:
        """Get number of open files."""
        if self._client is None:
            return 0
        return len(self._client.open_files)

    async def setup(
        self,
        project_dir: str,
        compile_commands_dir: Optional[str] = None,
    ) -> str:
        """Initialize shared clangd instance (idempotent).

        Safe to call multiple times - will reuse existing instance
        if already initialized with same project.

        Args:
            project_dir: Unreal Engine project root
            compile_commands_dir: Directory with compile_commands.json

        Returns:
            Status message
        """
        async with self._request_lock:
            # Already setup with same project?
            if self._client is not None:
                if self._project_dir == project_dir:
                    return (
                        f"✅ LSP already initialized (shared instance)\n"
                        f"  Project: {project_dir}\n"
                        f"  Open files: {self.open_files_count}"
                    )
                else:
                    # Different project - stop existing client
                    await self._stop_client()

            # Validate project directory
            project_path = Path(project_dir)
            if not project_path.exists():
                self._setup_error = f"Project directory not found: {project_dir}"
                return f"❌ {self._setup_error}"

            # Check compile_commands.json
            cc_dir = Path(compile_commands_dir) if compile_commands_dir else project_path
            cc_path = cc_dir / "compile_commands.json"

            if not cc_path.exists():
                self._setup_error = f"compile_commands.json not found at: {cc_path}"
                return (
                    f"❌ {self._setup_error}\n\n"
                    "To generate compile_commands.json, run:\n"
                    "  UnrealBuildTool -Mode=GenerateClangDatabase -Project=YourProject.uproject"
                )

            # Check clangd
            import shutil
            if not shutil.which("clangd"):
                self._setup_error = "clangd not found in PATH"
                return (
                    f"❌ {self._setup_error}\n\n"
                    "Please install clangd:\n"
                    "  Windows: choco install llvm\n"
                    "  Verify: clangd --version"
                )

            # Create client
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
        """Ensure clangd is running. Returns error message if failed."""
        if self._client is None:
            return (
                "⚠️ LSP not initialized!\n"
                "Please call 'setup_lsp' tool first.\n"
                'Example: setup_lsp(project_dir="D:/BttUnrealEngine")'
            )

        if self._setup_error:
            return f"⚠️ LSP setup failed: {self._setup_error}"

        # Check if process is actually alive (not just not None)
        process_dead = (
            self._client.process is None or
            self._client.process.returncode is not None
        )

        if process_dead:
            # Stop any dead process state
            try:
                await self._client.stop()
            except Exception:
                pass

            # Start fresh
            try:
                await self._client.start()
            except Exception as e:
                return f"⚠️ Failed to start clangd: {e}"

        return None

    async def _stop_client(self):
        """Stop existing client."""
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception:
                pass
            self._client = None
            self._project_dir = None

    # ═══════════════════════════════════════════════════════════════════
    # LSP Operations (all thread-safe via _request_lock)
    # ═══════════════════════════════════════════════════════════════════

    async def find_definition(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """Find definition of symbol at position."""
        async with self._request_lock:
            return await self._client.find_definition(file_path, line, character)

    async def find_references(
        self,
        file_path: str,
        line: int,
        character: int,
        include_declaration: bool = True,
    ) -> List[Dict[str, Any]]:
        """Find all references to symbol at position."""
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
        """Get hover information."""
        async with self._request_lock:
            return await self._client.get_hover(file_path, line, character)

    async def document_symbol(self, file_path: str) -> List[Dict[str, Any]]:
        """Get all symbols in a document."""
        async with self._request_lock:
            return await self._client.document_symbol(file_path)

    async def workspace_symbol(self, query: str) -> List[Dict[str, Any]]:
        """Search for symbols across workspace."""
        async with self._request_lock:
            return await self._client.workspace_symbol(query)

    async def go_to_implementation(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """Find implementations of interface/abstract method."""
        async with self._request_lock:
            return await self._client.go_to_implementation(file_path, line, character)

    async def prepare_call_hierarchy(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> List[Dict[str, Any]]:
        """Prepare call hierarchy at position."""
        async with self._request_lock:
            return await self._client.prepare_call_hierarchy(file_path, line, character)

    async def incoming_calls(
        self,
        call_hierarchy_item: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Find all callers of a function."""
        async with self._request_lock:
            return await self._client.incoming_calls(call_hierarchy_item)

    async def outgoing_calls(
        self,
        call_hierarchy_item: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Find all callees of a function."""
        async with self._request_lock:
            return await self._client.outgoing_calls(call_hierarchy_item)


# Global singleton instance
_manager: Optional[LSPManager] = None


def get_lsp_manager() -> LSPManager:
    """Get the global LSPManager singleton.

    This is the primary entry point for all LSP operations.
    The manager ensures only one clangd instance exists.
    """
    global _manager
    if _manager is None:
        _manager = LSPManager()
    return _manager
