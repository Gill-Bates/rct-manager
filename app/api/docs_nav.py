#!/usr/bin/env python3
#
# app/api/docs_nav.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Sidebar and light/dark theme toggle for the Swagger UI page.

The styles and script are shipped as self-hosted static files (``docs_static/docs-nav.css`` and
``docs_static/docs-nav.js``), served under ``/docs-assets``. The page only injects a ``<link>`` and
a ``<script src>`` so its ``default-src 'self'`` CSP needs no ``'unsafe-inline'`` exception; an
inline ``<style>``/``<script>`` would be blocked by a CSP-enforcing browser.
"""

from pathlib import Path

# Directory served under /docs-assets; app_factory mounts it next to the swagger-ui dist.
DOCS_STATIC_ROOT = Path(__file__).resolve().parent / "docs_static"

# Injected right before </body>. Both assets are same-origin, so default-src 'self' allows them.
SIDEBAR_HTML = (
    '<link rel="stylesheet" href="/docs-assets/docs-nav.css">'
    '<script src="/docs-assets/docs-nav.js"></script>'
)

# Full Swagger UI page, assembled from same-origin assets only. The SwaggerUIBundle bootstrap lives
# in /docs-assets/docs-init.js rather than an inline <script>, so FastAPI's default inline
# initialization (blocked by default-src 'self') is avoided and the page needs no CSP exception.
DOCS_PAGE = (
    "<!DOCTYPE html>"
    '<html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
    '<link type="text/css" rel="stylesheet" href="/docs-static/swagger-ui.css">'
    '<link rel="shortcut icon" href="/favicon.ico">'
    "<title>RCT Manager</title></head>"
    '<body><div id="swagger-ui"></div>'
    '<script src="/docs-static/swagger-ui-bundle.js"></script>'
    '<script src="/docs-assets/docs-init.js"></script>'
    + SIDEBAR_HTML
    + "</body></html>"
)


