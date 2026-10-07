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

from app import __version__
from app.admin import updates
from app.api.app_factory import create_app
from app.config import Settings

ADMIN_DIR = Path(__file__).resolve().parent.parent / "app" / "admin"


JS = ADMIN_DIR / "static/js/admin.js"


TEMPLATES = ADMIN_DIR / "templates"


# The Swagger UI sidebar ships user-visible text too, so it is covered by the English-only check.
DOCS_NAV = Path(__file__).resolve().parent.parent / "app" / "api" / "docs_nav.py"


PAGES = ("dashboard", "inverters", "tsdb", "prometheus", "tokens", "settings")


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
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as anonymous:
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


def test_header_is_sticky_above_content_and_toasts_stay_on_top():
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    assert re.search(r"\.admin-navbar\s*\{[^}]*position:\s*sticky;\s*top:\s*0;\s*z-index:\s*1030", css)
    assert re.search(r"\.mobile-nav\s*\{[^}]*position:\s*sticky;\s*top:\s*4\.1rem", css)
    assert re.search(r"html\s*\{\s*scroll-padding-top", css)
    assert int(re.search(r"\.toast-region\s*\{[^}]*z-index:\s*(\d+)", css).group(1)) > 1030


def test_device_editor_has_host_port_and_network_id_fields():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "Device ID" not in js and "Display name" not in js
    assert ("data-field" in js or "dataset.field" in js) and "Add inverter" in js
    # The network id is part of the backend uniqueness key, so master/slave setups need the field.
    assert "'network_id'" in js and "Network ID" in js
    assert "networkLabel.htmlFor" in js and "networkInput.inputMode = 'numeric'" in js
    # The blank trailing row goes through the same single place that mints a device identity.
    assert "devices.push(withUid({ host: '', port: 8899, network_id: null }))" in js


def test_device_duplicate_rule_matches_the_backend_key_and_autosave_keeps_local_edits():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "networkId(other.network_id) === network" in js  # host, port and network id together
    assert "Object.assign(device, (result.settings" not in js  # no clobbering of newer local input
    assert "if (assigned && !device.device_id && assigned.device_id) device.device_id = assigned.device_id;" in js


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


def test_navbar_shows_the_short_label_and_an_enlarged_logo():
    base = (ADMIN_DIR / "templates/base.html").read_text(encoding="utf-8")
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    assert "<span>Administration</span>" in base and "<span>RCT Administration</span>" not in base
    # The browser-tab title keeps the full product name.
    assert "{% block title %}RCT Administration{% endblock %}" in base
    assert re.search(r"\.nav-logo\s*\{[^}]*width:\s*5\.1rem;\s*height:\s*2\.78rem", css)
    # Header buttons shrink but stay above a 2.2rem touch target.
    assert re.search(r"\.navbar\s\.btn\s*\{[^}]*height:\s*2\.3rem", css)


def test_settings_grid_cards_share_the_row_height():
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    assert re.search(r"\.settings-grid\s*\{[^}]*align-items:\s*stretch", css)
    assert re.search(r"\.settings-grid>\.card>\.card-body\s*\{\s*flex:\s*1 1 auto;\s*\}", css)
    # The TSDB export grid shares the same row-stretch alignment, so cards in one row match height.
    assert re.search(r"\.export-grid\s*\{[^}]*align-items:\s*stretch", css)
    assert re.search(r"\.export-grid>\.card>\.card-body\s*\{\s*flex:\s*1 1 auto;\s*\}", css)


def test_metrics_fields_hide_behind_the_master_toggle():
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "label: 'Enable Metrics Endpoint'" in js
    for key in ("metrics_require_token", "metrics_trusted_sources",
                "metrics_rate_limit_requests", "metrics_rate_limit_window_seconds"):
        row = next(line for line in js.splitlines() if f"key: '{key}'" in line)
        assert "requires: 'enable_metrics_endpoint'" in row, key
    # Only the affected group is rebuilt, and the toggle keeps the focus it was operated with.
    assert "function settingVisible(field)" in js
    assert "gatesOtherFields(field.key)) preserveFocus(() => rerenderGroup(field.group))" in js


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
    """Finding 4 (MEDIUM): an incomplete export configuration must drop the already-queued fields
    *and* stop the debounce timer, and the group must be re-checked immediately before sending."""
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    assert "let pendingExportGroup = false;" in js
    queue = js[js.index("function queueExportSettings()"):js.index("function renderExportSettings(")]
    assert "pendingExportGroup = false;" in queue
    assert "setSectionState('export', 'incomplete', missingExportHint(type));" in queue
    assert "if (!hasPending(null)) clearTimeout(settingsTimer);" in queue
    # The send-time re-check sits in the synchronous wrapper, before any key is computed.
    send = js[js.index("function sendSettings(sections = null)"):js.index("function secretKeysIn(")]
    assert "if (pendingExportGroup && !exportReady(type)) {" in send
    assert "if (pendingKeys.has('devices') && !devicesValid()) {" in send
    assert send.index("if (pendingExportGroup && !exportReady(type)) {") < send.index("const keys = keysFor(sections);")


def test_missing_export_hint_names_both_failure_causes():
    """Review item 8 (MEDIUM): exportReady() also fails for an unpaired username/password, where
    every required key is present — a single "… is required" message would name nothing missing."""
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    hint = js[js.index("function missingExportHint(type)"):js.index("// Export fields autosave as one")]
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
    assert "if (key === 'devices') continue;" in catch_block
    assert "if (isSecretKey(key)) continue;" in catch_block
    assert "structuredClone(settingsCommitted.devices" not in catch_block
    assert "for (const id of sentSections) reportSaveFailure(id, failureMessage(id, reason));" in catch_block


def test_device_id_is_matched_back_by_host_port_and_network_id_not_array_index():
    """Review item 10 (LOW): the server is not guaranteed to echo devices back in the exact order
    they were sent, so matching by array index can assign the wrong id to the wrong row."""
    js = (ADMIN_DIR / "static/js/admin.js").read_text(encoding="utf-8")
    success_block = _send_block(js, "success")
    assert "sentDevices.forEach((device) => {" in success_block
    assert "(result.settings?.devices || [])[i]" not in success_block
    assert ".find((candidate) =>" in success_block


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
    resolved, so "Add inverter" closed the dialog on a failed save."""
    js = JS.read_text(encoding="utf-8")
    flush = js[js.index("async function flushSettings({ sections = null } = {})"):js.index("// Synchronous wrapper")]
    assert "const result = await inFlight.promise.catch(() => false);" in flush
    # Only a request that carried one of the caller's sections may decide the caller's result.
    assert "if (!sections || sections.some((id) => inFlight.sections.has(id))) ok = result && ok;" in flush
    assert "if (requestedIncomplete(sections)) return false;" in flush
    assert "ok = (await sendSettings(sections).catch(() => false)) && ok;" in flush
    assert "return true;" not in flush
    # Add inverter flushes scoped and keeps the dialog open on a false result.
    add = js[js.index("async function addInverter(card, button)"):js.index("function buildGroupBody(")]
    assert "if (!(await flushSettings({ sections: ['devices'] }))) return;" in add
    assert "button.disabled = true;" in add and "clearTimeout(settingsTimer);" in add


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
    assert "if (hasPending(null)) restartDebounce();" in finally_block
    assert "flushSettings(" not in finally_block
    # Exactly one declaration, re-shaped from a bare promise rather than declared a second time.
    assert js.count("let settingsInFlight") == 1
    assert "await settingsInFlight?.catch" not in js
    # The beforeunload guard is derived, and a rolled-back failure does not warn forever.
    outstanding = js[js.index("function outstandingWork()"):js.index("function reportSaveFailure(")]
    assert "anySection(['saving', 'incomplete'])" in outstanding and "failed" not in outstanding


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
    render = js[js.index("function renderSaveState()"):js.index("// Derived on demand")]
    for text in ("Save failed", "Saving …", "Unsaved changes", "Incomplete — not saved yet"):
        assert text in js, text
    assert "`Saved ${hhmm(savedAt)}`" in render
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
    assert "export:" in retries and "devices:" in retries
    assert "general:" not in retries and "parameters:" not in retries
    assert "discard.addEventListener('click', discardDeviceChanges);" in js
    # The parameters path is wired, not just declared.
    assert "setSectionState('parameters', 'unsaved');" in js
    assert "setSectionState('parameters', 'saving');" in js
    assert "reportSaveFailure('parameters', failureMessage('parameters', messageFrom(error)));" in js


def test_devices_are_bound_by_a_stable_identity_not_by_array_index():
    """Finding 2 (HIGH): replacing settingsDraft.devices (and every object in it) on a failed save
    orphans the objects the live change handlers and the row DOM still reference."""
    js = JS.read_text(encoding="utf-8")
    assert "function withUid(device) { if (!device._uid) device._uid = `d${++deviceUid}`; return device; }" in js
    assert "function adoptDevices(list) { settingsDraft.devices = (list || []).map(withUid); return settingsDraft.devices; }" in js
    assert js.count("device._uid = `d${++deviceUid}`") == 1  # exactly one place mints a _uid
    assert "if (Object.hasOwn(settingsDraft, 'devices')) adoptDevices(settingsDraft.devices);" in js
    assert "function deviceByUid(uid) { return (settingsDraft.devices || []).find((device) => device._uid === uid) || null; }" in js
    # Both lookups resolve by _uid and skip a miss, which is a real state right after Discard changes.
    assert js.count("const device = deviceByUid(row.dataset.uid);") == 2
    assert js.count("if (!device) continue;") >= 2
    assert "settingsDraft.devices[Number(row.dataset.index)]" not in js
    assert "row.dataset.uid = device._uid;" in js
    # The payload is explicit instead of a spread, so no client-only key rides along.
    payload = js[js.index("function devicesPayload()"):js.index("function devicesValid()")]
    assert "{ ...device" not in payload
    for field in ("host:", "port:", "network_id:", "device_id:", "display_name:"):
        assert field in payload, field


def test_device_remove_handler_persists_the_removal_and_keeps_focus():
    js = JS.read_text(encoding="utf-8")
    handler = js[js.index("remove.addEventListener('click', () => {"):js.index("const networkHelp = element(")]
    assert "if (wasSaved && !confirm(" in handler          # only a saved row asks
    assert "const at = devices.indexOf(device);" in handler  # identity, not the captured index
    assert "if (at < 0) return;" in handler
    assert "devices.splice(index, 1)" not in js
    assert "rebuildDeviceSection();" in handler
    assert ".device-settings ~ button" in handler           # focus falls back to the Add button
    assert "queueSettings('devices');" in handler           # the removal is what persists it
    # The section owner keeps the state unstuck when a row becomes valid again.
    state = js[js.index("function refreshDeviceState(card)"):js.index("function rebuildDeviceSection()")]
    assert "pendingKeys.delete('devices');" in state
    assert "setSectionState('devices', pendingKeys.has('devices') ? 'unsaved' : 'idle');" in state
    assert "function devicesValid()" in js


def test_build_group_body_is_shared_so_a_group_rerender_adds_no_second_heading():
    js = JS.read_text(encoding="utf-8")
    builder = js[js.index("function buildGroupBody(group)"):js.index("function groupSectionId(")]
    assert "page === 'prometheus' && group === 'Prometheus'" in builder
    assert "{ Account: 'Administrator account' }[group]" in builder
    # One copy of the heading conditional, used by both callers.
    assert js.count("page === 'prometheus' && group === 'Prometheus'") == 1
    assert js.count("buildGroupBody(group)") == 3  # definition plus both call sites
    assert "section.replaceChildren(buildGroupBody(group));" in js
    assert "if (!section) { renderSettings(); return; }" in js
    # Slugified, so a group name with a space still yields a valid id.
    assert "return `settings-group-${group.toLowerCase().replace(/\\s+/g, '-')}`;" in js


def test_dashboard_has_no_stale_data_banner_and_still_records_last_update():
    """The stale-data warning banner was removed from the Overview page per user request; the
    independent 'last updated' timestamp line and the KPI/device polling it does not drive stay."""
    js = JS.read_text(encoding="utf-8")
    html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    for marker in ('id="dashboard-staleness"', 'id="dashboard-stale-text"', 'id="refresh-dashboard"',
                   'markDashboardStale', 'dataset.stale', 'dashboard-stale-text'):
        assert marker not in html and marker not in js, marker
    assert 'id="dashboard-updated"' in html
    load = js[js.index("async function loadDashboard("):js.index("function initDashboardPolling()")]
    assert "updated.textContent = `Updated ${hhmm(dashboardLastSuccess)}`;" in load
    css = (ADMIN_DIR / "static/css/admin.css").read_text(encoding="utf-8")
    assert ".dashboard-staleness" not in css
    assert "data-stale" not in css


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
    # Device rows reference their row-level error node and mark only the offending input.
    assert "feedback.id = `device-${index}-feedback`;" in js
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
async def test_about_requires_session_and_renders_project_details(tmp_path):
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
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
        assert "Application Details" in response.text
        assert "Dependencies" in response.text
        assert "<code>" in response.text
        assert response.text.count("/rct-manager/releases") == 1
        assert "https://gill-bates.github.io/rct-manager/" in response.text
        assert "https://github.com/Gill-Bates/rct-manager/releases" in response.text
        assert (await client.get("/admin/static/css/about.css")).status_code == 200
