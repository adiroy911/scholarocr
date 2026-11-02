"""ScholarOCR: a reusable academic-document OCR and intelligence engine.

The core package has no dependency on any particular AI agent. Agents reach it through
the MCP server (``scholarocr.mcp.server``) or the CLI (``scholarocr.cli.main``); Python
callers use :class:`scholarocr.service.ScholarOCR`.
"""

__version__ = "0.1.0"

# Bump when extraction semantics change; part of every cache key.
EXTRACTION_VERSION = "1"
SCHEMA_VERSION = "1"

__all__ = ["__version__", "EXTRACTION_VERSION", "SCHEMA_VERSION"]
