#!/usr/bin/env python3
#
# app/admin/ui.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Server-rendered administration pages."""

import sys
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.admin.api import admin_session

_ROOT = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_ROOT / "templates"))
_PAGES = {"dashboard", "inverters", "tsdb", "prometheus", "tokens", "settings", "energy", "about"}
_CHANGELOG = _ROOT.parent.parent / "CHANGELOG.md"
_DEPENDENCIES = ("argon2-cffi", "cryptography", "fastapi", "jinja2", "pydantic", "pydantic-settings", "starlette", "uvicorn")
_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


class _AdminStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        # Dotfiles (e.g. local tool state such as .omc/) are never public.
        if any(part.startswith(".") for part in path.replace("\\", "/").split("/")):
            raise HTTPException(404)
        response = await super().get_response(path, scope)
        response.headers.update(_HEADERS)
        return response


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303, headers={**_HEADERS, "Cache-Control": "no-store"})


def _render(request: Request, template: str, context: dict):
    context = {"app_version": __version__, "copyright_year": datetime.now(UTC).year, **context}
    return _TEMPLATES.TemplateResponse(
        request, template, context, headers={**_HEADERS, "Cache-Control": "no-store"}
    )


def _destination(request: Request) -> str:
    session = admin_session(request)
    if session is None:
        return "/login"
    if session.get("must_change_password"):
        return "/change-password"
    return "/ui/dashboard"


def _changelog_sections() -> list[dict]:
    """Parse the Markdown changelog into releases of titled item lists; templates escape all text."""
    try:
        text = _CHANGELOG.read_text(encoding="utf-8")
    except OSError:
        return []
    releases: list[dict] = []
    group: dict | None = None
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("## "):
            group = None
            releases.append({"title": line[3:].replace("[", "").replace("]", ""), "groups": []})
        elif line.startswith("### ") and releases:
            group = {"title": line[4:], "items": []}
            releases[-1]["groups"].append(group)
        elif line.startswith("- ") and group is not None:
            group["items"].append([(part, index % 2 == 1) for index, part in enumerate(line[2:].split("`")) if part])
    return releases


def _about_context() -> dict:
    dependencies = []
    for package in _DEPENDENCIES:
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = "Not installed"
        dependencies.append((package, installed))
    return {
        "app_version": __version__,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "dependencies": dependencies,
        "changelog": _changelog_sections(),
    }


def install_ui(app: FastAPI) -> None:
    """Register administration pages and local static assets."""
    app.mount("/admin/static", _AdminStaticFiles(directory=str(_ROOT / "static")), name="admin-static")

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> RedirectResponse:
        return _redirect(_destination(request))

    @app.get("/login", include_in_schema=False)
    def login(request: Request):
        session = admin_session(request)
        if session is not None:
            return _redirect(_destination(request))
        return _render(request, "login.html", {"page": "login"})

    @app.get("/change-password", include_in_schema=False)
    def change_password(request: Request):
        session = admin_session(request)
        if session is None:
            return _redirect("/login")
        return _render(request, "change_password.html", {"page": "change-password", "must_change_password": session["must_change_password"]})

    @app.get("/ui/{page}", include_in_schema=False)
    def page(request: Request, page: str):
        if page not in _PAGES:
            return _redirect("/")
        destination = _destination(request)
        if destination != "/ui/dashboard":
            return _redirect(destination)
        context = {"page": page}
        if page == "about":
            context.update(_about_context())
        return _render(request, f"{page}.html", context)
