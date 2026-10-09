#!/usr/bin/env python3
#
# tests/test_admin_design_rules.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Design rules of the administration GUI as static checks (no browser).

Each rule is a function that returns violations as "file:line: rule - how to fix"; the real sources
must produce none, and a deliberately violating sample must produce at least one, so a rule that
silently stops matching fails here instead of letting the design drift. Badge rules live in
test_admin_design_rules_badges.py.

Rules:
 R1  The TSDB page has no own layout or typography CSS: the only `.tsdb-*` selectors are the log
     console's. Use settings-grid, card-body, h2.h5, setting-row and form-text instead.
 R2  Section titles are h2 with the `h5` class (About keeps its h6 headers until it is harmonised)
     and TSDB card titles are sentence case (names such as InfluxDB and "Log" are whitelisted).
 R3  The TSDB cards carry no decorative icons, and the TSDB page shows no yellow banner: an
     incomplete form is reported by a toast plus aria-invalid on the field.
 R4  The admin scripts never open the native "leave page" dialog (beforeunload).
 R5  The TSDB action bar is exactly one `btn btn-primary` "Apply changes" button, requested through
     the shared bar's `compact` option; there is no Discard, count badge or hint text.
 R6  Card grids define at most four columns (`.settings-grid`), a card may span a whole row via the
     shared `.card-span-all`.
 R7  `.btn-primary` themes every state, so no Bootstrap blue shows through (the disabled state used to).
 R8  Colours in the TSDB, settings-grid and primary-button rules come from tokens (var(--...)),
     except the log console's terminal palette (listed in R8_ALLOWED_SELECTORS).
 R9  TSDB fields are built by the shared buildFieldControl() (setting-row, form-text), not by own
     wrappers.
"""

import re
from pathlib import Path

ADMIN = Path(__file__).resolve().parents[1] / "app" / "admin"
CSS_FILE = ADMIN / "static" / "css" / "admin.css"
JS_DIR = ADMIN / "static" / "js"
TEMPLATES = ADMIN / "templates"

R1_ALLOWED = {"tsdb-console", "tsdb-console-line", "tsdb-console-time"}
R2_PENDING_PAGES = {"about.html"}  # h6 card headers, harmonised separately
R2_NAMES = {"TSDB", "InfluxDB", "QuestDB", "Log", "Prometheus"}
R8_ALLOWED_SELECTORS = {".tsdb-console", ".tsdb-console-time"}


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _rules(css: str):
    """Yield (selector, body, line) for every rule that has a body; comments are blanked, lines kept."""
    css = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), css, flags=re.DOTALL)
    for match in re.finditer(r"([^{}@][^{}]*)\{([^{}]*)\}", css):
        yield " ".join(match.group(1).split()), match.group(2), _line(css, match.start())


def check_r1_tsdb_css(css: str, name: str = "admin.css") -> list[str]:
    found = set(re.findall(r"\.((?:tsdb|export)-[a-z-]+)", css))
    return [
        f"{name}: R1 - TSDB-specific class .{cls}: use the shared settings grid, cards and setting rows"
        for cls in sorted(found - R1_ALLOWED)
    ]


def check_r2_titles(templates: dict[str, str], js: str) -> list[str]:
    problems = []
    for file, text in templates.items():
        if file in R2_PENDING_PAGES:
            continue
        for match in re.finditer(r"<h2\b([^>]*)>", text):
            classes = re.search(r'class="([^"]*)"', match.group(1))
            if not classes or "h5" not in classes.group(1).split():
                problems.append(f"{file}:{_line(text, match.start())}: R2 - h2 without class h5: use <h2 class=\"h5 ...\">")
    for match in re.finditer(r"(?:card|groupCard)\('[A-Za-z]+',(?: '[A-Za-z]+',)? '([^']+)'", js):
        words = re.findall(r"[A-Za-z][A-Za-z-]*", match.group(1))
        for word in words[1:]:
            if word[0].isupper() and word not in R2_NAMES:
                problems.append(f"admin.js:{_line(js, match.start())}: R2 - title '{match.group(1)}' is not sentence case ({word})")
    return problems


def check_r3_tsdb_markup(render: str, template: str) -> list[str]:
    problems = []
    for needle, why in (("material-icons", "decorative icon in a card"), ("alert-warning", "yellow banner"), ("save-hint", "banner notice")):
        if needle in render or needle in template:
            problems.append(f"tsdb render/template: R3 - {needle} ({why}); use the toast and aria-invalid instead")
    return problems


def check_r4_unload(scripts: dict[str, str]) -> list[str]:
    return [
        f"{file}:{_line(text, m.start())}: R4 - {m.group(0)}: the GUI must not open the browser's leave-page dialog"
        for file, text in scripts.items()
        for m in re.finditer(r"onbeforeunload|beforeunload", text)
    ]


def check_r5_apply_bar(js: str, template: str) -> list[str]:
    problems = []
    builder = js[js.index("function buildApplyBar("):js.index("function buildDeviceApplyBar(")]
    compact = builder[builder.index("if (compact) {"):builder.index("const discard = element(")]
    if "'Discard'" in compact or "badge" in compact or "status" in compact:
        problems.append("admin.js: R5 - the compact apply bar must hold the button only")
    if "element('button', 'btn btn-primary', applyLabel)" not in builder:
        problems.append("admin.js: R5 - the apply button must use the shared primary class (btn btn-primary)")
    if "applyLabel = 'Apply changes'" not in builder:
        problems.append("admin.js: R5 - the apply label must be exactly 'Apply changes'")
    if "buildApplyBar('export', {" not in js or "compact: true," not in js:
        problems.append("admin.js: R5 - the TSDB form must request the compact apply bar")
    if 'id="export-actions"' not in template:
        problems.append("tsdb.html: R5 - missing #export-actions container")
    return problems


def check_r6_grid(css: str) -> list[str]:
    for selector, body, line in _rules(css):
        if selector != ".settings-grid":
            continue
        columns = re.search(r"grid-template-columns:\s*([^;]+);", body)
        if not columns:
            return [f"admin.css:{line}: R6 - .settings-grid has no grid-template-columns"]
        value = columns.group(1)
        fixed = re.search(r"repeat\((\d+)\b", value)
        if fixed and int(fixed.group(1)) > 4:
            return [f"admin.css:{line}: R6 - {fixed.group(1)} columns: a card row holds at most four"]
        if not fixed and "/ 4)" not in value:
            return [f"admin.css:{line}: R6 - .settings-grid tracks must not be narrower than a quarter of the row (calc(... / 4))"]
        return []
    return ["admin.css: R6 - .settings-grid rule not found"]


def check_r7_primary_button(css: str) -> list[str]:
    for selector, body, line in _rules(css):
        if selector == ".btn-primary":
            missing = [v for v in ("--bs-btn-disabled-bg", "--bs-btn-disabled-border-color", "--bs-btn-focus-shadow-rgb") if v not in body]
            return [f"admin.css:{line}: R7 - .btn-primary does not set {v} (Bootstrap blue would show)" for v in missing]
    return ["admin.css: R7 - .btn-primary rule not found"]


def check_r8_tokens(css: str) -> list[str]:
    problems = []
    for selector, body, line in _rules(css):
        watched = ".settings-grid" in selector or ".btn-primary" in selector or re.search(r"\.(tsdb|export)-", selector)
        if not watched or any(allowed in selector for allowed in R8_ALLOWED_SELECTORS):
            continue
        for color in re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", body):
            problems.append(f"admin.css:{line}: R8 - hard-coded colour {color} in '{selector}': use a design token var(--...)")
    return problems


def check_r9_shared_fields(js: str) -> list[str]:
    control = js[js.index("function exportControl("):js.index("// _settings_view() never exposes")]
    problems = []
    if "buildFieldControl(field, settingsDraft[field.key])" not in control:
        problems.append("admin.js: R9 - exportControl must build fields with buildFieldControl")
    if re.search(r"element\('div', 'tsdb-", control) or "tsdb-field" in control:
        problems.append("admin.js: R9 - own TSDB field wrappers: use the shared setting-row")
    return problems


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tsdb_render(js: str) -> str:
    return js[js.index("function renderExportSettings("):js.index("// Mirrors the server-side host plausibility")]


def test_r1_tsdb_has_no_own_layout_or_typography_css() -> None:
    assert check_r1_tsdb_css(_read(CSS_FILE)) == []
    assert check_r1_tsdb_css(".tsdb-section-copy h2 { font-weight: 700; }")


def test_r2_section_titles_are_h5_and_sentence_case() -> None:
    templates = {path.name: _read(path) for path in TEMPLATES.glob("*.html")}
    js = _read(JS_DIR / "admin.js")
    assert check_r2_titles(templates, js) == []
    assert check_r2_titles({"x.html": '<h2 class="h6">Title</h2>'}, "")
    assert check_r2_titles({}, "card('x', 'Target Database Settings', [])")


def test_r3_tsdb_has_no_icons_and_no_banner() -> None:
    js = _read(JS_DIR / "admin.js")
    assert check_r3_tsdb_markup(_tsdb_render(js), _read(TEMPLATES / "tsdb.html")) == []
    assert check_r3_tsdb_markup("body.append(element('span', 'material-icons', 'link'))", "")
    assert check_r3_tsdb_markup("", '<div class="alert alert-warning">')


def test_r4_no_native_leave_page_dialog() -> None:
    scripts = {path.name: _read(path) for path in JS_DIR.glob("*.js")}
    assert check_r4_unload(scripts) == []
    assert check_r4_unload({"x.js": "window.addEventListener('beforeunload', f);"})


def test_r5_tsdb_apply_bar_is_one_primary_button() -> None:
    js = _read(JS_DIR / "admin.js")
    template = _read(TEMPLATES / "tsdb.html")
    assert check_r5_apply_bar(js, template) == []
    broken = js.replace("element('button', 'btn btn-primary', applyLabel)", "element('button', 'btn btn-info', applyLabel)")
    assert check_r5_apply_bar(broken, template)


def test_r6_card_grids_have_at_most_four_columns() -> None:
    assert check_r6_grid(_read(CSS_FILE)) == []
    assert check_r6_grid(".settings-grid { grid-template-columns: repeat(5, 1fr); }")
    assert check_r6_grid(".settings-grid { grid-template-columns: repeat(auto-fit, minmax(19rem, 1fr)); }")


def test_r7_primary_button_themes_every_state() -> None:
    assert check_r7_primary_button(_read(CSS_FILE)) == []
    assert check_r7_primary_button(".btn-primary { --bs-btn-bg: var(--rct-brand); }")


def test_r8_colours_come_from_tokens() -> None:
    assert check_r8_tokens(_read(CSS_FILE)) == []
    assert check_r8_tokens(".btn-primary { --bs-btn-disabled-bg: #0d6efd; }")
    assert check_r8_tokens(".settings-grid > .card { color: rgb(1 2 3); }")


def test_r9_tsdb_fields_use_the_shared_field_builder() -> None:
    js = _read(JS_DIR / "admin.js")
    assert check_r9_shared_fields(js) == []
    broken = js.replace("buildFieldControl(field, settingsDraft[field.key]);\n    // The shared", "buildOtherControl();\n    // The shared", 1)
    if broken != js:
        assert check_r9_shared_fields(broken)
