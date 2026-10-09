#!/usr/bin/env python3
#
# tests/test_admin_ui.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Admin GUI pages: menu structure, English-only text and the About page."""

import contextlib
import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app import __version__, resolve_build
from app.admin import updates
from app.api.app_factory import create_app
from app.config import Settings

ADMIN_DIR = Path(__file__).resolve().parent.parent / "app" / "admin"


JS = ADMIN_DIR / "static/js/admin.js"


TEMPLATES = ADMIN_DIR / "templates"


# The Swagger UI sidebar ships user-visible text too, so it is covered by the English-only check.
DOCS_NAV = Path(__file__).resolve().parent.parent / "app" / "api" / "docs_nav.py"


PAGES = ("dashboard", "inverters", "energy", "tsdb", "prometheus", "tokens", "settings")


def _send_block(js: str, part: str) -> str:
    """The success or the catch half of ``sendSettingsInner()``, the PUT body of a settings save."""
    start = js.index("async function sendSettingsInner(keys)")
    catch = js.index("} catch (error) {", start)
    return js[start:catch] if part == "success" else js[catch:js.index("} finally {", start)]


@contextlib.asynccontextmanager
async def _logged_in(tmp_path) -> AsyncIterator[httpx.AsyncClient]:
    """Lifespan-aware admin client: ``app.router.lifespan_context(app)`` runs the real ASGI
    startup/shutdown events, unlike a bare ``ASGITransport``, and the client is always closed
    even on an early assertion failure (mirrors ``tests.api_helpers.running_app()``)."""
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    password = app.state.first_start_password
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client,
    ):
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        await client.post("/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                          json={"current_password": password, "new_password": "a much stronger password"})
        yield client


@pytest.mark.asyncio
async def test_pages_render_with_the_new_menu(tmp_path):
    async with _logged_in(tmp_path) as client:
        for page in PAGES:
            response = await client.get(f"/ui/{page}")
            assert response.status_code == 200, page
            assert '<html lang="en"' in response.text
            for slug in PAGES:
                assert f'href="/ui/{slug}"' in response.text
            assert 'aria-current="page"' in response.text
        assert "Inverters" in (await client.get("/ui/inverters")).text
        assert 'id="exposed-list"' in (await client.get("/ui/prometheus")).text
        assert 'id="writable-list"' in (await client.get("/ui/inverters")).text
        # Both pages render the same table, search and pager through one shared macro.
        for html, prefix in (((await client.get("/ui/prometheus")).text, "exposed"), ((await client.get("/ui/inverters")).text, "writable")):
            for suffix in ("search", "list", "range", "prev", "next"):
                assert f'id="{prefix}-{suffix}"' in html
            assert "param-table-wrap" in html and 'data-bs-toggle="modal"' in html
        removed = await client.get("/ui/parameters")
        assert removed.status_code == 303


@pytest.mark.asyncio
async def test_about_update_check_requires_login_and_renders_release_controls(tmp_path, monkeypatch):
    calls = []

    def fake_check(force=False):
        calls.append(force)
        return {"update_available": True, "current_version": "1.0.0", "latest_version": "1.1.0",
                "release_url": "https://github.com/Gill-Bates/rct-manager/releases/tag/v1.1.0",
                "published_at": "2026-10-01T12:00:00Z", "error": None}

    monkeypatch.setattr(updates, "check_for_updates", fake_check)
    # The route binds the imported function when the module loads.
    monkeypatch.setattr("app.admin.api.check_for_updates", fake_check)
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "anonymous.db"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as anonymous:
        assert (await anonymous.get("/admin/api/check-updates")).status_code == 401
    assert calls == []

    async with _logged_in(tmp_path) as client:
        page = await client.get("/ui/about")
        assert page.status_code == 200
        assert 'id="btn-check-updates"' in page.text
        assert 'id="update-current-version"' in page.text
        assert 'id="update-latest-version"' in page.text
        assert '/admin/static/js/about.js' in page.text
        result = await client.get("/admin/api/check-updates")
        assert result.status_code == 200
        assert result.json()["latest_version"] == "1.1.0"
        forced = await client.get("/admin/api/check-updates?force=true")
        assert forced.status_code == 200
    assert calls == [False, True]


def test_frontend_has_no_german_text():
    german = re.compile(
        r"[äöüßÄÖÜ]|\b(Passwort|Einstellungen|Zurück|Speichern|Abbrechen|Fehler|Wechselrichter|Übersicht|"
        r"Parameter gespeichert|Lade|Noch keine|Anmelden|Abmelden|Gerät|Schreibzugriff|Wartung|"
        r"Nach oben|Nach unten|Nach|Entfernen|Hinzufügen|Schließen|Weiter|Batterie|Speicher|"
        r"klicken|Wechseln|Pfad|Methode|Beschreibung|Endpunkt|Endpunkte|automatisch|dunkel)\b"
        r"|Design: (?:automatisch|hell|dunkel)"
    )
    for path in [*ADMIN_DIR.glob("templates/*.html"), *ADMIN_DIR.glob("static/js/*.js"),
                 *ADMIN_DIR.glob("static/img/*.svg"),
                 ADMIN_DIR / "static/css/admin.css", ADMIN_DIR / "static/css/about.css",
                 ADMIN_DIR / "api.py", ADMIN_DIR / "ui.py",
                 DOCS_NAV]:
        assert not german.search(path.read_text(encoding="utf-8")), path.name


def test_connection_monitor_probes_health_and_the_modal_is_wired_into_every_page():
    js = (ADMIN_DIR / "static/js/connection.js").read_text(encoding="utf-8")
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "pingUrl: '/health'" in js and "failThreshold: 3" in js
    assert "response.status === 401" in js and "'/login'" in js
    assert 'src="/admin/static/js/connection.js"' in base
    modal = re.search(r'<div class="modal fade" id="reconnect-modal"[^>]*>', base).group(0)
    assert 'data-bs-backdrop="static"' in modal and 'data-bs-keyboard="false"' in modal
    assert 'aria-labelledby="reconnect-title"' in modal and 'id="reconnect-title"' in base
    assert "window.RCTReconnect?.start()" in JS.read_text(encoding="utf-8")


def test_header_is_sticky_above_content_and_toasts_stay_on_top():
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    assert re.search(r"\.admin-navbar\s*\{[^}]*position:\s*sticky;\s*top:\s*0;\s*z-index:\s*1030", css)
    assert re.search(r"\.mobile-nav\s*\{[^}]*position:\s*sticky;\s*top:\s*4\.1rem", css)
    assert re.search(r"html\s*\{\s*scroll-padding-top", css)
    assert int(re.search(r"\.toast-region\s*\{[^}]*z-index:\s*(\d+)", css).group(1)) > 1030


def test_device_editor_has_host_port_and_network_id_fields():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "Device ID" not in js and "Display name" not in js
    assert ("data-field" in js or "dataset.field" in js) and "Add another inverter" in js
    # The network id is part of the backend uniqueness key, so master/slave setups need the field.
    assert "'network_id'" in js and "Network ID" in js
    assert "networkLabel.htmlFor" in js and "networkInput.inputMode = 'numeric'" in js
    # The blank trailing row goes through the same single place that mints a device identity.
    assert "devices.push(withUid({ host: '', port: 8899, network_id: null }))" in js


def test_device_duplicate_rule_matches_the_backend_key_and_apply_adopts_the_server_list():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "networkId(other.network_id) === network" in js  # host, port and network id together
    assert "Object.assign(device, (result.settings" not in js
    # After a successful apply the draft is the server state, including newly assigned ids.
    apply_block = js[js.index("async function applyDevices(card)"):js.index("function buildGroupBody(")]
    assert "adoptDevices(structuredClone(settingsCommitted.devices || payload.devices));" in apply_block


def test_numeric_settings_carry_their_own_bounds():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "control.min = String(field.min); control.max = String(field.max)" in js
    assert "min: 1, max: 10000" in js and "min: 1, max: 3600" in js and "min: 1024, max: 65535" in js


def test_tsdb_secret_fields_render_even_though_their_raw_value_is_never_sent_to_the_browser():
    """Regression: _settings_view() exposes only "<key>_configured" for a secret, never the raw
    value, so a field-presence check keyed on settingsDraft[field.key] alone hid the password
    input next to Username entirely (it has no raw value in settingsDraft until it is edited)."""
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "function exportFieldKnown(field)" in js
    assert "field.type === 'secret' && Object.hasOwn(settingsDraft, `${field.key}_configured`)" in js
    # Both call sites that gate which export fields render/post must use the fixed helper.
    assert js.count("exportFieldKnown(field)") >= 2
    assert "!Object.hasOwn(settingsDraft, field.key)) continue" not in js


def test_parameter_reorder_focus_uses_stable_action_hooks():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "button.dataset.action = key" in js
    assert "'.metric-menu-toggle'" in js
    assert not re.search(r'\[title="[^"]*"\]', js)


def test_inverters_and_prometheus_share_the_parameter_table_helpers():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert js.count("renderParameterTable(") == 3  # definition and both tables
    assert js.count("parameterActionMenu(") == 3
    assert "renderParameterTable('writable'" in js and "renderParameterTable('exposed'" in js
    assert js.count("EXPOSED_PAGE_SIZE = 15") == 1


def test_navbar_shows_the_short_label_and_an_enlarged_logo():
    base = (ADMIN_DIR / "templates/base.html").read_text(encoding="utf-8")
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    assert "<span>Administration</span>" in base and "<span>RCT Administration</span>" not in base
    # The browser-tab title keeps the full product name.
    assert "{% block title %}RCT Administration{% endblock %}" in base
    assert re.search(r"\.nav-logo\s*\{[^}]*width:\s*5\.1rem;\s*height:\s*2\.78rem", css)
    # Header buttons shrink but stay above a 2.2rem touch target.
    assert re.search(r"\.navbar\s\.btn\s*\{[^}]*height:\s*2\.3rem", css)


def test_settings_grid_stretches_its_cards_on_every_page_including_tsdb():
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    assert re.search(r"\.settings-grid\s*\{[^}]*align-items:\s*stretch", css)
    assert re.search(r"\.settings-grid>\.card>\.card-body\s*\{\s*flex:\s*1 1 auto;\s*\}", css)
    assert ".export-grid" not in css


def test_admin_card_spacing_uses_one_token_and_sibling_rule():
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    roots = re.findall(r":root\s*\{([^}]*)\}", css)
    assert any(re.search(r"--rct-card-gap:\s*1\.25rem\s*;", root) for root in roots)
    assert len(re.findall(r"--rct-card-gap\s*:", css)) == 1
    assert re.search(
        r"#main-content\s+\.page-heading\s*~\s*:is\(\.card,\s*\.settings-grid,\s*"
        r"\.energy-grid,\s*#export-actions,\s*\.prometheus-dependent-settings:not\(:empty\)\),\s*#main-content\s+\.about-top-row\s*~\s*"
        r"\.card\s*\{\s*margin-top:\s*var\(--rct-card-gap\);\s*\}", css,
    )
    for selector in ("settings-grid", "energy-grid"):
        rule = re.search(rf"\.{selector}\s*\{{([^}}]*)\}}", css)
        assert rule and re.search(r"\bgap:\s*var\(--rct-card-gap\)\s*;", rule.group(1))
        assert len(re.findall(r"\bgap\s*:", rule.group(1))) == 1
    about_row = re.search(r"#main-content\s+\.about-top-row\s*\{([^}]*)\}", css)
    assert about_row
    assert "--bs-gutter-x: var(--rct-card-gap);" in about_row.group(1)
    assert "--bs-gutter-y: var(--rct-card-gap);" in about_row.group(1)


@pytest.mark.parametrize("page", ("prometheus", "tokens", "inverters", "settings", "tsdb", "energy", "about"))
def test_admin_top_level_cards_and_grids_have_no_margin_utilities(page):
    html = (TEMPLATES / f"{page}.html").read_text(encoding="utf-8")
    targets = re.findall(r'<(?:section|details|div)\b[^>]*\bclass="([^"]+)"', html)
    targets = [classes for classes in targets if set(classes.split()) & {"card", "settings-grid", "energy-grid", "about-top-row"}]
    assert targets, page
    assert all(not ({"mt-3", "mt-4"} & set(classes.split())) for classes in targets), page


def test_prometheus_and_inverters_share_parameter_table_markup():
    macro = (TEMPLATES / "_param_table.html").read_text(encoding="utf-8")
    for page, prefix in (("prometheus", "exposed"), ("inverters", "writable")):
        html = (TEMPLATES / f"{page}.html").read_text(encoding="utf-8")
        assert '{% from "_param_table.html" import param_table %}' in html
        assert re.search(r'<div class="page-heading">', html)
        assert re.search(rf"\{{\{{ param_table\('{prefix}',", html)
    for shared_class in ("card", "param-card-head", "param-table-wrap", "param-table", "btn-group"):
        assert re.search(rf"\b{shared_class}\b", macro)
    for suffix in ("search", "list", "range", "prev", "next"):
        assert f'{{{{ prefix }}}}-{suffix}' in macro


def test_metrics_fields_hide_behind_the_master_toggle():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "label: 'Enable Metrics Endpoint'" in js
    for key in ("metrics_require_token", "metrics_trusted_sources",
                "metrics_rate_limit_requests", "metrics_rate_limit_window_seconds"):
        row = next(line for line in js.splitlines() if f"key: '{key}'" in line)
        assert "requires: 'enable_metrics_endpoint'" in row, key
    # Only the dependent fields are rebuilt, and the toggle keeps the focus it was operated with.
    assert "function settingVisible(field)" in js
    assert "preserveFocus(rerenderPrometheusDependents)" in js
    # The master toggle lives in the top status card, the dependents in #settings-sections.
    assert "function renderPrometheusSettings()" in js
    assert "$('prometheus-toggle')" in js


def test_token_expiry_uses_presets_with_a_ninety_day_default():
    html = (ADMIN_DIR / "templates/tokens.html").read_text(encoding="utf-8")
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert 'type="date"' not in html
    assert '<label for="token-expires" class="form-label">Expiry</label>' in html
    for value, text in (("30d", "30 days"), ("90d", "90 days"), ("1y", "1 year"), ("never", "Never expires")):
        assert f'value="{value}"' in html and f">{text}<" in html
    assert 'value="90d" selected' in html
    assert "const EXPIRY_DEFAULT = '90d';" in js
    # Real calendar arithmetic, so a leap day lands on the anniversary.
    assert "date.setFullYear(date.getFullYear() + 1)" in js
    # form.reset() snaps a select back to its markup default; the default is re-applied explicitly.
    assert "$('token-expires').value = EXPIRY_DEFAULT;" in js
    # Tokens live in a modal over a table; the secret is cleared when the modal closes.
    assert 'id="add-token-modal"' in html and 'id="new-token-box"' not in html
    assert "hidden.bs.modal" in js and "$('new-token-value').textContent = '';" in js


def test_parameter_action_buttons_have_touch_target_size():
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    assert re.search(r"\.parameter-actions\s*\.btn\s*\{\s*min-width:\s*2\.8rem;\s*min-height:\s*2\.8rem;\s*\}", css)


def test_dashboard_polling_is_sequential_bounded_and_incremental():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    start = js.index("function syncChildren(")
    block = js[start:js.index("function formatDate(", start)]
    # Recursive timeout instead of a fixed interval, with an in-flight guard and a stale-response guard.
    assert "setInterval" not in js[js.index("async function bootstrap()"):]
    assert "setTimeout(" in block and "if (automatic && dashboardController) return 'skipped';" in block
    assert "generation !== dashboardGeneration" in block
    # Fetch timeout, immediate refresh on tab visibility, and capped backoff with jitter.
    assert "DASHBOARD_TIMEOUT_MS = 8000" in block and "controller.abort(" in block
    assert "addEventListener('visibilitychange'" in block and "document.hidden" in block
    assert "DASHBOARD_MAX_INTERVAL_MS = 60000" in block and "Math.random()" in block
    # Parameters are loaded once and after device changes, never from the polling function.
    assert "api('parameters')" not in block[block.index("async function loadDashboard("):]
    assert "loadMetricCount()" in js
    # Cards are patched per device id; the list is never rebuilt wholesale.
    assert "list.replaceChildren" not in block and "dashboardCards.get(key)" in block
    assert " · stale" not in block


def test_setting_control_and_export_control_share_the_field_builder():
    """Consolidation (behavior-preserving): both functions build their label/control/help markup
    through one shared helper instead of duplicating the select/toggle/number/text/list/secret
    construction, so the number-range validation added for review item 3/4 lives in one place."""
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "function buildFieldControl(field, value)" in js
    assert "const { wrap, control } = buildFieldControl(field, settingsDraft[field.key]);" in js
    assert js.count("buildFieldControl(field, settingsDraft[field.key])") == 2
    assert "function numberFieldInvalid(control, field)" in js
    # Both number branches call the shared validator; neither writes an unguarded Number(control.value).
    assert js.count("numberFieldInvalid(control, field)") >= 2
    assert "Number(control.value) === 0" not in js
    assert "(field.nullable ? null : field.min)" not in js  # old silent-fallback for an empty value


def test_export_secret_is_cleared_from_the_draft_after_a_successful_save():
    """Review item 1 (HIGH): a saved secret must not stay in settingsDraft, or it keeps being
    resent on every later unrelated export-settings change. Finding 5: the clearing is gated by a
    per-secret revision, so an older response cannot clear a value typed during the request."""
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    success_block = _send_block(js, "success")
    assert "for (const key of secretKeysIn(payload)) {" in success_block
    assert "if (secretRevision.get(key) !== sendRevisions.get(key)) continue;" in success_block
    assert "settingsDraft[key] = '';" in success_block
    assert "settingsDraft[`${key}_configured`] = true;" in success_block
    # The snapshot is taken by the sender and lives exactly one request.
    assert "for (const key of keys) if (isSecretKey(key)) sendRevisions.set(key, secretRevision.get(key) || 0);" in js
    assert "sendRevisions.clear();" in js
    # _configured only for a secret the server actually stored (an empty one is dropped, api.py:435).
    assert "return Object.keys(payload).filter((key) => isSecretKey(key) && payload[key]);" in js


def test_export_group_is_withheld_as_a_group_and_revalidated_at_send_time():
    """An incomplete export configuration is never sent, nothing is sent while typing, and the group
    is re-checked immediately before sending."""
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "let pendingExportGroup = false;" in js
    queue = js[js.index("function queueExportSettings()"):js.index("function refreshExportBar()")]
    assert "setSectionState('export', 'incomplete', missingExportHint(type));" in queue
    assert "restartDebounce" not in queue and "flushSettings" not in queue  # no autosave for the export form
    apply = js[js.index("async function applyExport()"):js.index("function tsdbStatusView()")]
    assert "if (!exportReady(type)) { queueExportSettings(); return; }" in apply
    assert "pendingExportGroup = true;" in apply and "flushSettings({ sections: ['export'] })" in apply
    # The debounced autosave only covers the general settings, never the export group.
    assert "flushSettings({ sections: ['general'] })" in js
    # The send-time re-check sits in the synchronous wrapper, before any key is computed.
    send = js[js.index("function sendSettings(sections = null)"):js.index("function secretKeysIn(")]
    assert "if (pendingExportGroup && !exportReady(type)) {" in send
    assert "devices" not in send  # the device list never goes through the autosave queue
    assert send.index("if (pendingExportGroup && !exportReady(type)) {") < send.index("const keys = keysFor(sections);")


def test_tsdb_form_has_a_button_only_apply_bar_and_does_not_autosave():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    html = (TEMPLATES / "tsdb.html").read_text(encoding="utf-8")
    assert 'id="export-actions"' in html and "align-items-end" in html
    # The shared bar is parameterised, not overridden: TSDB asks for the button alone.
    assert "buildApplyBar('export', {" in js
    builder = js[js.index("function buildApplyBar("):js.index("function buildDeviceApplyBar(")]
    # Every explicit-apply form shows the button alone: no Discard, count badge or status text.
    assert "'Discard'" not in builder and "badge" not in builder and "status" not in builder
    assert "bar.append(apply);" in builder and "'Apply changes'" in builder
    bar = js[js.index("function refreshExportBar()"):js.index("async function applyExport()")]
    assert "apply.disabled = settingsSending || !exportDirty() || incomplete;" in bar
    assert "export-discard" not in js and "export-change-count" not in js and "discardExport" not in js
    # Export fields are marked dirty on change but never queued for the debounce timer.
    control = js[js.index("function exportControl("):js.index("// _settings_view() never exposes")]
    assert "queueExportSettings();" in control and "queueSettings(" not in control
    # A rejected apply keeps what was typed.
    assert "if (sectionOf(key) === 'export') continue;" in js
    # An empty secret is never sent, so a blank token keeps the stored one.
    assert "filter((key) => !(isSecretKey(key) && !settingsDraft[key]))" in js


def test_admin_scripts_never_open_the_native_leave_page_dialog():
    for script in (ADMIN_DIR / "static/js").glob("*.js"):
        text = script.read_text(encoding="utf-8")
        assert "beforeunload" not in text and "onbeforeunload" not in text, script.name


def test_missing_export_hint_names_both_failure_causes():
    """Review item 8 (MEDIUM): exportReady() also fails for an unpaired username/password, where
    every required key is present — a single "… is required" message would name nothing missing."""
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    hint = js[js.index("function missingExportHint(type)"):js.index("// The export group is applied explicitly")]
    assert "required for ${backendLabel(type)}" in hint
    assert "must be set together or left empty" in hint
    assert "return 'Not saved yet.';" in hint
    # Labels come from exportFields, never hard-coded.
    assert "function labelOf(key) { return exportFields.find((field) => field.key === key)?.label || key; }" in js
    assert "Organization" not in hint and "Bucket" not in hint


def test_failed_settings_save_does_not_re_render_the_whole_page():
    """Review item 6 (MEDIUM): a global renderSettings() on a failed save would also rebuild a
    section the operator was mid-edit on but that was never part of this save. Finding 2: the
    devices draft is not rolled back at all, because that would orphan the live row objects."""
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    catch_block = _send_block(js, "catch")
    assert "renderSettings();" not in catch_block
    # Re-queued during the request, device rows and secrets are all skipped by the rollback.
    assert "if (pendingKeys.has(key)) continue;" in catch_block
    assert "devices" not in catch_block
    assert "if (isSecretKey(key)) continue;" in catch_block
    assert "for (const id of sentSections) reportSaveFailure(id, failureMessage(id, reason));" in catch_block


def test_device_list_has_no_autosave_path():
    """The device list reconfigures live connections, so only an explicit Apply may send it."""
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "queueSettings('devices')" not in js
    assert "pendingKeys.has('devices')" not in js
    assert "function syncDeviceInputs(" not in js and "function addInverter(" not in js
    assert "function devicesValid()" not in js
    editor = js[js.index("function renderDevicesSettings(card)"):js.index("function buildDeviceApplyBar(")]
    assert "addEventListener('change'" not in editor  # edits are draft-only, on input
    assert "addEventListener('input', onInput)" in editor
    # Exactly one request site sends the devices key.
    assert js.count("body: JSON.stringify({ devices") + js.count("payload = { devices: devicesPayload() }") == 1
    template = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    assert "saved automatically" not in template


def test_device_apply_bar_is_explicit_accessible_and_guards_reset_and_unload():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    bar = js[js.index("function buildApplyBar("):js.index("function addDeviceRow(")]
    assert "'Apply changes'" in bar and "apply.id = `${prefix}-apply`" in bar and "'Discard'" not in bar
    assert "buildApplyBar('device'" in bar
    assert "discardDeviceChanges" not in js and "device-reset-warning" not in js
    state = js[js.index("function refreshDeviceState(card)"):js.index("function rebuildDeviceSection()")]
    assert "apply.disabled = deviceUi.applying || invalid || changes.count === 0;" in state
    run = js[js.index("async function applyDevices(card)"):js.index("function buildGroupBody(")]
    assert "if (deviceUi.applying) return;" in run
    assert "confirmAction({" in run and "message: `${RESET_NOTE}" in run and "changes.risky.length" in run
    assert "document.getElementById('device-apply')?.focus()" in run
    assert "error?.status === 409" in js and "error?.status === 504" in js
    # Leaving the page with an unsaved draft is silent: no native beforeunload dialog anywhere.
    assert "beforeunload" not in js and "onbeforeunload" not in js and "returnValue" not in js


def test_questdb_username_and_password_must_be_filled_together():
    """Regression: app/config.py's Settings._check_export rejects a PUT with exactly one of
    questdb_username/questdb_password set ("must be set together or not at all"); admin.js must
    withhold the autosave until both are filled, or both are empty, same as a required field."""
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "const EXPORT_PAIRED_FIELDS = { questdb: [['questdb_username', 'questdb_password']] };" in js
    assert "function exportPairsReady(type)" in js
    assert "exportRequiredKeys(type).every(exportFieldFilled) && exportPairsReady(type)" in js


def test_login_form_posts_and_registers_its_handler_before_any_await():
    """Finding 1 (HIGH): without method="post" a JS-dead page (or one whose session() failed before
    the handler was registered) submits username and password as GET query parameters, so the
    password lands in the URL, the browser history and the server access log."""
    login = (TEMPLATES / "login.html").read_text(encoding="utf-8")
    change = (TEMPLATES / "change_password.html").read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    assert '<form id="login-form" method="post" action="/admin/api/login">' in login
    assert 'method="post" action="/admin/api/change-password"' in change
    assert "<noscript>" in login and "JavaScript is required to log in." in login
    # The button must stay enabled: the enabling code would never run on the JS-dead path.
    assert 'type="submit" id="login-submit"' in login and "disabled" not in login
    for text in (login, change):
        assert 'id="auth-init-state"' in text
    assert "login-init-state" not in js
    for path in TEMPLATES.glob("*.html"):
        assert "login-init-state" not in path.read_text(encoding="utf-8"), path.name
    # Registered during page parse, outside bootstrap(): start() is not async at all, so nothing
    # can be awaited before the handler exists, and the async remainder runs detached.
    assert "async function start()" not in js
    start = js[js.index("function start() {"):]
    assert "if (page === 'login') initAuthForm('login-form', submitLogin);" in start
    assert start.index("initAuthForm('login-form'") < start.index("bootstrap()")
    assert "bootstrap().catch((error) => toast(messageFrom(error), 'danger'));" in start
    handler = js[js.index("function initAuthForm(formId, submit)"):js.index("async function submitLogin(")]
    assert handler.index("event.preventDefault();") < handler.index("await")


def test_ensure_session_does_not_cache_a_rejection():
    """A rejected promise is truthy, so caching it would make every later submit await the same
    failure and never issue a second request — the recovery path could not work at all."""
    js = JS.read_text(encoding="utf-8")
    assert "sessionPromise ||=" not in js
    block = js[js.index("function ensureSession()"):js.index("// The status line of both auth pages")]
    assert "sessionPromise = session().catch((error) => { sessionPromise = null; throw error; });" in block
    # The first failure focuses #form-error; repeat attempts leave focus in the password field.
    assert "if (!authInitReported) { authInitReported = true; formError(message); }" in js
    assert "authInitReported = false;" in js[js.index("async function session()"):js.index("let sessionPromise")]


def test_flush_settings_propagates_the_result_of_an_in_flight_save():
    """Finding 3 (HIGH): the in-flight branch returned true regardless of what the running save
    resolved."""
    js = JS.read_text(encoding="utf-8")
    flush = js[js.index("async function flushSettings({ sections = null } = {})"):js.index("// Synchronous wrapper")]
    assert "const result = await inFlight.promise.catch(() => false);" in flush
    # Only a request that carried one of the caller's sections may decide the caller's result.
    assert "if (!sections || sections.some((id) => inFlight.sections.has(id))) ok = result && ok;" in flush
    assert "if (requestedIncomplete(sections)) return false;" in flush
    assert "ok = (await sendSettings(sections).catch(() => false)) && ok;" in flush
    assert "return true;" not in flush


def test_the_settings_request_is_scoped_to_the_keys_it_carries():
    js = JS.read_text(encoding="utf-8")
    send = js[js.index("function sendSettings(sections = null)"):js.index("function secretKeysIn(")]
    assert "const keys = keysFor(sections);" in send
    # The sent keys leave the queue before the await, which is what the rollback guard reads.
    assert "for (const key of keys) pendingKeys.delete(key);" in send
    assert "if (keys.some((key) => sectionOf(key) === 'export')) pendingExportGroup = false;" in send
    assert "settingsInFlight = { promise, keys: new Set(keys), sections: new Set(keys.map(sectionOf)) };" in send
    assert "return Promise.resolve(!requestedIncomplete(sections));" in send
    keys_for = js[js.index("function keysFor(sections)"):js.index("function hasPending(")]
    assert "const blocked = incompleteSections();" in keys_for
    assert "sectionOf(key)" in keys_for and "exportPayloadKeys(type)" in keys_for
    assert "isSecretKey(key) && !settingsDraft[key]" in keys_for
    # The finally re-arms the debounce instead of recursing.
    finally_block = js[js.index("sendRevisions.clear();"):js.index("// Shared by settingControl() and exportControl(): builds")]
    assert "if (hasPending(['general'])) restartDebounce();" in finally_block
    assert "flushSettings(" not in finally_block
    # Exactly one declaration, re-shaped from a bare promise rather than declared a second time.
    assert js.count("let settingsInFlight") == 1
    assert "await settingsInFlight?.catch" not in js


def test_secret_keys_mirror_the_server_secret_list():
    """A suffix test classifies the `metrics_require_token` boolean toggle as a secret, which drops
    it from the payload when off and blanks the checkbox when on."""
    from app.admin.api import _SECRET_EDITABLE

    js = JS.read_text(encoding="utf-8")
    body = re.search(r"const SECRET_KEYS = new Set\(\[([^\]]*)\]\)", js)
    assert body, "SECRET_KEYS allowlist missing"
    assert set(re.findall(r"'([^']+)'", body.group(1))) == set(_SECRET_EDITABLE)
    assert "endsWith('_token')" not in js and "endsWith('_password')" not in js
    assert "function isSecretKey(key) { return SECRET_KEYS.has(key); }" in js
    # exportFields' own secret fields and the allowlist cannot drift apart.
    export_secrets = {
        match.group(1)
        for match in re.finditer(r"\{ key: '([^']+)'[^\n]*type: 'secret'", js)
    }
    assert export_secrets == set(_SECRET_EDITABLE), export_secrets


def test_save_state_has_one_writer_and_five_distinguishable_states():
    js = JS.read_text(encoding="utf-8")
    assert "const SAVE_STATES = { idle: 0, saved: 1, incomplete: 2, unsaved: 3, saving: 4, failed: 5 };" in js
    render = js[js.index("function renderSaveState()"):js.index("function reportSaveFailure(")]
    for text in ("Save failed", "Saving …", "Unsaved changes", "Incomplete — not saved yet"):
        assert text in js, text
    # A successful save shows no label text (the toast reports it); the state stays readable as data-state.
    assert "`Saved ${hhmm(savedAt)}`" not in render and "label.dataset.state = top;" in render
    # Absent on tokens/about/login/change-password: the alerts are reconciled first, then it returns.
    assert render.index("renderSectionAlert(id, entry.failure)") < render.index("if (!label) return;")
    # The persistent alert is lifted out of a folded <details> and anchored before its section.
    # Both notice kinds share one builder, so `sectionAlert` is only the error-kind entry point and
    # the anchoring lives in `sectionNotice`; a folded disclosure is forced open for errors only,
    # because a hint must not unfold a section the operator deliberately collapsed.
    alert = js[js.index("function sectionNotice(id, kind)"):js.index("// Retry only where a retry can do something")]
    assert "function sectionAlert(id) { return sectionNotice(id, 'error'); }" in alert
    assert "anchor.closest('details') || anchor" in alert
    assert "if (host.tagName === 'DETAILS' && kind === 'error') host.open = true;" in alert
    assert "host.parentElement.insertBefore(node, host);" in alert
    assert "aria-live', 'off'" in alert
    # The danger toast stays as the visibility floor next to the persistent alert.
    report = js[js.index("function reportSaveFailure(section, message)"):js.index("function restartDebounce()")]
    assert "toast(message, 'danger');" in report
    # No Retry for general or parameters; devices additionally offers Discard changes.
    retries = js[js.index("const RETRY_ACTIONS = {"):js.index("function renderSectionAlert(")]
    assert "export:" in retries and "devices:" not in retries
    assert "general:" not in retries and "parameters:" not in retries
    # The parameters path is wired, not just declared.
    assert "setSectionState('parameters', 'unsaved');" in js
    assert "setSectionState('parameters', 'saving');" in js
    assert "reportSaveFailure('parameters', failureMessage('parameters', messageFrom(error)));" in js


def test_devices_are_bound_by_a_stable_identity_not_by_array_index():
    """Finding 2 (HIGH): replacing settingsDraft.devices (and every object in it) on a failed save
    orphans the objects the live change handlers and the row DOM still reference."""
    js = JS.read_text(encoding="utf-8")
    assert "function withUid(device) { if (!device._uid) device._uid = `d${++deviceUid}`; return device; }" in js
    assert "settingsDraft.devices = (list || []).map(withUid);" in js
    assert js.count("device._uid = `d${++deviceUid}`") == 1  # exactly one place mints a _uid
    assert "if (Object.hasOwn(settingsDraft, 'devices')) adoptDevices(settingsDraft.devices);" in js
    assert "function deviceByUid(uid) { return (settingsDraft.devices || []).find((device) => device._uid === uid) || null; }" in js
    # The row lookup resolves by _uid and skips a miss, which is a real state right after Discard.
    assert js.count("const device = deviceByUid(row.dataset.uid);") == 1
    assert js.count("if (!device) continue;") >= 1
    assert "settingsDraft.devices[Number(row.dataset.index)]" not in js
    assert "row.dataset.uid = device._uid;" in js
    # The payload is explicit instead of a spread, so no client-only key rides along.
    payload = js[js.index("function devicesPayload()"):js.index("// The device list is applied explicitly")]
    assert "{ ...device" not in payload
    for field in ("host:", "port:", "network_id:", "device_id:", "display_name:"):
        assert field in payload, field


def test_device_remove_handler_only_edits_the_draft_and_keeps_focus():
    js = JS.read_text(encoding="utf-8")
    handler = js[js.index("remove.addEventListener('click', () => {"):js.index("const networkHelp = element(")]
    assert "confirm(" not in handler                         # the reset warning belongs to Apply
    assert "const at = devices.indexOf(device);" in handler  # identity, not the captured index
    assert "if (at < 0) return;" in handler
    assert "devices.splice(index, 1)" not in js
    assert "rebuildDeviceSection();" in handler
    assert ".device-settings ~ button" in handler           # focus falls back to the Add button
    assert "queueSettings(" not in handler and "api(" not in handler


def test_build_group_body_is_shared_so_a_group_rerender_adds_no_second_heading():
    js = JS.read_text(encoding="utf-8")
    builder = js[js.index("function buildGroupBody(group)"):js.index("function groupSectionId(")]
    # The Account relabel lives in one place, used by both renderSettings() and rerenderGroup().
    assert "{ Account: 'Administrator account' }[group]" in builder
    # One <h2> per group card, so a live group re-render never stacks a second heading.
    assert builder.count("element('h2'") == 1
    assert js.count("buildGroupBody(group)") == 3  # definition plus both call sites
    assert "section.replaceChildren(buildGroupBody(group));" in js
    assert "if (!section) { renderSettings(); return; }" in js
    # Slugified, so a group name with a space still yields a valid id.
    assert "return `settings-group-${group.toLowerCase().replace(/\\s+/g, '-')}`;" in js


def test_dashboard_keeps_last_known_data_on_fetch_failure():
    """A failed or empty poll must not blank or shrink the Overview: values expire, the layout stays."""
    js = JS.read_text(encoding="utf-8")
    html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    block = js[js.index("async function loadDashboard("):js.index("function initDashboardPolling(")]
    fail = block[block.index("} catch (error) {"):]
    assert "setDashboardStale(error)" in fail and "clearDashboardStale()" in block
    # The failure path only replaces the list with the error notice when nothing was ever known.
    assert fail.index("if (dashboardSnapshot) setDashboardStale") < fail.index("dashboardNotices.error")
    # A successful poll renders from the merged last-known state, never from the raw payload.
    assert "absorbDashboard(" in block and "refreshDashboard()" in block and "renderDashboard(data" not in block
    # Values run out by age (n/a), structure (towers, flow graphic) outlives them; a reload restores both.
    for marker in ("VALUE_MAX_AGE_MS", "expired: metricShown(metric)", "flow_known", "restoreSnapshot()", "SNAPSHOT_MAX_AGE_MS", "localStorage"):
        assert marker in js, marker
    # The note replaces the subtitle instead of being inserted above the grid, so nothing shifts.
    assert "subtitle.after(dashboardBanner)" in js and "grid.before(dashboardBanner)" in js
    assert "dashboard-offline-banner" in (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    # The removed 'Updated HH:MM' line stays removed.
    for marker in ('dashboard-updated', 'dashboardLastSuccess', 'id="refresh-dashboard"'):
        assert marker not in html and marker not in js, marker


def test_accessibility_wiring_for_help_texts_field_errors_and_icon_only_buttons():
    js = JS.read_text(encoding="utf-8")
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    # The logout label text is hidden below the sm breakpoint and the icon is aria-hidden.
    assert 'id="logout-button"' in base and 'aria-label="Log out"' in base
    # Help text is associated with its field, and a field error is referenced too.
    builder = js[js.index("function buildFieldControl(field, value)"):js.index("function settingControl(field)")]
    assert "help.id = `${id}-help`;" in builder
    assert "control.setAttribute('aria-describedby', help.id);" in builder
    invalid = js[js.index("function reportInvalid(control, message)"):js.index("const exportFields = [")]
    assert "control.setAttribute('aria-invalid', 'true');" in invalid
    assert "element('div', 'invalid-feedback d-block')" in invalid
    assert "control.setAttribute('aria-describedby', `${control.id}-help ${errorId}`);" in invalid
    assert "function clearInvalid(control)" in js and js.count("clearInvalid(control);") == 2
    # Device rows reference their row-level error node and mark only the offending input. The ids
    # derive from the stable row uid (not the loop index) so preserveFocus() restores focus to the
    # right row after a removal (JS-01).
    assert "feedback.id = `device-${device._uid}-feedback`;" in js
    assert "input.setAttribute('aria-invalid', 'true');" in js
    # Focus survives the two full-section rebuilds and the device rebuild.
    assert "function preserveFocus(render)" in js
    assert "const rerender = () => preserveFocus(render);" in js
    assert "preserveFocus(() => {" in js
    # Prometheus descriptions stay reachable on mobile through a native disclosure.
    assert "element('details', 'metric-description-mobile')" in js
    assert re.search(r"@media \(max-width: 767\.98px\) \{\s*\.metric-description-mobile \{\s*display: block", css)


def test_dashboard_metrics_are_whole_numbers_in_the_browser_locale():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    start = js.index("function metricParts(")
    block = js[start:js.index("function showHeroMetrics(", start)]
    # No decimal places; Intl.NumberFormat(undefined, ...) picks the separator from the browser locale.
    assert "new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 })" in block
    assert "toFixed" not in block and "'en-US'" not in block and "'en-GB'" not in block


def test_grid_power_is_shown_as_magnitude_with_a_direction_indicator():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    # Negative = feed-in, positive = draw; values that round to zero (including -0) get no indicator.
    assert "if (!Number.isFinite(value) || Math.round(value) === 0) return null;" in js
    assert "return value < 0\n      ? { dir: 'feed'" in js
    assert "Math.abs(grid)" in js and "isGrid ? Math.abs(value) : value" in js
    assert "icon: 'arrow_upward'" in js and "icon: 'arrow_downward'" in js
    assert "node.setAttribute('aria-label', flow.label);" in js and "node.title = flow.label;" in js
    assert ".device-flow-feed" in css and ".device-flow-draw" in css


def test_dates_follow_the_browser_locale():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    start = js.index("function formatDate(")
    block = js[start:js.index("function ", start + 10)]
    assert "Intl.DateTimeFormat(undefined" in block
    assert "'en-GB'" not in block and "toLocaleDateString" not in block


@pytest.mark.asyncio
async def test_about_requires_session_and_renders_project_details(tmp_path, monkeypatch):
    monkeypatch.setattr("app.admin.ui.__build__", "abc1234")
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        unauthenticated = await client.get("/ui/about", follow_redirects=False)
        assert unauthenticated.status_code == 303
        assert unauthenticated.headers["location"] == "/login"

        assert "admin-footer" not in (await client.get("/login")).text

        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post(
            "/admin/api/login",
            headers={"X-CSRF-Token": csrf},
            json={"username": "admin", "password": password},
        )
        assert login.status_code == 200
        changed = await client.post(
            "/admin/api/change-password",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={"current_password": password, "new_password": "a much stronger password"},
        )
        assert changed.status_code == 200

        response = await client.get("/ui/about")
        assert response.status_code == 200
        assert 'href="/ui/about" aria-current="page"' in response.text
        assert f"<code>{__version__}</code>" in response.text
        assert 'class="admin-footer"' in response.text and f"v{__version__}" in response.text
        assert f'v{__version__} (<span class="font-monospace">abc1234</span>)' in response.text
        assert "Application Details" in response.text
        assert "Dependencies" in response.text
        assert "<code>" in response.text
        assert response.text.count("/rct-manager/releases") == 1
        assert "https://gill-bates.github.io/rct-manager/" in response.text
        assert "https://github.com/Gill-Bates/rct-manager/releases" in response.text
        assert (await client.get("/admin/static/css/about.css")).status_code == 200


def test_dashboard_grid_has_the_gridstack_structure_and_stable_widget_ids():
    html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="dashboard-grid"' in html and "grid-stack" in html
    # Whitespace-collapsed so an auto-formatter that line-wraps a long button is tolerated: the
    # label text matters, not where the markup happens to break across lines.
    assert "Edit dashboard" in re.sub(r"\s+", " ", html)
    assert 'id="dashboard-add-widget"' in html and "Edit Widgets" in html
    assert 'id="dashboard-reset-layout"' in html and "Reset layout" in html
    assert 'id="dashboard-edit-done"' in html and ">Done<" in html
    for widget_id in ("device-count", "connected-count", "metric-count", "tsdb-status",
                      "pv-power", "house-power", "grid-power", "battery-soc",
                      "days-to-calibration", "devices"):
        assert f'data-widget-id="{widget_id}"' in html
        assert f'gs-id="{widget_id}"' in html
    # The business-data DOM ids the polling code reads/writes stay intact inside the wrappers.
    for inner_id in ("device-count", "connected-count", "metric-count", "pv-power", "house-power",
                     "grid-power", "battery-soc", "days-to-calibration", "tsdb-status-icon",
                     "tsdb-status-label", "tsdb-status-time", "devices-list"):
        assert f'id="{inner_id}"' in html


def test_dashboard_template_positions_equal_the_factory_default_layout():
    # First paint without a saved layout shows the template, so it must equal DEFAULT_LAYOUT.
    js = (ADMIN_DIR / "static/js/dashboard.js").read_text(encoding="utf-8")
    html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    default = {
        m[0]: (*map(int, m[1:5]), m[5] == "true")
        for m in re.findall(
            r"\{ id: '([\w-]+)', x: (\d+), y: (\d+), w: (\d+), h: (\d+), visible: (true|false)", js)
    }
    template = {}
    for m in re.finditer(
            r'<div class="grid-stack-item( d-none)?" gs-id="([\w-]+)"\s+gs-x="(\d+)" gs-y="(\d+)" gs-w="(\d+)" gs-h="(\d+)"',
            html):
        template[m[2]] = (*map(int, m.group(3, 4, 5, 6)), not m[1])
    assert template == default and len(default) == 10
    # Off-by-default (visible:false) but still addable through the "Add widget" library.
    assert not default["days-to-calibration"][4]
    # Factory default: one row of six 2-column tiles, the inverter overview directly below it.
    tiles = ["pv-power", "grid-power", "house-power", "battery-soc", "tsdb-status", "metric-count"]
    assert [default[t][:4] for t in tiles] == [(x, 0, 2, 2) for x in range(0, 12, 2)]
    assert default["devices"][:4] == (0, 2, 12, 8)
    assert not default["device-count"][4] and not default["connected-count"][4]


def test_dashboard_grid_is_hidden_until_the_layout_is_final_with_a_css_failsafe():
    html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    css = (ADMIN_DIR / "static/css/dashboard.css").read_text(encoding="utf-8")
    js = (ADMIN_DIR / "static/js/dashboard.js").read_text(encoding="utf-8")
    assert "dashboard-grid--loading" in html
    # Hidden by default, and revealed by CSS alone if the script never does it.
    assert "visibility: hidden" in css and "@keyframes dashboard-grid-failsafe" in css
    # Revealed in a finally so a failed layout step never leaves the page blank; no initial animation.
    assert "} finally {\n      await revealGrid();" in js
    assert "classList.remove('dashboard-grid--loading')" in js
    assert "animate: false" in js and "setAnimation(true)" in js


def test_dashboard_grid_has_no_gravity_so_tiles_stay_where_they_are_dropped():
    js = (ADMIN_DIR / "static/js/dashboard.js").read_text(encoding="utf-8")
    # float:false compacts every tile to the top and makes free cells below/beside unreachable.
    assert "float: true" in js and "float: false" not in js


def test_gridstack_assets_load_only_on_the_dashboard_page():
    dashboard = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "gridstack.min.css" in dashboard and "gridstack-all.js" in dashboard
    assert "{% block extra_js %}{% endblock %}" in base
    for other_page in ("inverters", "energy", "tsdb", "prometheus", "tokens", "settings", "about"):
        other = (TEMPLATES / f"{other_page}.html").read_text(encoding="utf-8")
        assert "gridstack" not in other.lower(), other_page


def test_dashboard_js_reuses_the_shared_api_helper_and_does_not_touch_polling():
    js = (ADMIN_DIR / "static/js/dashboard.js").read_text(encoding="utf-8")
    admin_js = JS.read_text(encoding="utf-8")
    assert "const { api, toast, messageFrom, confirmAction } = window.RCTAdmin;" in js
    # Tiles are moved by mouse drag on the header strip; the old Move/Resize context menu is gone.
    assert "handle: '.dashboard-widget-header'" in js
    assert "Move up" not in js and "dashboard-layout-menu" not in js
    assert "window.RCTAdmin = Object.freeze({ api, toast, messageFrom, element, confirmAction });" in admin_js
    assert "startDashboardLayout()" in admin_js and "rct:dashboard-ready" in js
    assert "initDashboardPolling()" in admin_js
    # dashboard.js never reimplements the polling helpers it must leave alone.
    for forbidden in ("function loadDashboard", "function initDashboardPolling", "setInterval"):
        assert forbidden not in js


def test_energy_poll_is_bounded_keeps_panels_and_never_overwrites_a_running_action():
    js = JS.read_text(encoding="utf-8")
    poll = js[js.index("async function pollEnergy("):js.index("function initEnergy(")]
    # A caller-initiated abort (timeout or supersede) must not open the reconnect modal.
    assert "error?.name === 'TimeoutError'" in js and "options.signal?.aborted" in js
    assert "ENERGY_POLL_TIMEOUT_MS" in poll and "signal: controller.signal" in poll
    # Panels of surviving devices are kept; polls requested before an action finished are dropped.
    assert "energyPanels.clear()" not in poll.split("const ordered")[0]
    assert "panel.acceptsPoll(requestedAt)" in poll
    assert "pendingMode ?? device.mode" in js
    # A selected mode that the unfinished setup blocks is shown as pending, never as active.
    assert "setupLead.hidden = !pendingSetup" in js and "'is-pending'" in js
    assert ".energy-mode-option.is-selected.is-pending" in (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    # The Setup step names the required writes that are not approved yet.
    assert "(device.required_write_names || []).filter((name) => !approved.has(name))" in js


def test_energy_mode_is_a_three_state_radio_group_with_mode_dependent_controls():
    js = JS.read_text(encoding="utf-8")
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    # Off / Manual / External as a radio group; the old on/off switch and the /armed call are gone.
    assert "setAttribute('role', 'radiogroup')" in js and "setAttribute('role', 'radio')" in js
    assert "['off', 'Off'" in js and "['manual', 'Manual'" in js and "['external', 'External'" in js
    assert "`${path}/mode`" in js and "/armed" not in js and "energy-armed" not in js
    # Keyboard: arrows move the focus only, a selection needs Space/Enter/click; aria-disabled keeps focus.
    assert "ArrowRight: 1" in js and "'Home'" in js and "aria-disabled" in js and "aria-checked" in js
    # Controls are enabled in Manual only; External shows the info line and disabled controls.
    assert "const enabled = operable && item.available && !busy && device.connected;" in js
    assert "This inverter is controlled by an external app through the API (PAT required)." in js
    assert "external: 'controlled by an external app'" in js and "mode_off:" in js
    # Write access off: hint with a link to the Inverters page.
    assert "modeLink.href = '/ui/inverters'" in js
    assert ".energy-mode-option" in css and ".energy-mode-option:focus-visible" in css


def test_destructive_actions_use_the_themed_confirm_modal_not_native_confirm():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'id="confirm-modal"' in base and 'id="confirm-accept"' in base
    for name in ("admin.js", "dashboard.js"):
        js = (ADMIN_DIR / "static/js" / name).read_text(encoding="utf-8")
        assert not re.search(r"(?<![\w.])confirm\(", js)


def test_toasts_are_dismissible_and_errors_do_not_vanish_quickly():
    js = JS.read_text(encoding="utf-8")
    toast = js[js.index("function toast("):js.index("function messageFrom(")]
    assert "'Dismiss notification'" in toast and "'mouseenter'" in toast and "'focusin'" in toast
    assert "danger: 30000" in js and "warning: 12000" in js


def test_one_time_token_cannot_be_dismissed_by_backdrop_or_escape():
    js = JS.read_text(encoding="utf-8")
    tokens = js[js.index("function initTokens()"):js.index("// Business labels for the Energy Manager")]
    assert "'hide.bs.modal'" in tokens and "event.preventDefault()" in tokens
    assert "$('copy-token').focus()" in tokens


def test_token_copy_button_is_icon_only_and_done_waits_for_a_successful_copy():
    html = (ADMIN_DIR / "templates/tokens.html").read_text(encoding="utf-8")
    copy = html[html.index('id="copy-token"'):html.index("</button>", html.index('id="copy-token"'))]
    assert 'aria-label="Copy token"' in copy and 'title="Copy token"' in copy
    assert 'class="material-icons"' in copy and ">Copy<" not in copy
    assert re.search(r'id="token-done"[^>]*\bdisabled\b', html)
    assert re.search(r'id="token-close"[^>]*\bdisabled\b', html)
    js = JS.read_text(encoding="utf-8")
    tokens = js[js.index("function initTokens()"):js.index("// Business labels for the Energy Manager")]
    assert "if (!copied) return;" in tokens
    assert tokens.index("setCopied(true)") > tokens.index("await copyToClipboard")


def test_hardware_verification_form_is_expert_only_and_the_basic_step_only_points_to_it():
    js = JS.read_text(encoding="utf-8")
    advanced = js[js.index("function energyAdvanced("):js.index("// Shows one status paragraph in the list")]
    # The form lives in the Expert section; no Setup slot hosts it, so Basic mode never shows
    # strategy code or byte widths.
    assert "verifyExpertSlot.append(verifyForm)" in advanced
    assert "verifySetupSlot" not in advanced and "place(verifyForm" not in advanced
    # The Basic step offers a button that opens the guided assistant; it never switches Expert mode on.
    button = advanced[advanced.index("const openVerifyButton"):advanced.index("const renderHardwareText")]
    assert "'Verify hardware'" in button and "addEventListener('click'" in button
    assert "openVerificationAssistant(" in button and "expertSwitch" not in button and "setExpert(true)" not in advanced
    # The assistant stores nothing itself: it calls the server steps and commits only on the explicit save click.
    assistant = js[js.index("function openVerificationAssistant("):js.index("function energyAdvanced(")]
    assert "post('control-test', { confirm: true })" in assistant and "post('commit', { confirm: true })" in assistant
    assert "innerHTML" not in assistant and "hardware-verification" not in assistant


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1a240c3bd217e006310ae09a676428bf099c827b", "1a240c3"),
        ("ABCDEF1234", "abcdef1"),
        (None, "dev"),
        ("", "dev"),
        ("unknown", "dev"),
        ("abc12", "dev"),
        ("<script>alert(1)</script>", "dev"),
        ("abc1234\n<b>", "dev"),
    ],
)
def test_resolve_build_accepts_only_hex(raw, expected):
    assert resolve_build(raw) == expected


def test_banner_shows_build_hash(monkeypatch):
    from app import banner

    monkeypatch.setattr(banner, "__build__", "abc1234")
    banner.build_info.cache_clear()
    try:
        assert "(abc1234)" in banner.banner()
    finally:
        banner.build_info.cache_clear()


def test_manual_mode_toast_does_not_promise_control_while_the_setup_is_incomplete():
    js = JS.read_text(encoding="utf-8")
    assert "control stays locked until the setup steps below are complete" in js
    assert "wanted === 'manual' && energyNeedsSetup(energyChecklist(device))" in js


def test_hardware_step_text_names_the_guided_check_and_the_expert_path():
    js = JS.read_text(encoding="utf-8")
    assert "A guided check reads the inverter" in js and "Nothing is guessed" in js
    assert "Experienced administrators can instead enter measured values under Expert settings" in js


def test_tsdb_page_uses_the_shared_page_heading_cards_and_setting_rows():
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    html = (TEMPLATES / "tsdb.html").read_text(encoding="utf-8")
    # No TSDB-specific layout or typography rules: only the log console keeps its own look, plus
    # .export-card, which only tightens the setting-row padding so the page fits one viewport.
    own = set(re.findall(r"\.(tsdb-[a-z-]+|export-[a-z-]+)", css))
    assert own <= {"tsdb-console", "tsdb-console-line", "tsdb-console-time", "export-card"}, own
    assert 'class="page-heading"' in html and 'id="save-state" class="save-state' in html
    assert 'class="settings-grid"' in html and "tsdb-status-badge" not in html
    render = js[js.index("function renderExportSettings("):js.index("// Mirrors the server-side host plausibility")]
    assert "element('h2', 'h5 mb-2', title)" in render and "'Status & Log'" in render
    assert "role', 'log'" in render and "aria-live', 'polite'" in render
    assert "Status & log" not in js



def test_a_missing_metric_value_reads_n_a_in_every_formatter_and_placeholder():
    js = JS.read_text(encoding="utf-8")
    dash = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    # One spelling for "no value", whatever the reason; the unit is never attached to it.
    assert "const NOT_AVAILABLE = 'n/a';" in js
    for fn in ("function metricParts(", "function formatPower(", "function formatPowerKw(", "function formatPercent("):
        body = js[js.index(fn):js.index("\n  }\n", js.index(fn))]
        assert "NOT_AVAILABLE" in body and "'–'" not in body, fn
    assert "'–'" not in js[js.index("function energyFlowGraphic("):js.index("function energyAvailability(")]
    # The first paint before any data is the same text as a missing value.
    for tile in ("pv-power", "grid-power", "house-power", "battery-soc", "tsdb-status-label", "metric-count",
                 "device-count", "connected-count", "days-to-calibration"):
        assert f'<strong id="{tile}">n/a</strong>' in dash, tile


def test_tsdb_tile_shows_n_a_without_an_export_value_and_keeps_the_reason_secondary():
    js = JS.read_text(encoding="utf-8")
    tile = js[js.index("function renderTsdbTile("):js.index("function renderDashboard(")]
    assert "label.textContent = 'Not configured'" not in tile and "'–'" not in tile
    assert tile.count("label.textContent = NOT_AVAILABLE") == 2
    # The reason survives as a tooltip / accessible name, never as the primary value.
    assert "Not configured" in tile and "label.title" in tile
    # A real state of the running export stays a value.
    assert "'Sending'" in tile and "'Failing'" in tile


def test_all_status_badges_share_one_component_with_fixed_tones():
    js = JS.read_text(encoding="utf-8")
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    tones = ("success", "neutral", "warning", "danger")
    # Every tone is defined once, light and dark through Bootstrap's theme-aware variables.
    for tone in tones:
        rule = re.search(rf"\.status-badge-{tone}\s*{{([^}}]*)}}", css)
        assert rule and "--status-badge-fg" in rule[1] and "--status-badge-bg" in rule[1], tone
        assert "#" not in rule[1] and "rgb(" not in rule[1], f"{tone} must use theme variables, not hardcoded colours"
    base = re.search(r"\n\.status-badge\s*{([^}]*)}", css)
    assert base and all(prop in base[1] for prop in ("padding", "border-radius", "font-size", "font-weight"))
    assert re.search(r"\.status-badge::before\s*{[^}]*width[^}]*height", css), "the status dot is part of the component"
    # The page-head badge, the card-head chips, the flow states and the token pills all use it.
    for creation in ("'status-badge status-badge-success'", "element('span', 'status-badge status-badge-neutral')",
                     "'flow-badge-state status-badge status-badge-neutral'", "status-badge ${write ? 'status-badge-warning' : 'status-badge-success'}"):
        assert creation in js, creation
    assert "'status-badge-warning', Boolean(notice)" in js and "'status-badge-neutral', !notice" in js
    assert "'status-badge-success', favourable === true" in js and "'status-badge-neutral', favourable !== true" in js
    assert "status-badge-danger" not in js[js.index("function setBadge("):js.index("function setLine(")]
    # The old per-badge variants are gone; no other rule sets a badge's font or padding.
    for legacy in ("device-status-badge", "device-chip", "token-role-", "flow-badge-state.is-", "badge text-bg-warning"):
        assert legacy not in css and legacy not in js, legacy
    for selector, body in re.findall(r"([^{}]+){([^}]*)}", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)):
        if "status-badge" in selector and selector.strip() != ".status-badge" and "::before" not in selector:
            assert not re.search(r"(?:^|;)\s*(?:font-|padding|height|border-radius)", body), selector
    # A stale reading must not dim the flow badges: that made their contrast flicker between polls.
    assert ".flow-badge.is-stale" not in css and "opacity" not in css[css.index(".flow-badge {"):css.index(".flow-badge-caption {")]
    set_badge = js[js.index("function setBadge("):js.index("function setLine(")]
    assert "is-stale" not in set_badge


# --- Layout contract: one card gap, one stacking rule, one shared parameter table -------------------

CSS = ADMIN_DIR / "static/css/admin.css"

# Pages whose cards, grids or export row stack below a .page-heading.
STACKED_PAGES = ("prometheus", "tokens", "inverters", "settings", "tsdb", "energy", "about")


def _css_rules() -> list[tuple[str, str]]:
    """(selector, body) of every innermost rule, comments removed, whitespace collapsed."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.DOTALL)
    return [(" ".join(sel.split()), " ".join(body.split())) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def test_card_gap_token_is_declared_once_in_root_as_twenty_pixels():
    css = CSS.read_text(encoding="utf-8")
    assert len(re.findall(r"--rct-card-gap\s*:", css)) == 1
    root = [body for sel, body in _css_rules() if sel == ":root" and "--rct-card-gap" in body]
    assert len(root) == 1 and re.search(r"--rct-card-gap:\s*1\.25rem\s*;", root[0])


def test_one_sibling_rule_spaces_every_top_level_card_below_the_page_heading():
    spacing = [(sel, body) for sel, body in _css_rules() if "page-heading~" in sel.replace(" ", "")]
    assert len(spacing) == 1, spacing
    selector, body = spacing[0]
    for block in (".card", ".settings-grid", ".energy-grid", "#export-actions"):
        assert block in selector, block
    assert ".about-top-row~.card" in selector.replace(" ", "")
    assert body.rstrip(";").strip() == "margin-top: var(--rct-card-gap)"
    # No second rule may re-space the same blocks: that is how the pages drifted apart before.
    blocks = {".card", ".settings-grid", ".energy-grid", "#export-actions", ".about-top-row", ".page-heading"}
    for sel, body in _css_rules():
        parts = {part.strip() for part in sel.split(",")}
        if parts & blocks and sel != selector and "page-heading~" not in sel.replace(" ", ""):
            assert not re.search(r"(?:^|;)\s*margin(?:-top|-block-start)?\s*:", body), sel


def test_grid_gaps_and_about_gutters_come_from_the_card_gap_token():
    rules = _css_rules()
    for grid in (".settings-grid", ".energy-grid"):
        bodies = [body for sel, body in rules if grid in {part.strip() for part in sel.split(",")}]
        gaps = [value for body in bodies for value in re.findall(r"(?:^|;)\s*((?:row-|column-)?gap)\s*:\s*([^;]+)", body)]
        assert gaps, grid
        assert all(value.strip() == "var(--rct-card-gap)" for _, value in gaps), (grid, gaps)
    row = [body for sel, body in rules if sel.endswith(".about-top-row")]
    assert row and "--bs-gutter-x: var(--rct-card-gap)" in row[0] and "--bs-gutter-y: var(--rct-card-gap)" in row[0]


def test_stacked_page_templates_carry_no_margin_utilities_on_top_level_blocks():
    block_class = re.compile(r"(?:^|\s)(?:card|settings-grid|energy-grid)(?:\s|$)")
    for page in STACKED_PAGES:
        html = (TEMPLATES / f"{page}.html").read_text(encoding="utf-8")
        for tag in re.findall(r"<(?:section|div|details|header)\b[^>]*>", html):
            classes = re.search(r'class="([^"]*)"', tag)
            blocks = bool(classes and block_class.search(classes[1])) or 'id="export-actions"' in tag
            if blocks:
                assert not re.search(r"(?:^|[\s\"])(?:mt|my)-[34](?:\s|\")", tag), (page, tag)


def test_prometheus_and_inverters_build_their_table_from_the_one_shared_macro():
    macro = (TEMPLATES / "_param_table.html").read_text(encoding="utf-8")
    for page in ("prometheus", "inverters"):
        html = (TEMPLATES / f"{page}.html").read_text(encoding="utf-8")
        assert '{% from "_param_table.html" import param_table %}' in html, page
        assert html.count("{{ param_table(") == 1, page
        # The table, search and pager exist only inside the macro.
        assert "<table" not in html and "btn-group" not in html, page
    for suffix in ("title", "search", "list", "range", "prev", "next"):
        assert f'id="{{{{ prefix }}}}-{suffix}"' in macro, suffix
    for shared in ("param-table-wrap", "param-table", "btn btn-primary", "btn-group btn-group-sm"):
        assert shared in macro, shared
    js = JS.read_text(encoding="utf-8")
    assert "const EXPOSED_PAGE_SIZE = 15;" in js
    assert "renderParameterTable('exposed'" in js and "renderParameterTable('writable'" in js


@pytest.mark.asyncio
async def test_prometheus_and_inverters_render_the_same_table_card_structure(tmp_path):
    def card_classes(html: str, prefix: str) -> set[str]:
        section = re.search(rf'<section class="card param-card" aria-labelledby="{prefix}-title">.*?</section>', html, re.DOTALL)
        assert section, prefix
        return {name for value in re.findall(r'class="([^"]*)"', section[0]) for name in value.split()}

    async with _logged_in(tmp_path) as client:
        exposed = card_classes((await client.get("/ui/prometheus")).text, "exposed")
        writable = card_classes((await client.get("/ui/inverters")).text, "writable")
    # Only the drag handle column is Prometheus-specific.
    assert exposed - {"col-handle", "param-table-reorder"} == writable
    assert {"param-table", "param-table-wrap", "btn-primary", "btn-group-sm"} <= writable


def test_incomplete_export_group_is_reported_by_toast_and_on_the_field_not_by_a_banner():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    render = js[js.index("function renderSaveState()"):js.index("function reportSaveFailure(")]
    assert "toast(entry.message, 'warning')" in render and "lastIncompleteToast.get(id) !== entry.message" in render
    assert "sectionNotice(id, 'hint')" not in render  # no persistent yellow banner
    mark = js[js.index("function markMissingExportFields()"):js.index("function queueExportSettings()")]
    assert "aria-invalid" in mark and "control.dataset.touched === '1'" in mark


def test_primary_button_themes_its_disabled_and_focus_state():
    css = CSS.read_text(encoding="utf-8")
    rule = css[css.index(".btn-primary {"):css.index(".btn-outline-primary {")]
    assert "--bs-btn-disabled-bg: var(--rct-brand);" in rule and "--bs-btn-focus-shadow-rgb: var(--bs-primary-rgb);" in rule



def test_device_header_serial_number_is_text_only_and_omitted_when_unknown():
    js = JS.read_text(encoding="utf-8")
    assert "if (serial) setText(ref.serial, `Serial number: ${serial}`);" in js
    assert "ref.serial.innerHTML" not in js
    assert "...(serial ? [ref.serial] : [])" in js
