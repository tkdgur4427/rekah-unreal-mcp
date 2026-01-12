"""JSON-RPC protocol handling for LSP communication.

LSP uses JSON-RPC 2.0 over stdio with Content-Length headers:
  Content-Length: <length>\r\n
  \r\n
  <JSON payload>

This module handles buffering and parsing of these messages.
"""

import json
from typing import Optional, Dict, Any, List


class JSONRPCProtocol:
    """Handles JSON-RPC message parsing and formatting for LSP."""

    def __init__(self):
        self.buffer = b""

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        """Feed data and return complete messages.

        Args:
            data: Raw bytes from clangd stdout

        Returns:
            List of parsed JSON-RPC messages (may be empty if incomplete)
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
        """Try to parse a complete JSON-RPC message from buffer.

        Returns:
            Parsed message dict, or None if incomplete
        """
        # Find header end (double CRLF)
        header_end = self.buffer.find(b"\r\n\r\n")
        if header_end == -1:
            return None

        # Parse Content-Length header
        header = self.buffer[:header_end].decode("utf-8")
        content_length = None

        for line in header.split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":")[1].strip())
                break

        if content_length is None:
            # Malformed message, skip this header
            self.buffer = self.buffer[header_end + 4 :]
            return None

        # Check if we have complete content
        content_start = header_end + 4
        content_end = content_start + content_length

        if len(self.buffer) < content_end:
            # Not enough data yet
            return None

        # Extract and parse content
        content = self.buffer[content_start:content_end].decode("utf-8")
        self.buffer = self.buffer[content_end:]

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Invalid JSON, skip
            return None

    def encode(self, message: Dict[str, Any]) -> bytes:
        """Encode a JSON-RPC message with Content-Length header.

        Args:
            message: JSON-RPC message dict

        Returns:
            Encoded bytes ready to send to clangd stdin
        """
        content = json.dumps(message)
        content_bytes = content.encode("utf-8")
        header = f"Content-Length: {len(content_bytes)}\r\n\r\n"
        return header.encode("utf-8") + content_bytes

    def clear(self):
        """Clear the internal buffer."""
        self.buffer = b""
