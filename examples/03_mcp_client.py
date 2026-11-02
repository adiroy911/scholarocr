"""Example 3 — any MCP client: launch the ScholarOCR server over stdio and call its tools,
exactly as Claude Code or Gemini CLI would.

    python examples/03_mcp_client.py path/to/document.pdf
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters


async def main(path: Path) -> None:
    env = {**os.environ, "SCHOLAROCR_ALLOWED_DIRS": str(path.parent)}
    params = StdioServerParameters(command=sys.executable, args=["-m", "scholarocr.mcp.server"], env=env)
    async with Client(params) as client:
        tools = await client.list_tools()
        print("tools:", ", ".join(t.name for t in tools.tools))
        r = await client.call_tool("ocr_document", {"file_path": str(path), "wait": True})
        if r.is_error:
            raise SystemExit(r.content[0].text)
        doc_id = r.structured_content["document_id"]
        print("status:", r.structured_content["status"])
        content = await client.call_tool("get_document_content", {"document_id": doc_id, "max_chars": 2000})
        print(content.structured_content["content"][:1200])
        ver = await client.call_tool("verify_document", {"document_id": doc_id})
        print("verification counts:", json.dumps(ver.structured_content["counts"]))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
