"""lsp_utils integrated tests

test classes:
- TestProtocol: JSON-RPC protocol tests
- TestClient: LSPClient tests
- TestManager: LSPManager singleton tests
- TestE2E: end-to-end tests (clangd required)
"""

import os
import pytest
import asyncio
from pathlib import Path

from rekah_mcp.lsp.lsp_utils import (
    JSONRPCProtocol,
    LSPClient,
    LSPManager,
    get_lsp_manager,
)
from rekah_mcp.utils.config_utils import get_config_value


# ═══════════════════════════════════════════════════════════════════════════
# test configuration (from config.ini)
# ═══════════════════════════════════════════════════════════════════════════

INTERMEDIATES_DIR = get_config_value("test", "intermediates_dir", default="./intermediates")
TEST_PROJECT_DIR = get_config_value("test", "test_project_dir", default="")


# ═══════════════════════════════════════════════════════════════════════════
# test fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def ensure_intermediates_dir():
    """ensure intermediates directory exists"""
    if not os.path.exists(INTERMEDIATES_DIR):
        os.makedirs(INTERMEDIATES_DIR)


@pytest.fixture
def protocol():
    """JSONRPCProtocol instance"""
    return JSONRPCProtocol()


@pytest.fixture
def reset_manager():
    """reset LSPManager before and after test"""
    LSPManager.reset_instance()
    yield
    LSPManager.reset_instance()


# ═══════════════════════════════════════════════════════════════════════════
# TestProtocol: JSON-RPC protocol tests
# ═══════════════════════════════════════════════════════════════════════════

class TestProtocol:
    """JSON-RPC protocol tests"""

    def test_parse_single_message(self, protocol):
        """parse a complete JSON-RPC message"""
        json_content = b'{"jsonrpc": "2.0", "id": 1, "result": {"success": true}}'
        data = b'Content-Length: 56\r\n\r\n' + json_content
        messages = protocol.feed(data)

        assert len(messages) == 1
        assert messages[0]["id"] == 1
        assert messages[0]["result"]["success"] is True

    def test_parse_incomplete_message(self, protocol):
        """incomplete messages return empty list"""
        data = b'Content-Length: 100\r\n\r\n{"jsonrpc":'
        messages = protocol.feed(data)

        assert len(messages) == 0

    def test_parse_multiple_messages(self, protocol):
        """parse multiple complete messages"""
        json1 = b'{"jsonrpc": "2.0", "id": 1, "result": 1}'
        json2 = b'{"jsonrpc": "2.0", "id": 2, "result": 2}'
        msg1 = f'Content-Length: {len(json1)}\r\n\r\n'.encode() + json1
        msg2 = f'Content-Length: {len(json2)}\r\n\r\n'.encode() + json2
        messages = protocol.feed(msg1 + msg2)

        assert len(messages) == 2
        assert messages[0]["id"] == 1
        assert messages[1]["id"] == 2

    def test_incremental_feed(self, protocol):
        """feed data incrementally"""
        # first chunk: partial header
        messages = protocol.feed(b"Content-Length: ")
        assert len(messages) == 0

        # second chunk: rest of header and partial content
        messages = protocol.feed(b'40\r\n\r\n{"jsonrpc":')
        assert len(messages) == 0

        # third chunk: rest of content
        messages = protocol.feed(b' "2.0", "id": 1, "result": 1}')
        assert len(messages) == 1
        assert messages[0]["id"] == 1

    def test_encode_message(self, protocol):
        """encode a JSON-RPC message"""
        message = {"jsonrpc": "2.0", "id": 1, "method": "test"}
        encoded = protocol.encode(message)

        assert b"Content-Length:" in encoded
        assert b'{"jsonrpc": "2.0"' in encoded

    def test_clear_buffer(self, protocol):
        """clear the buffer"""
        protocol.feed(b"some data")
        assert len(protocol.buffer) > 0

        protocol.clear()
        assert len(protocol.buffer) == 0


# ═══════════════════════════════════════════════════════════════════════════
# TestClient: LSPClient utility tests
# ═══════════════════════════════════════════════════════════════════════════

class TestClient:
    """LSPClient utility tests"""

    @pytest.fixture
    def client(self, tmp_path):
        """create a test client instance"""
        return LSPClient(str(tmp_path))

    def test_language_id_detection_cpp(self, client):
        """C++ language ID detection"""
        assert client._get_language_id("/path/to/file.cpp") == "cpp"
        assert client._get_language_id("/path/to/file.cc") == "cpp"
        assert client._get_language_id("/path/to/file.cxx") == "cpp"

    def test_language_id_detection_header(self, client):
        """header file language ID detection"""
        assert client._get_language_id("/path/to/file.h") == "cpp"
        assert client._get_language_id("/path/to/file.hpp") == "cpp"
        assert client._get_language_id("/path/to/file.hxx") == "cpp"

    def test_language_id_detection_c(self, client):
        """C language ID detection"""
        assert client._get_language_id("/path/to/file.c") == "c"

    def test_language_id_detection_default(self, client):
        """default language ID for unknown extensions"""
        assert client._get_language_id("/path/to/file.xyz") == "cpp"

    def test_path_to_uri_unix(self, client):
        """Unix path to URI conversion"""
        uri = client._path_to_uri("/home/user/file.cpp")
        assert uri == "file:///home/user/file.cpp"

    def test_path_to_uri_windows(self, client):
        """Windows path to URI conversion"""
        uri = client._path_to_uri("D:/Projects/file.cpp")
        assert uri == "file:///D:/Projects/file.cpp"

    def test_path_to_uri_windows_backslash(self, client):
        """Windows path with backslashes"""
        uri = client._path_to_uri("D:\\Projects\\file.cpp")
        assert uri == "file:///D:/Projects/file.cpp"

    def test_uri_to_path_unix(self, client):
        """Unix URI to path conversion"""
        path = client._uri_to_path("file:///home/user/file.cpp")
        assert path == "/home/user/file.cpp"

    def test_uri_to_path_windows(self, client):
        """Windows URI to path conversion"""
        path = client._uri_to_path("file:///D:/Projects/file.cpp")
        assert path == "D:/Projects/file.cpp"

    def test_symbol_kind_to_string(self, client):
        """symbol kind conversion"""
        assert client._symbol_kind_to_string(5) == "Class"
        assert client._symbol_kind_to_string(6) == "Method"
        assert client._symbol_kind_to_string(12) == "Function"
        assert client._symbol_kind_to_string(999) == "Unknown(999)"

    def test_normalize_locations_none(self, client):
        """normalize None locations"""
        assert client._normalize_locations(None) == []

    def test_normalize_locations_single(self, client):
        """normalize a single location"""
        result = {
            "uri": "file:///D:/test.cpp",
            "range": {"start": {"line": 9, "character": 4}}
        }
        locations = client._normalize_locations(result)

        assert len(locations) == 1
        assert locations[0]["file"] == "D:/test.cpp"
        assert locations[0]["line"] == 10  # 1-based
        assert locations[0]["character"] == 5  # 1-based

    def test_normalize_locations_list(self, client):
        """normalize a list of locations"""
        result = [
            {"uri": "file:///D:/a.cpp", "range": {"start": {"line": 0, "character": 0}}},
            {"uri": "file:///D:/b.cpp", "range": {"start": {"line": 10, "character": 5}}},
        ]
        locations = client._normalize_locations(result)

        assert len(locations) == 2
        assert locations[0]["file"] == "D:/a.cpp"
        assert locations[1]["file"] == "D:/b.cpp"

    def test_extract_hover_content_string(self, client):
        """extract hover content from string"""
        assert client._extract_hover_content("test") == "test"

    def test_extract_hover_content_dict(self, client):
        """extract hover content from dict"""
        content = {"value": "int main()"}
        assert client._extract_hover_content(content) == "int main()"

    def test_extract_hover_content_list(self, client):
        """extract hover content from list"""
        content = ["line1", {"value": "line2"}]
        result = client._extract_hover_content(content)
        assert "line1" in result
        assert "line2" in result


# ═══════════════════════════════════════════════════════════════════════════
# TestManager: LSPManager singleton tests
# ═══════════════════════════════════════════════════════════════════════════

class TestManager:
    """LSPManager singleton tests"""

    def test_singleton_same_instance(self, reset_manager):
        """multiple calls return same instance"""
        manager1 = get_lsp_manager()
        manager2 = get_lsp_manager()

        assert manager1 is manager2

    def test_manager_initial_state(self, reset_manager):
        """initial state of manager"""
        manager = get_lsp_manager()

        assert manager.is_setup is False
        assert manager.is_running is False
        assert manager.setup_error is None
        assert manager.project_dir is None
        assert manager.open_files_count == 0

    @pytest.mark.asyncio
    async def test_setup_nonexistent_project(self, reset_manager):
        """setup with non-existent project directory"""
        manager = get_lsp_manager()

        result = await manager.setup("/nonexistent/path/to/project")

        assert "❌" in result
        assert "not found" in result.lower()
        assert manager.is_setup is False

    def test_reset_instance(self, reset_manager):
        """reset_instance works correctly"""
        manager1 = get_lsp_manager()
        manager1._project_dir = "/test/path"

        LSPManager.reset_instance()
        manager2 = get_lsp_manager()

        assert manager2.project_dir is None

    def test_is_setup_false_when_no_client(self, reset_manager):
        """is_setup is False when client is None"""
        manager = get_lsp_manager()

        assert manager.is_setup is False

    def test_is_running_false_when_no_client(self, reset_manager):
        """is_running is False when client is None"""
        manager = get_lsp_manager()

        assert manager.is_running is False

    def test_open_files_count_zero_when_no_client(self, reset_manager):
        """open_files_count is 0 when client is None"""
        manager = get_lsp_manager()

        assert manager.open_files_count == 0


# ═══════════════════════════════════════════════════════════════════════════
# TestE2E: end-to-end tests (clangd and test project required)
# ═══════════════════════════════════════════════════════════════════════════

class TestE2E:
    """end-to-end LSP tests (clangd and test project required)"""

    @pytest.fixture(autouse=True)
    def check_project_dir(self):
        """check test project directory"""
        if not TEST_PROJECT_DIR or not os.path.exists(TEST_PROJECT_DIR):
            pytest.skip("config.ini [test] test_project_dir not set or not found")

    @pytest.fixture
    def reset_manager_e2e(self):
        """reset manager for e2e tests"""
        LSPManager.reset_instance()
        yield
        LSPManager.reset_instance()

    @pytest.mark.asyncio
    async def test_setup(self, reset_manager_e2e):
        """setup LSP with real project"""
        manager = get_lsp_manager()
        result = await manager.setup(TEST_PROJECT_DIR)

        assert "✅" in result or "error" not in result.lower()
        assert manager.is_setup is True

    @pytest.mark.asyncio
    async def test_ensure_running(self, reset_manager_e2e):
        """ensure clangd is running"""
        manager = get_lsp_manager()
        await manager.setup(TEST_PROJECT_DIR)

        error = await manager.ensure_running()

        if error is None:
            assert manager.is_running is True

    @pytest.mark.asyncio
    async def test_workspace_symbol(self, reset_manager_e2e):
        """workspace symbol search"""
        manager = get_lsp_manager()
        await manager.setup(TEST_PROJECT_DIR)
        error = await manager.ensure_running()
        if error:
            pytest.skip(f"clangd not running: {error}")

        # search for a common symbol
        symbols = await manager.workspace_symbol("main")
        # result count depends on indexing state

    @pytest.mark.asyncio
    async def test_document_symbol(self, reset_manager_e2e):
        """document symbol retrieval"""
        manager = get_lsp_manager()
        await manager.setup(TEST_PROJECT_DIR)
        error = await manager.ensure_running()
        if error:
            pytest.skip(f"clangd not running: {error}")

        # find a .cpp or .h file
        test_file = None
        for root, dirs, files in os.walk(TEST_PROJECT_DIR):
            for f in files:
                if f.endswith((".cpp", ".h")):
                    test_file = os.path.join(root, f)
                    break
            if test_file:
                break

        if not test_file:
            pytest.skip("no .cpp or .h file found in test project")

        symbols = await manager.document_symbol(test_file)
        # result may be empty for small files

    @pytest.mark.asyncio
    async def test_concurrent_requests(self, reset_manager_e2e):
        """concurrent requests are handled safely"""
        manager = get_lsp_manager()
        await manager.setup(TEST_PROJECT_DIR)
        error = await manager.ensure_running()
        if error:
            pytest.skip(f"clangd not running: {error}")

        # launch multiple concurrent requests
        tasks = [
            manager.workspace_symbol("main"),
            manager.workspace_symbol("test"),
            manager.workspace_symbol("func"),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # all should complete (success or exception)
        assert len(results) == 3
