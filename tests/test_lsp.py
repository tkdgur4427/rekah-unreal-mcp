"""Unit tests for LSP module."""

import pytest
from rekah_unreal_mcp.lsp.protocol import JSONRPCProtocol
from rekah_unreal_mcp.lsp.client import LSPClient


class TestJSONRPCProtocol:
    """Tests for JSON-RPC protocol handling."""

    def test_parse_single_message(self):
        """Test parsing a complete JSON-RPC message."""
        protocol = JSONRPCProtocol()
        # JSON: {"jsonrpc": "2.0", "id": 1, "result": {"success": true}} = 56 bytes
        json_content = b'{"jsonrpc": "2.0", "id": 1, "result": {"success": true}}'
        data = b'Content-Length: 56\r\n\r\n' + json_content
        messages = protocol.feed(data)

        assert len(messages) == 1
        assert messages[0]["id"] == 1
        assert messages[0]["result"]["success"] is True

    def test_parse_incomplete_message(self):
        """Test that incomplete messages return empty list."""
        protocol = JSONRPCProtocol()
        data = b'Content-Length: 100\r\n\r\n{"jsonrpc":'
        messages = protocol.feed(data)

        assert len(messages) == 0

    def test_parse_multiple_messages(self):
        """Test parsing multiple complete messages."""
        protocol = JSONRPCProtocol()
        # Each JSON is 44 bytes: {"jsonrpc": "2.0", "id": 1, "result": 1}
        json1 = b'{"jsonrpc": "2.0", "id": 1, "result": 1}'
        json2 = b'{"jsonrpc": "2.0", "id": 2, "result": 2}'
        msg1 = f'Content-Length: {len(json1)}\r\n\r\n'.encode() + json1
        msg2 = f'Content-Length: {len(json2)}\r\n\r\n'.encode() + json2
        messages = protocol.feed(msg1 + msg2)

        assert len(messages) == 2
        assert messages[0]["id"] == 1
        assert messages[1]["id"] == 2

    def test_incremental_feed(self):
        """Test feeding data incrementally."""
        protocol = JSONRPCProtocol()
        # Full JSON: {"jsonrpc": "2.0", "id": 1, "result": 1} = 40 bytes

        # First chunk: partial header
        messages = protocol.feed(b"Content-Length: ")
        assert len(messages) == 0

        # Second chunk: rest of header and partial content
        messages = protocol.feed(b'40\r\n\r\n{"jsonrpc":')
        assert len(messages) == 0

        # Third chunk: rest of content
        messages = protocol.feed(b' "2.0", "id": 1, "result": 1}')
        assert len(messages) == 1
        assert messages[0]["id"] == 1

    def test_encode_message(self):
        """Test encoding a JSON-RPC message."""
        protocol = JSONRPCProtocol()
        message = {"jsonrpc": "2.0", "id": 1, "method": "test"}
        encoded = protocol.encode(message)

        assert b"Content-Length:" in encoded
        assert b'{"jsonrpc": "2.0"' in encoded

    def test_clear_buffer(self):
        """Test clearing the buffer."""
        protocol = JSONRPCProtocol()
        protocol.feed(b"some data")
        assert len(protocol.buffer) > 0

        protocol.clear()
        assert len(protocol.buffer) == 0


class TestLSPClientUtilities:
    """Tests for LSP client utility methods."""

    @pytest.fixture
    def client(self, tmp_path):
        """Create a test client instance."""
        return LSPClient(str(tmp_path))

    def test_language_id_detection_cpp(self, client):
        """Test C++ language ID detection."""
        assert client._get_language_id("/path/to/file.cpp") == "cpp"
        assert client._get_language_id("/path/to/file.cc") == "cpp"
        assert client._get_language_id("/path/to/file.cxx") == "cpp"

    def test_language_id_detection_header(self, client):
        """Test header file language ID detection."""
        assert client._get_language_id("/path/to/file.h") == "cpp"
        assert client._get_language_id("/path/to/file.hpp") == "cpp"
        assert client._get_language_id("/path/to/file.hxx") == "cpp"

    def test_language_id_detection_c(self, client):
        """Test C language ID detection."""
        assert client._get_language_id("/path/to/file.c") == "c"

    def test_language_id_detection_default(self, client):
        """Test default language ID for unknown extensions."""
        assert client._get_language_id("/path/to/file.xyz") == "cpp"

    def test_path_to_uri_unix(self, client):
        """Test Unix path to URI conversion."""
        uri = client._path_to_uri("/home/user/file.cpp")
        assert uri == "file:///home/user/file.cpp"

    def test_path_to_uri_windows(self, client):
        """Test Windows path to URI conversion."""
        uri = client._path_to_uri("D:/Projects/file.cpp")
        assert uri == "file:///D:/Projects/file.cpp"

    def test_path_to_uri_windows_backslash(self, client):
        """Test Windows path with backslashes."""
        uri = client._path_to_uri("D:\\Projects\\file.cpp")
        assert uri == "file:///D:/Projects/file.cpp"

    def test_uri_to_path_unix(self, client):
        """Test Unix URI to path conversion."""
        path = client._uri_to_path("file:///home/user/file.cpp")
        assert path == "/home/user/file.cpp"

    def test_uri_to_path_windows(self, client):
        """Test Windows URI to path conversion."""
        path = client._uri_to_path("file:///D:/Projects/file.cpp")
        assert path == "D:/Projects/file.cpp"

    def test_symbol_kind_to_string(self, client):
        """Test symbol kind conversion."""
        assert client._symbol_kind_to_string(5) == "Class"
        assert client._symbol_kind_to_string(6) == "Method"
        assert client._symbol_kind_to_string(12) == "Function"
        assert client._symbol_kind_to_string(999) == "Unknown(999)"

    def test_normalize_locations_none(self, client):
        """Test normalizing None locations."""
        assert client._normalize_locations(None) == []

    def test_normalize_locations_single(self, client):
        """Test normalizing a single location."""
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
        """Test normalizing a list of locations."""
        result = [
            {"uri": "file:///D:/a.cpp", "range": {"start": {"line": 0, "character": 0}}},
            {"uri": "file:///D:/b.cpp", "range": {"start": {"line": 10, "character": 5}}},
        ]
        locations = client._normalize_locations(result)

        assert len(locations) == 2
        assert locations[0]["file"] == "D:/a.cpp"
        assert locations[1]["file"] == "D:/b.cpp"

    def test_extract_hover_content_string(self, client):
        """Test extracting hover content from string."""
        assert client._extract_hover_content("test") == "test"

    def test_extract_hover_content_dict(self, client):
        """Test extracting hover content from dict."""
        content = {"value": "int main()"}
        assert client._extract_hover_content(content) == "int main()"

    def test_extract_hover_content_list(self, client):
        """Test extracting hover content from list."""
        content = ["line1", {"value": "line2"}]
        result = client._extract_hover_content(content)
        assert "line1" in result
        assert "line2" in result
