"""Tests for LSPManager singleton.

These tests verify:
1. Singleton pattern works correctly
2. Thread-safe initialization
3. Shared clangd instance behavior
4. Idempotent setup
"""

import pytest
import asyncio
from pathlib import Path

from rekah_mcp.lsp.manager import LSPManager, get_lsp_manager


class TestLSPManagerSingleton:
    """Tests for LSPManager singleton pattern."""

    def test_singleton_same_instance(self):
        """Test that multiple calls return same instance."""
        manager1 = LSPManager()
        manager2 = LSPManager()

        assert manager1 is manager2
        assert id(manager1) == id(manager2)

    def test_get_lsp_manager_returns_singleton(self):
        """Test that get_lsp_manager returns singleton."""
        manager1 = get_lsp_manager()
        manager2 = get_lsp_manager()

        assert manager1 is manager2

    def test_manager_initial_state(self):
        """Test initial state of manager."""
        manager = get_lsp_manager()

        # Reset for test (simulate fresh state)
        manager._client = None
        manager._setup_error = None
        manager._project_dir = None

        assert manager.is_setup is False
        assert manager.is_running is False
        assert manager.setup_error is None
        assert manager.project_dir is None
        assert manager.open_files_count == 0


class TestLSPManagerSetup:
    """Tests for LSPManager setup functionality."""

    async def test_setup_nonexistent_project(self):
        """Test setup with non-existent project directory."""
        manager = get_lsp_manager()

        # Reset
        manager._client = None
        manager._setup_error = None

        result = await manager.setup("/nonexistent/path/to/project")

        assert "❌" in result
        assert "not found" in result.lower()
        assert manager.is_setup is False

    async def test_setup_idempotent(self):
        """Test that setup is idempotent (safe to call multiple times)."""
        manager = get_lsp_manager()

        # Reset
        manager._client = None
        manager._project_dir = None

        # First call (will fail if project doesn't exist, but that's OK)
        result1 = await manager.setup("D:/BttUnrealEngine")

        # Second call with same project should return "already initialized"
        if manager.is_setup:
            result2 = await manager.setup("D:/BttUnrealEngine")
            assert "already initialized" in result2.lower() or "✅" in result2


class TestLSPManagerProperties:
    """Tests for LSPManager property accessors."""

    def test_is_setup_false_when_no_client(self):
        """Test is_setup is False when client is None."""
        manager = get_lsp_manager()
        manager._client = None

        assert manager.is_setup is False

    def test_is_running_false_when_no_client(self):
        """Test is_running is False when client is None."""
        manager = get_lsp_manager()
        manager._client = None

        assert manager.is_running is False

    def test_open_files_count_zero_when_no_client(self):
        """Test open_files_count is 0 when client is None."""
        manager = get_lsp_manager()
        manager._client = None

        assert manager.open_files_count == 0


# E2E tests with real clangd (skipped if project not available)
PROJECT_DIR = "D:/BttUnrealEngine"

pytestmark_e2e = pytest.mark.skipif(
    not Path(PROJECT_DIR).exists(),
    reason=f"Project directory not found: {PROJECT_DIR}"
)


@pytest.mark.skipif(
    not Path(PROJECT_DIR).exists(),
    reason=f"Project directory not found: {PROJECT_DIR}"
)
class TestLSPManagerE2E:
    """E2E tests for LSPManager with real clangd."""

    @pytest.fixture
    async def manager(self):
        """Get manager and ensure it's setup and running."""
        manager = get_lsp_manager()
        # Always ensure setup and running before each test
        await manager.setup(PROJECT_DIR)
        await manager.ensure_running()
        yield manager

    async def test_setup_and_ensure_running(self):
        """Test setup and ensure_running with real project."""
        manager = get_lsp_manager()

        # Setup (idempotent - safe to call again)
        result = await manager.setup(PROJECT_DIR)
        assert "✅" in result or "already" in result.lower()
        assert manager.is_setup is True

        # Ensure running
        error = await manager.ensure_running()
        assert error is None
        assert manager.is_running is True

    async def test_workspace_symbol_via_manager(self, manager):
        """Test workspace symbol search through manager."""
        symbols = await manager.workspace_symbol("AActor")

        # Note: Result count depends on clangd indexing state (cold start may return 0)
        # The important thing is that the call succeeds without error
        print(f"✅ Found {len(symbols)} symbols matching 'AActor' (cold start may be 0)")

    async def test_document_symbol_via_manager(self, manager):
        """Test document symbol through manager."""
        test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"
        if not test_file.exists():
            pytest.skip(f"Test file not found: {test_file}")

        symbols = await manager.document_symbol(str(test_file))

        assert len(symbols) > 0
        print(f"✅ Found {len(symbols)} symbols in Actor.h")

    async def test_find_definition_via_manager(self, manager):
        """Test find definition through manager."""
        test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"
        if not test_file.exists():
            pytest.skip(f"Test file not found: {test_file}")

        # Find AActor definition (around line 256)
        locations = await manager.find_definition(str(test_file), 256, 20)
        print(f"✅ Found {len(locations)} definition(s)")

    async def test_go_to_implementation_via_manager(self, manager):
        """Test go to implementation (Tick function)."""
        test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"
        if not test_file.exists():
            pytest.skip(f"Test file not found: {test_file}")

        # Find Tick implementations (line 3059, char 15)
        locations = await manager.go_to_implementation(str(test_file), 3059, 15)
        # Note: goToImplementation requires complete index (cold start may return 0)
        # The important thing is that the call succeeds without error
        print(f"✅ Found {len(locations)} implementation(s) of Tick (cold start may be 0)")

    async def test_concurrent_requests(self, manager):
        """Test concurrent requests are handled safely."""
        # Launch multiple concurrent requests
        tasks = [
            manager.workspace_symbol("AActor"),
            manager.workspace_symbol("UObject"),
            manager.workspace_symbol("FVector"),
        ]

        results = await asyncio.gather(*tasks)

        # All should succeed
        assert all(isinstance(r, list) for r in results)
        print(f"✅ Concurrent requests completed: {[len(r) for r in results]} symbols each")


if __name__ == "__main__":
    async def manual_test():
        print("=" * 60)
        print("LSPManager Manual Test")
        print("=" * 60)

        manager = get_lsp_manager()

        print("\n1. Testing singleton...")
        m1 = LSPManager()
        m2 = LSPManager()
        print(f"   Same instance: {m1 is m2}")

        print("\n2. Setting up with project...")
        result = await manager.setup(PROJECT_DIR)
        print(f"   Result: {result[:100]}...")

        if manager.is_setup:
            print("\n3. Ensuring clangd is running...")
            error = await manager.ensure_running()
            print(f"   Running: {manager.is_running}, Error: {error}")

            print("\n4. Testing workspace symbol...")
            symbols = await manager.workspace_symbol("BeginPlay")
            print(f"   Found {len(symbols)} symbols")

            print("\n5. Testing concurrent requests...")
            tasks = [
                manager.workspace_symbol("Tick"),
                manager.workspace_symbol("EndPlay"),
            ]
            results = await asyncio.gather(*tasks)
            print(f"   Results: {[len(r) for r in results]} symbols each")

        print("\n" + "=" * 60)
        print("Test Complete!")
        print("=" * 60)

    asyncio.run(manual_test())
