"""ScholarOCR's standalone web app (``scholarocr-app``): a local, single-user chat-style interface.

Runs on 127.0.0.1 only. The default engine is Antigravity CLI (``agy``) signed in with the user's own
Google account: the app runs ScholarOCR's deterministic pipeline in host mode and hands each page that needs
vision to ``agy -p``. A Gemini or OpenRouter key (stored locally) switches to the server-side pipeline.
See docs/WEB_APP.md.
"""
