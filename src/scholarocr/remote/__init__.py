"""Hosted (remote) mode of ScholarOCR: a public streamable-HTTP MCP server for ChatGPT apps.

Separate from ``scholarocr.mcp.server`` (the local stdio/localhost server), which is unchanged.
Remote mode never accepts filesystem paths: documents arrive as ChatGPT file parameters
(``download_url`` objects) or base64, are stored in a per-user sandbox, and are processed through
the same ``ScholarOCR`` service layer. Model API keys are bring-your-own by default.

Entry point: ``scholarocr-remote`` (see ``docs/CHATGPT_APP.md``).
"""
