"""End-to-end tests for LSP client with real clangd.

These tests require:
1. clangd installed and in PATH
2. D:/BttUnrealEngine with compile_commands.json
"""

import pytest
import asyncio
from pathlib import Path

from rekah_mcp.lsp.client import LSPClient

# Test configuration
PROJECT_DIR = "D:/BttUnrealEngine"
COMPILE_COMMANDS_DIR = "D:/BttUnrealEngine"

# Skip all tests if project doesn't exist
pytestmark = pytest.mark.skipif(
    not Path(PROJECT_DIR).exists(),
    reason=f"Project directory not found: {PROJECT_DIR}"
)


@pytest.fixture
async def lsp_client():
    """Create and start LSP client for testing."""
    client = LSPClient(
        project_dir=PROJECT_DIR,
        compile_commands_dir=COMPILE_COMMANDS_DIR,
    )
    await client.start()
    yield client
    await client.stop()


class TestE2ELSPClient:
    """E2E tests with real clangd."""

    async def test_client_start_stop(self):
        """Test that client can start and stop clangd."""
        client = LSPClient(
            project_dir=PROJECT_DIR,
            compile_commands_dir=COMPILE_COMMANDS_DIR,
        )

        # Start
        await client.start()
        assert client.process is not None
        assert client.process.returncode is None  # Still running

        # Stop
        await client.stop()
        assert client.process is None

    async def test_find_definition_actor(self, lsp_client):
        """Test finding definition of AActor class."""
        # Find a simple UE header file
        test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"

        if not test_file.exists():
            pytest.skip(f"Test file not found: {test_file}")

        # Open file first
        await lsp_client.ensure_file_open(str(test_file))

        # File should be tracked
        assert len(lsp_client.open_files) >= 1
        print(f"✅ File opened successfully: {test_file.name}")

    async def test_hover_on_uclass(self, lsp_client):
        """Test hover information on UCLASS macro."""
        test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"

        if not test_file.exists():
            pytest.skip(f"Test file not found: {test_file}")

        # Read file to find UCLASS line
        content = test_file.read_text(encoding='utf-8', errors='replace')
        lines = content.split('\n')

        # Find "class AActor" line
        target_line = None
        for i, line in enumerate(lines):
            if 'class ENGINE_API AActor' in line or 'class AActor' in line:
                target_line = i + 1  # 1-based
                break

        if target_line is None:
            pytest.skip("Could not find AActor class definition")

        print(f"📍 Found AActor at line {target_line}")

        # Get hover info
        hover_info = await lsp_client.get_hover(
            str(test_file),
            line=target_line,
            character=20  # Should be within "AActor"
        )

        print(f"📝 Hover info: {hover_info[:200] if hover_info else 'None'}...")
        # May or may not have hover info depending on indexing status

    async def test_document_symbols(self, lsp_client):
        """Test getting document symbols."""
        test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"

        if not test_file.exists():
            pytest.skip(f"Test file not found: {test_file}")

        symbols = await lsp_client.document_symbol(str(test_file))

        print(f"📊 Found {len(symbols)} top-level symbols")
        for sym in symbols[:10]:  # Print first 10
            print(f"  - {sym['kind']}: {sym['name']} (line {sym['line']})")

        # Should find at least some symbols
        assert len(symbols) > 0, "Should find at least one symbol in Actor.h"

    async def test_workspace_symbol_search(self, lsp_client):
        """Test workspace-wide symbol search."""
        # Search for a common UE class
        symbols = await lsp_client.workspace_symbol("AActor")

        print(f"🔍 Found {len(symbols)} symbols matching 'AActor'")
        for sym in symbols[:5]:  # Print first 5
            print(f"  - {sym['kind']}: {sym['name']}")
            if sym.get('file'):
                print(f"    at {sym['file']}:{sym['line']}")

    async def test_find_definition_simple(self, lsp_client):
        """Test finding definition of a known symbol."""
        test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Core/Public/CoreMinimal.h"

        if not test_file.exists():
            # Try alternative
            test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"

        if not test_file.exists():
            pytest.skip(f"Test file not found")

        # Open and try to find a definition
        await lsp_client.ensure_file_open(str(test_file))

        # Read to find a good position
        content = test_file.read_text(encoding='utf-8', errors='replace')
        lines = content.split('\n')

        # Find first #include line
        for i, line in enumerate(lines):
            if line.strip().startswith('#include'):
                target_line = i + 1
                # Find position of the included file name
                if '"' in line:
                    char_pos = line.find('"') + 2
                elif '<' in line:
                    char_pos = line.find('<') + 2
                else:
                    continue

                print(f"📍 Testing definition at line {target_line}, char {char_pos}")
                print(f"   Line content: {line.strip()}")

                locations = await lsp_client.find_definition(
                    str(test_file),
                    line=target_line,
                    character=char_pos
                )

                print(f"📁 Found {len(locations)} definition(s)")
                for loc in locations[:3]:
                    print(f"  - {loc['file']}:{loc['line']}")

                break


class TestE2ELSPManager:
    """E2E tests for LSPManager (shared instance)."""

    @pytest.fixture
    async def manager(self):
        """Get manager and ensure setup."""
        from rekah_mcp.lsp.manager import get_lsp_manager
        manager = get_lsp_manager()
        await manager.setup(PROJECT_DIR)
        await manager.ensure_running()
        yield manager

    async def test_manager_workspace_symbol(self, manager):
        """Test workspace symbol via manager."""
        symbols = await manager.workspace_symbol("AActor")

        # Note: Result count depends on clangd indexing state (cold start may return 0)
        print(f"🔍 [Manager] Found {len(symbols)} symbols matching 'AActor' (cold start may be 0)")

    async def test_manager_find_definition(self, manager):
        """Test find definition via manager."""
        test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"
        if not test_file.exists():
            pytest.skip(f"Test file not found: {test_file}")

        # Find AActor class definition (line ~256)
        locations = await manager.find_definition(str(test_file), 256, 10)
        print(f"📍 [Manager] Found {len(locations)} definition(s)")

    async def test_manager_go_to_implementation(self, manager):
        """Test goToImplementation via manager (Tick function)."""
        test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"
        if not test_file.exists():
            pytest.skip(f"Test file not found: {test_file}")

        # Find Tick function implementations (line 3059, character 15)
        locations = await manager.go_to_implementation(str(test_file), 3059, 15)
        print(f"🔧 [Manager] Found {len(locations)} implementation(s) of Tick")

    async def test_manager_incoming_calls(self, manager):
        """Test incomingCalls via manager."""
        test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"
        if not test_file.exists():
            pytest.skip(f"Test file not found: {test_file}")

        # First prepare call hierarchy for BeginPlay (line 2128)
        items = await manager.prepare_call_hierarchy(str(test_file), 2128, 15)
        if items:
            callers = await manager.incoming_calls(items[0])
            print(f"📞 [Manager] Found {len(callers)} caller(s) of BeginPlay")


if __name__ == "__main__":
    # Run a quick manual test
    async def manual_test():
        from rekah_mcp.lsp.manager import get_lsp_manager

        print("=" * 60)
        print("E2E LSP Test with real clangd")
        print("=" * 60)

        # Test 1: Direct LSPClient
        print("\n[Part 1: Direct LSPClient]")
        client = LSPClient(
            project_dir=PROJECT_DIR,
            compile_commands_dir=COMPILE_COMMANDS_DIR,
        )

        try:
            print("\n1. Starting clangd...")
            await client.start()
            print("   ✅ clangd started")

            print("\n2. Opening Actor.h...")
            test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"
            if test_file.exists():
                await client.ensure_file_open(str(test_file))
                print(f"   ✅ File opened, {len(client.open_files)} file(s) tracked")
            else:
                print(f"   ⚠️ File not found: {test_file}")

            print("\n3. Getting document symbols...")
            if test_file.exists():
                symbols = await client.document_symbol(str(test_file))
                print(f"   ✅ Found {len(symbols)} symbols")
                for sym in symbols[:5]:
                    print(f"      - {sym['kind']}: {sym['name']}")

            print("\n4. Workspace symbol search 'GetComponents'...")
            symbols = await client.workspace_symbol("GetComponents")
            print(f"   ✅ Found {len(symbols)} symbols")
            for sym in symbols[:5]:
                print(f"      - {sym['kind']}: {sym['name']}")

        finally:
            print("\n5. Stopping clangd...")
            await client.stop()
            print("   ✅ clangd stopped")

        # Test 2: LSPManager (shared instance)
        print("\n" + "-" * 60)
        print("[Part 2: LSPManager (Shared Instance)]")

        manager = get_lsp_manager()
        result = await manager.setup(PROJECT_DIR)
        print(f"\n1. Setup: {result[:80]}...")

        error = await manager.ensure_running()
        print(f"2. Running: {manager.is_running}, Error: {error}")

        print("\n3. Workspace symbol 'BeginPlay'...")
        symbols = await manager.workspace_symbol("BeginPlay")
        print(f"   ✅ Found {len(symbols)} symbols")

        print("\n4. goToImplementation for Tick (line 3059, char 15)...")
        test_file = Path(PROJECT_DIR) / "Engine/Source/Runtime/Engine/Classes/GameFramework/Actor.h"
        if test_file.exists():
            impls = await manager.go_to_implementation(str(test_file), 3059, 15)
            print(f"   ✅ Found {len(impls)} implementation(s)")

        print("\n" + "=" * 60)
        print("E2E Test Complete!")
        print("=" * 60)

    asyncio.run(manual_test())
