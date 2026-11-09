# Design

## 1 Scope, status and normative language

Status: Active. Updated 2026-11-09. This contract governs the RCT Manager administration GUI (`/ui/*`, served from `app/admin/`): the shared shell, the Overview dashboard, Inverters, Energy Manager (Inverter, Charging plan, Spot Market, Weather), TSDB, Prometheus, API tokens, Settings, About, the standalone login and password-change screens, the shared dialogs and every future UI route. It records the visual and interaction language that exists today and is not a redesign. Where the code deviates from a rule, the deviation is listed in 10.5 as an open requirement; it is not silently accepted. A change to a shared rule or component updates this document and its owning check in the same change.

The document was derived from `app/admin/static/css/admin.css`, `dashboard.css`, `about.css`, `app/admin/templates/*.html`, `app/admin/static/js/*.js` and the existing design tests. Its structure follows the design contract of the sibling project justUp! so that both read alike; the product decisions are RCT Manager's own.

### 1.1 Normative language

| Word | Meaning |
| --- | --- |
| MUST, MUST NOT | A verifiable requirement. It names a check, or states why it cannot be automated. |
| SHOULD, SHOULD NOT | The default. A deviation needs a reason in the change that deviates. |
| MAY | A permitted variation. Choosing it needs no exception. |

- **DC-NORM-1** Only a sentence with one of these words, or a row of a rule table, is a requirement. Other text describes how something works, or gives rationale, and never adds a requirement.
- **DC-NORM-2** Rules have stable IDs (`DC-<AREA>-<n>`). A test, a code comment or a later change references the ID. An ID is never reused for a different rule.
- **DC-NORM-3** Absolute words (always, every, never, only, exactly) hold inside the scope the sentence names. Where no scope is named, the scope is the section that contains the rule. An absolute rule has no exception unless an exception row (DC-EXC-1) exists.
- **DC-NORM-4** A numeric requirement states unit, scope and measurement condition. A tolerance is a named constant in the owning test, not contract text.

### 1.2 Measurement vocabulary

| Term | Definition |
| --- | --- |
| CSS viewport | Width x height in CSS pixels, for example 320x640, 390x844, 1280x720. |
| Content zoom | `--content-zoom` (0.9), applied as CSS `zoom` to `#main-content` only. Header, sidebar and footer stay at 100 %. A size inside `#main-content` that must reach N rendered pixels is declared as `N / var(--content-zoom)`. |
| Reflow | No horizontal page scroll at a CSS viewport 320 px wide. |
| Evidence level E-ENGINE | A desktop or emulated-device run in Chromium, WebKit or Firefox (`tests/e2e/harness.mjs`, `BROWSER=`). Playwright WebKit is E-ENGINE. |
| Evidence level E-IOS | A run on a real iPhone or iPad, or in the iOS Simulator. |

- **DC-MEAS-1** A reflow check MUST use a CSS viewport 320 px wide. A label such as "200 % zoom" MUST NOT name a run that only changes the device pixel ratio.
- **DC-MEAS-2** Behaviour specific to iOS (on-screen keyboard, browser chrome, safe areas, input zoom) MUST be reported as "unverified" until an E-IOS run is recorded. E-ENGINE results, including `tests/e2e/ios_viewport_audit.mjs`, do not change that status.

## 2 Precedence and exceptions

- **DC-PREC-1** When rules conflict, the higher level wins:
  1. Unchangeable product decisions and security boundaries (section 3).
  2. Global invariants (sections 4 and 5: tokens, shell, scrolling, focus; the state taxonomy of 6.1).
  3. Component contracts (section 6). They refine a global invariant and MUST NOT silently contradict it.
  4. Feature contracts (section 9) and exception rows.
  5. Implementation, tests and code comments. They are evidence, not norms.
- **DC-PREC-2** A conflict is resolved in the same change by correcting the lower item or by adding an exception row. Existing code or a passing test is never a reason to keep a contradiction.
- **DC-PREC-3** A level-1 rule changes only by an explicit product decision. It is never relaxed through an exception row.
- **DC-EXC-1** An exception is a row in the exception table of the rule it modifies, with: ID (`DC-EX-<AREA>-<n>`), scope (surface and selector), rationale, owner (file) and the test that proves it. A row without a test is invalid.
- **DC-EXC-2** An exception is bounded by its scope and sets no precedent. A second surface with the same need extends the row or adds a shared variant (DC-EXT-2).
- **DC-EXC-3** Section 9 indexes all exception rows. The row stays with its rule, so each rule has one home.

## 3 Design principles and unchangeable product decisions

RCT Manager is a clear, dependable console for an RCT Power inverter and battery installation. Operators watch live power flow and state at a glance, then configure the Energy Manager, the time-series export and access. The GUI is used on a desktop and on a phone next to the installation, so both are first-class.

### 3.1 Principles

- **DC-PRIN-1** The UI uses one token system (section 4), Bootstrap 5.3 components refined in `admin.css`, visible text labels next to colour, and one flat navigation level.
- **DC-PRIN-2** Colour MUST NOT be the only carrier of status, selection, direction or series identity. A second cue (text, a dot or glyph, shape, a dash pattern, position) is required (WCAG 2.2 SC 1.4.1).
- **DC-PRIN-3** Light, dark and automatic themes share one structure. The theme is applied before first paint (DC-THEME-1).
- **DC-PRIN-4** Decorative effects MUST NOT compete with status information. Radii are modest, borders thin, shadows follow the elevation scale (DC-TOK-5).
- **DC-PRIN-5** Labels are short, concrete English terms already established in the product (Overview, Inverters, Energy Manager, TSDB). Titles are sentence case (DC-TITLE-1).
- **DC-PRIN-6** The GUI never tells the operator to restart the service. Settings apply on their own (DC-MSG-6).

### 3.2 Unchangeable product decisions

A change to one of these needs a new product decision (DC-PREC-3).

| ID | Decision | Scope |
| --- | --- | --- |
| DC-PD-1 | Brand red `--rct-brand` `#88171a` (hover `--rct-brand-hover` `#6f1215`) for the header, primary actions, the active navigation item and `.text-primary`. In dark mode `.text-primary` text is `#f1a0a3`. | All themes and pages. |
| DC-PD-2 | IBM Plex Sans and IBM Plex Mono as local `woff2` assets, Material Icons and all libraries (Bootstrap, Chart.js, GridStack) as local files under `static/`. The GUI MUST NOT request a font, icon, script or stylesheet from another origin at runtime. A link the operator follows (the GitHub link in the footer) is not a request. | Application pages. |
| DC-PD-3 | Navigation is the single ordered list `nav` in `base.html`: Overview, Inverters, Energy Manager, TSDB, Prometheus, API tokens, Settings, About. Desktop sidebar from 992 px, menu drawer below. No second navigation pattern as a side effect of a page. | Application shell. |
| DC-PD-4 | The header is brand red in both themes and holds the logo, the theme switch, Log out and (below 992 px) the menu toggle. | Application shell. |
| DC-PD-5 | Bootstrap is the component base. A component deviates from it only inside `admin.css` or the feature stylesheet that owns it, never inline. | All pages. |
| DC-PD-6 | The first run shows the mandatory usage disclaimer (`#disclaimer-modal`). It has a static backdrop, no keyboard dismissal and no close control; the server lock enforces acceptance. | Overview and every application page. |
| DC-PD-7 | Overview layouts use the dashboard layout contract of 9.1 (widget identities from `widget_ids.json`, layout version 1). | Dashboard persistence. |

### 3.3 Extension rules

- **DC-EXT-1** Extend the nearest existing page or component pattern. Do not introduce a parallel design system. Application pages extend `base.html` and use `page-heading`, `card`, `settings-grid`, `setting-row`, tabs and the table patterns of section 6. Login and password change keep the standalone `auth-shell` layout and share type, theme, controls and tokens.
- **DC-EXT-2** Reuse an existing component or variant first. A shared variant is added only for an intentional, reusable difference. A one-off colour, spacing, radius, shadow or type variant for a single page is not allowed.
- **DC-EXT-3** A page arranges its own content but MUST NOT redefine the palette, common controls, the card gap or the appearance of a shared component.
- **DC-EXT-4** A visual change MUST NOT change a route, permission boundary, stored preference, API contract, keyboard support, element IDs or Bootstrap hooks.
- **DC-EXT-5** An interactive component defines each applicable state: default, hover, focus, active, disabled, loading, empty, success, error. A state that does not apply is stated as not applicable in the component's spec or test.
- **DC-EXT-6** New presentation rules go to the shared or feature stylesheet, not to inline styles or `style` attributes written by script (a computed geometry value is allowed). When an existing component is touched, its obsolete local overrides are removed; unrelated legacy markup is not refactored.
- **DC-EXT-7** Do not add a restyle of a Bootstrap utility class to a page. A utility that must change globally (`.text-secondary`, `.text-primary`) is overridden once in `admin.css` with a comment that names the reason.

## 4 Design tokens and technical owners

### 4.1 Token rules

- **DC-TOK-1** Brand, surface and focus tokens are `--rct-*` custom properties in the `:root` block of `admin.css`, with the dark redefinitions in `[data-bs-theme="dark"]`. Theme-dependent Bootstrap values are read from `--bs-*` (`--bs-body-bg`, `--bs-tertiary-bg`, `--bs-secondary-color`, `--bs-border-color`, the semantic `--bs-*-text-emphasis` and `--bs-*-bg-subtle`). A feature stylesheet defines only tokens for its own geometry or series colours (`--flow-*`, `--spot-market-*-line`, `--consumption-*`, `--status-badge-*`).
- **DC-TOK-2** A raw colour (hex, `rgb()`) appears only in a token definition, in a documented fixed-palette surface (the TSDB log console), in `forced-colors` rules that use system colours, and in the cases of the exception table below. A component reads a token. A component never writes its own shadow colour.
- **DC-TOK-3** A value that CSS and JavaScript both need (series colours, breakpoints, geometry) has one source. Script reads the CSS custom property with `getComputedStyle`, as the chart code does for `--spot-market-*-line`, `--rct-plan-power`, `--rct-plan-soc` and `--consumption-*`. Script MUST NOT hold a second copy of a colour.
- **DC-TOK-4** `!important` is allowed only to override third-party inline or utility styles (Bootstrap utilities, GridStack inline geometry) and in the reduced-motion reset, with a comment that names the target. It MUST NOT be used for focus, modals or tabs. Existing uses are reviewed when their rule is touched; there is no sweep.
- **DC-TOK-5** Elevation has three levels: card (`.card`, subtle), chrome (header, footer) and floating (toast, modal, search results). A component MUST NOT introduce a fourth level. The values are literals in `admin.css` today; promoting them to tokens is open (10.5).
- **DC-TOK-6** Non-essential animation and transition MUST be disabled or reduced under `prefers-reduced-motion: reduce`. `admin.css` has a global reset (`transition-duration` and `animation-duration` to `.01ms`, `scroll-behavior: auto`). A feature that animates also guards its own animation (toast slide, flow dots, onboarding arrow, reconnect pulse).
- **DC-TOK-7** Fonts: `font-display: swap` for IBM Plex Sans and Mono. No second or engine-specific font. Icons are Material Icons (`.material-icons`, `aria-hidden="true"` when decorative).
- **DC-TOK-8** The card gap is `--rct-card-gap`. Cards and grids that follow the page heading get their top margin from the single rule in `admin.css`; a page MUST NOT add `mt-*` to them.

| ID | Exception to | Scope | Rationale | Owner | Test |
| --- | --- | --- | --- | --- | --- |
| DC-EX-TOK-1 | DC-TOK-2 (raw colour) | `.tsdb-console` and its level colours | The log console is a fixed dark terminal surface in both themes; its text colours are chosen against `#0b0e11`. | `admin.css` | `tests/test_admin_design_rules.py` (R8 allow-list) |

### 4.2 Owners

- **DC-OWN-1** Stylesheet responsibilities: `admin.css` owns the palette, shell, navigation, baseline buttons, forms, cards, tables, badges, toasts, dialogs and the Energy Manager and settings pages. `dashboard.css` owns the GridStack layer and widget geometry of the Overview. `about.css` owns the About page. Scripts own behaviour and persistence, never appearance. New rules go to these owners and reuse the shared tokens.
- **DC-OWN-2** Each topic has one normative section and one owner per layer. A change edits the owner, not a copy.

| Topic | Normative rule | CSS owner | JS owner | Template owner | Test owner |
| --- | --- | --- | --- | --- | --- |
| Palette, focus, switch tokens | 4.1, 5.5 | `admin.css` | `theme.js` | `base.html` | `tests/test_admin_ui.py`, `tests/e2e/contrast_audit.mjs` |
| Theme | 7.3 | `admin.css` | `theme.js`, `admin-core.js` | `base.html` | `tests/js/theme.test.js` |
| Shell, header, footer, drawer | 5.1, 5.4 | `admin.css` | `admin-core.js` | `base.html` | `tests/e2e/mobile_layout.mjs`, `tests/e2e/ios_viewport_audit.mjs` |
| Status badge, flow badge | 6.1 | `admin.css` (`.status-badge*`) | `admin-dashboard-cards.js`, `admin-energy-controls.js` | page templates | `tests/test_admin_design_rules_badges.py` |
| Toast and section notices | 6.2 | `admin.css` (`.toast-region`, `.save-alert`) | `admin-core.js` (`toast`), `admin-settings.js` | `base.html` | `tests/test_admin_ui.py` |
| Forms and apply bar | 6.2 | `admin.css` (`.setting-row`) | `admin-settings.js` (`buildFieldControl`, `buildApplyBar`) | settings and tsdb templates | `tests/test_admin_design_rules.py` (R5, R9) |
| Dialogs | 6.3 | `admin.css` | `admin-core.js` (`confirmAction`), `connection.js` | `base.html` | `tests/test_admin_ui.py` |
| Tabs | 6.4 | `admin.css` (`.energy-tabs`) | Bootstrap tab plugin | `energy.html` | `tests/e2e/mobile_layout.mjs` |
| Cards, KPI tiles | 6.5 | `admin.css` | none | page templates | `tests/e2e/admin_flow.mjs` |
| Data tables | 6.6 | `admin.css` (`.data-table-stack`, `.param-card`) | `admin-energy-setup.js`, `admin-parameters.js` | `_param_table.html`, `tokens.html` | `tests/test_admin_design_rules.py` (R11, R13) |
| Charts | 6.7 | feature tokens in `admin.css` | `admin-spot-market.js`, `admin-weather.js`, `admin-energy-setup.js`, `dashboard-*.js` | `energy.html`, `dashboard.html` | `tests/js/*`, `tests/e2e/plan_chart_cases.mjs` |
| Dashboard widgets | 9.1 | `dashboard.css` | `dashboard.js`, `dashboard-widgets.js` | `dashboard.html` | `tests/test_dashboard_widget_registry.py`, `tests/e2e/dashboard_resilience.mjs` |
| Energy flow graphic | 9.2 | `admin.css` (`.flow-*`) | `admin-energy-controls.js` | `energy.html` | `tests/test_admin_design_rules_badges.py` |

## 5 Shell, viewport, scrolling and navigation

### 5.1 Shell and viewport

The shell differs from a viewport-bound application: the document scrolls, the header is sticky and the footer is fixed.

- **DC-SHELL-1** Every page that extends `base.html` uses the shell: `<header class="admin-navbar">` (sticky, `z-index` 1030), the `.app-shell` flex row with `.sidebar` and `<main id="main-content">`, and the fixed `.admin-footer`. The login and password-change screens render `<main class="auth-shell">` without the shell chrome.
- **DC-SHELL-2** The document is the vertical scroller of page content. `body` is a flex column with `min-height: 100dvh`; `.app-shell` takes the remaining height.
- **DC-SHELL-3** The fixed footer MUST NOT cover content. `.app-shell` reserves `--rct-footer-reserve` (footer height plus the bottom safe-area inset) as bottom padding. Everything that positions against the footer (the toast region, the footer's own `min-height`) reads that one token.
- **DC-SHELL-4** The sticky header MUST NOT hide a focused control or an anchor target. `html` sets `scroll-padding-top` to at least the header height plus the `.admin-navbar::after` gap (6.5 rem today). The `::after` strip belongs to the header, adds no scroll height and repeats the sidebar column colours on desktop.
- **DC-SHELL-5** `#main-content` is `max-width: 105rem`, `min-width: 0` and carries the content zoom (`--content-zoom`). Content MUST NOT depend on a fixed pixel offset from a viewport edge.
- **DC-SHELL-6** Primary form actions stay reachable at the end of the content or in the shared action bar (`#export-actions`, DC-FORM-8). An action bar is never placed under the footer.
- **DC-VP-1** `viewport-fit=cover` is set. Therefore `body` pads `env(safe-area-inset-left|right)` and the footer and toast region add `env(safe-area-inset-bottom|right)`. A new fixed or sticky element MUST add the insets it touches. A use of `env(safe-area-inset-*)` without `viewport-fit=cover` resolves to 0 and is a defect (checked by `ios_viewport_audit.mjs`).
- **DC-VP-2** A height tied to the viewport MUST use `dvh` or `svh` (or a flex chain), never bare `vh`. The pinned TSDB console uses `svh` so browser chrome does not resize it; the reason is in its comment.
- **DC-VP-3** The Visual Viewport API MUST NOT be used unless a failure is reproduced on a device and recorded with device, OS and browser version (E-IOS). iOS behaviour is "unverified" until then (DC-MEAS-2).

### 5.2 Scroll ownership

- **DC-SCROLL-1** The document scrolls vertically; a card has no inner vertical scroll. A bounded region is allowed only where listed here, with an accessible name and a keyboard route (`tabindex="0"` when it has no focusable content).
- **DC-SCROLL-2** Never nest scroll regions (DC-SCROLL-3). `ios_viewport_audit.mjs` fails on one scroll container scrolling inside another.
- **DC-SCROLL-3** A bounded region sets `overscroll-behavior: contain` (horizontal: `overscroll-behavior-x`). Its height comes from the layout chain or a `max-height`, not a fixed `height`.

| Region | Axis | Where | Owner |
| --- | --- | --- | --- |
| TSDB log console `.tsdb-console` | vertical | TSDB page status and log. Fills the viewport down to the footer on wide screens; below 38 rem it gets a `max-height` so the document does not grow with every log line. `role="log"`, `tabindex`, visible focus ring (DC-EX-FOCUS-1). | `admin.css` |
| Parameter table `.param-table-wrap` | both | Inverters and Prometheus. The `param-card` fills the viewport down to the footer and the pager sits at the card bottom (R11). | `admin.css`, `_param_table.html` |
| Tab strip `.energy-tabs` | horizontal | Energy Manager. One swipeable row, no wrap, scrollbar hidden, snap points. | `admin.css` |
| Geocoding results list | vertical | Weather search, `max-height: 16rem`, overlays the form (DC-OVL-2). | `admin.css` |
| `.token-table-wrap` | horizontal | Token table above 575.98 px. | `admin.css` |

- **DC-SCROLL-4** While a dialog is open, Bootstrap locks `body`. A nested dialog that closes MUST restore the lock when another dialog is still open (`confirmAction` does this explicitly).
- **DC-SCROLL-5** A dynamic update (poll, async fragment) MUST NOT change the scroll position of the page or rebuild an unchanged region. The Overview keeps its last values on a failed or empty poll (9.1, `dashboard_resilience.mjs`).

### 5.3 Breakpoints

- **DC-BP-1** Viewport breakpoints in use, all in `admin.css` unless noted: 992 px (sidebar rail versus menu drawer), 768 px (`max-width: 767.98px`: touch targets, stacked dashboard grid in `dashboard.css`, mobile metric descriptions), 576 px (`max-width: 575.98px`: compact header, `.data-table-stack` rows), 420 px and 360 px (narrow-phone refinements), 1400 px (`max-width: 1399.98px`), and 38 rem (TSDB console mode). A new viewport breakpoint is a reusable layout decision: it is added to this list with its owner file and reason in the same change. A one-off need uses a container query.
- **DC-BP-2** Layouts that depend on a card's own width use container queries (the inverter overview at 41.5 rem and 56 rem, the narrow subcard at 19 rem, the Energy Manager panel at 52 rem). A container query MUST NOT compete with a viewport breakpoint for the same content.

### 5.4 Navigation

- **DC-NAV-1** The sidebar (`nav.sidebar`, "Main menu") and the drawer (`nav#mobile-nav`, "Mobile menu") render the same `nav` list. The active item carries `aria-current="page"`, a brand-red fill and white text (9.61:1) and the label; colour is not the only cue.
- **DC-NAV-2** The first focusable element is the skip link `.skip-link` to `#main-content` (WCAG 2.4.1).
- **DC-NAV-3** The drawer toggle `#nav-toggle` has `aria-controls="mobile-nav"` and a script-maintained `aria-expanded`. The drawer is `hidden` when closed and sticky below the header (`z-index` 1029).
- **DC-NAV-4** Header buttons share one height and glyph size. Below 768 px they are at least 44 px; below 420 px the brand text becomes `visually-hidden` so three 44 px buttons fit. An icon-only button has an `aria-label` (the Log out button keeps its label when the text hides).
- **DC-NAV-5** Page titles and actions sit in `.page-heading`: one `h1`, optional actions to the right, wrapping.

### 5.5 Focus policy

- **DC-FOCUS-1** One global rule draws the indicator: `outline: 3px solid var(--rct-focus-ring)`, `outline-offset: 2px`, `box-shadow: none`, on `:focus-visible` (`.btn`, `.btn-close`, `.form-control`, `.form-select`, `.form-check-input`, `.dropdown-item`, `.btn-check + .btn`). A component MUST NOT set its own focus colour or width. A pointer click on a button draws nothing.
- **DC-FOCUS-2** `--rct-focus-ring` is `#88171a` (light) and `#f2878a` (dark). It reaches at least 3:1 against the page background and the card in its theme (WCAG 1.4.11); measured values are in A.3.
- **DC-FOCUS-3** Inside the brand-red header the ring is white (`.admin-navbar :focus-visible`).
- **DC-FOCUS-4** `outline: none` MUST NOT remove a focus indicator without an equally perceivable replacement. The switch and button focus glow of Bootstrap is replaced by the outline above (`--bs-btn-focus-shadow-rgb` follows the brand).
- **DC-FOCUS-5** The outline indicator remains visible in `forced-colors: active`; no indicator relies on `box-shadow` or background alone.

| ID | Exception to | Scope | Rationale | Owner | Test |
| --- | --- | --- | --- | --- | --- |
| DC-EX-FOCUS-1 | DC-FOCUS-1 (colour, width) | `.tsdb-console:focus-visible` | The console is a fixed dark surface; a 2 px `#4da3ff` ring inside the border stays visible on it in both themes. | `admin.css` | none (open, 10.5) |
| DC-EX-FOCUS-2 | DC-FOCUS-1 (offset) | `.energy-status` | The row is clipped by its container; the ring is drawn inside (`outline-offset: -2px`). | `admin.css` | none (open, 10.5) |

## 6 Shared components and binding variants

### 6.1 State taxonomy, badges and dots

| Kind | Examples | Component | Never |
| --- | --- | --- | --- |
| S1 Connection or device state | Online, Offline, Not configured | `.status-badge` with a tone and the state word | Colour alone |
| S2 Energy flow state | Feeding in, Drawing, Charging, Idle | Flow badge: `.status-badge` toggled by `setBadge()`, plus the direction glyph | A problem tone for a normal state |
| S3 Indicator beside a label | The online dot beside an inverter name | `.status-dot` next to a visible text label | A dot without an adjacent word |
| S4 Count or metadata | Interval, count, version | Plain Bootstrap `.badge` or text | `.status-badge` |
| S5 Message severity | Warning, error, success text | Message markers (DC-MSG-1) | A dot or badge |

- **DC-STATE-1** A status badge is `.status-badge` plus exactly one tone: `.status-badge-success`, `-neutral`, `-warning` or `-danger`. A tone sets only `--status-badge-fg` and `--status-badge-bg`, from `--bs-*-text-emphasis` and `--bs-*-bg-subtle`. A tone rule MUST NOT set size, padding or typography; only `.status-badge` does. Check: `test_admin_design_rules_badges.py` (`rule_tones_use_tokens_only`, `rule_no_badge_specific_metrics`).
- **DC-STATE-2** A status badge keeps its leading dot (`.status-badge::before`) and its visible text. A badge MUST NOT hide its text (`font-size: 0`, `color: transparent`, `display: none`). The dot is the non-colour cue next to the word.
- **DC-STATE-3** Every tone meets 4.5:1 text contrast in light and dark. The test computes it from the Bootstrap token values. A new tone or a token change reruns the check.
- **DC-STATE-4** Flow badges are never dimmed or faded (`opacity`, an `is-stale` class, an opacity transition); a stale reading does not dim them. Their classes are written only inside `setBadge()` so a second writer cannot cause flicker. A normal flow state uses `success` or `neutral`, never `danger` or `warning`.
- **DC-STATE-5** A `.status-dot` never carries the meaning alone: the state word beside it does, in markup or `title`. In `forced-colors: active` the dot and badge dot use `CanvasText`, the badge gets a visible `currentColor` border, and the word carries the distinction.
- **DC-STATE-6** A new state kind, or a kind shown with another component, needs a row in the table above or an exception row (DC-EXC-1).
- **DC-STATE-7** A grid direction indicator (`.device-flow`) repeats its colour in a glyph, a tooltip and an `aria-label`.

### 6.2 Forms, buttons and messages

#### Buttons

- **DC-BTN-1** `.btn-primary` themes every state: background, border, hover, active, disabled and focus glow come from the brand tokens, so no Bootstrap blue shows. Check: `test_admin_design_rules.py` R7, `test_admin_ui.py::test_primary_button_themes_its_disabled_and_focus_state`.
- **DC-BTN-2** A view SHOULD have one primary action. Secondary and cancel actions use `btn-outline-secondary`; a destructive confirmation uses `btn-danger`.
- **DC-BTN-3** Energy Manager step buttons are plain `btn btn-primary energy-step-link`; no CSS rule targets `.energy-step-link`. Check: R10.
- **DC-BTN-4** An icon-only button (`.icon-button`, 2.3 rem square) has an `aria-label` and, where it is not obvious, a `title`. The same action uses the same Material icon everywhere (delete is `delete`).
- **DC-BTN-5** An action that is unavailable but should stay visible is `disabled` with the shared disabled look; it MUST NOT look actionable.

#### Fields

- **DC-FORM-1** Every field has a visible `<label for>` (`.form-label`, `.form-check-label` for toggles). A placeholder never replaces a label.
- **DC-FORM-2** Settings fields are built by the shared `buildFieldControl()` into `.setting-row` with `.form-text` help tied by `aria-describedby`. A page MUST NOT build its own field wrapper. Check: R9.
- **DC-FORM-3** A field uses the `type`, `inputmode` and `autocomplete` that match its data (`password` for secrets, `number` for numbers).
- **DC-FORM-4** A switch is a checkbox in `.form-switch`. Its off state keeps an outline and knob of at least 3:1 against page, card and track (`--rct-switch-off`, `--rct-switch-knob-off`), in both themes. The dark theme rule matches Bootstrap's higher-specificity selector on purpose (comment in `admin.css`).
- **DC-FORM-5** On a coarse pointer, text inputs are at least 16 px rendered so iOS does not zoom. Inside the content zoom the declared size is `16px / var(--content-zoom)`; modals live outside the zoom and declare 16 px. On phones (up to 767.98 px) buttons, inputs, `summary` and `.form-check` are at least 44 px high, scaled by the content zoom inside `#main-content`.
- **DC-FORM-6** A field-level problem is marked with `aria-invalid="true"` and tied to its message by `aria-describedby`; `aria-invalid` is removed when the field is edited (`admin-settings.js`). The authentication forms do the same for `AUTH_FIELD_IDS` and move focus to the offending field. A timed marker is not an error display.
- **DC-FORM-7** A control that is not editable yet explains why in help text, not a silent disabled field.
- **DC-FORM-8** An explicit-apply form has exactly one `btn btn-primary` "Apply changes" in the shared bar `buildApplyBar()` (`#export-actions`): no Discard, count badge or hint text. The Inverters dialog saves each add and delete at once and has no bar. Check: R5.
- **DC-FORM-9** The page never opens the browser's leave-page dialog (`beforeunload`). Check: R4.

#### Messages

- **DC-MSG-1** Message severity markers: an error starts with an `error` icon, a success with `check_circle`, a warning with the warning symbol, information has none. The text carries the meaning; the marker is `aria-hidden`. Implemented markers are partial (10.5).
- **DC-MSG-2** Toasts (`toast(message, kind)` in `admin-core.js`) are the result of an action. They render into `#toast-region` (`role="status"`, `aria-live="polite"`, `aria-atomic`), fixed bottom right above the footer (`z-index` 1100, `max-width: min(23rem, 100vw - 2rem - insets)`). Kinds: `success`, `info`, `warning`, `danger`. Auto-dismiss: 3.5 s, 3.5 s, 12 s, 30 s. Hover or keyboard focus pauses the timer. An identical message is suppressed while it shows. Every toast has a close button named "Dismiss notification".
- **DC-MSG-3** A toast enters and leaves through the right edge with a `transform` transition and no fade; under `prefers-reduced-motion` the transition is off.
- **DC-MSG-4** A failure that the operator must act on persists as a section notice (`.save-alert`), inserted as a sibling before its section anchor, never inside an `aria-live` container. An error uses `role="alert"`; a hint uses `role="status"` with `aria-live="off"`. A collapsed `<details>` that hosts the section opens when an error is placed in it.
- **DC-MSG-5** A request-level failure that names no field goes to the toast or the section notice; it never takes focus and the input is preserved. Autosaved settings report success by toast only; the status text stays for screen readers.
- **DC-MSG-6** No template, script, stylesheet or admin API text asks for a restart ("restart required", "restart to apply", `restart_required`). Check: R12.
- **DC-MSG-7** Help, error and result text reserves its space so that it causes no layout jump. A status line that can change length wraps between words; a number with its unit stays on one line.

### 6.3 Dialogs

- **DC-MODAL-1** All dialogs are Bootstrap `.modal` with `.modal-dialog.modal-dialog-centered` semantics: `.modal-dialog` is a flex box centred in the visible viewport, with `min-height: calc(100% - 2 * var(--bs-modal-margin))`; a scrollable dialog shrinks to fit a short viewport instead of pushing the page. No dialog needs its own centring.
- **DC-MODAL-2** Structure: `.modal-header` with `h2.modal-title.h5`, `.modal-body`, and for an actionable dialog `.modal-footer`. A dialog has a stable ID and `aria-labelledby` naming the title. Cancel is `btn-outline-secondary`, the primary action is `btn-primary`, a destructive one `btn-danger`.
- **DC-MODAL-3** A confirmation uses `confirmAction({ title, message, confirmLabel, danger })` (`#confirm-modal`). It resolves true only through the accept button, focuses Cancel when shown, allows one at a time, and restores the body lock if another dialog is still open. The native `confirm()` MUST NOT be used. Check: `test_admin_ui.py::test_destructive_actions_use_the_themed_confirm_modal_not_native_confirm`.
- **DC-MODAL-4** The confirmation dialog stacks above every other dialog (`z-index` 1075); see the scale in DC-STACK-1.
- **DC-MODAL-5** `#reconnect-modal` (connection lost) has a static backdrop and no keyboard dismissal. While `body.is-reconnecting`, the page behind is dimmed, blurred and inert, then reloads when the server answers. `#disclaimer-modal` does the same under `body.is-disclaimer-pending` (DC-PD-6). The blur is justified: it marks the page as unusable; it MUST NOT be used for another purpose.
- **DC-MODAL-6** A dialog MUST NOT use inline `style`, hard-coded colours, `bg-*` header classes or `card` markup in its body. Colours come from `--bs-*` and `--rct-*`; light and dark follow `data-bs-theme`.

### 6.4 Tabs

- **DC-TAB-1** Tabs are Bootstrap `ul.nav.nav-tabs` with `role="tablist"`, buttons `role="tab"` with `aria-controls`, and panes `role="tabpanel"` with `aria-labelledby` and `tabindex="0"` (`energy.html`). The Energy Manager tabs are the only tab set today.
- **DC-TAB-2** `.energy-tabs` is one row: `flex-wrap: nowrap`, horizontal scroll with hidden scrollbar, `overscroll-behavior-x: contain`, snap points, `white-space: nowrap` links. The clipped last tab signals more; there is no "More" control. Check: `tests/e2e/mobile_layout.mjs`.
- **DC-TAB-3** The idle tab colour in light mode uses `--bs-link-hover-color` to reach 4.5:1; hover, active and focus keep Bootstrap's values (`contrast_audit.mjs`).
- **DC-TAB-4** A tab that exists only for certain inverter modes (Charging plan) is added and removed as the mode requires. A selected tab that disappears MUST fall back to an existing tab; no pane stays empty.

### 6.5 Cards and tiles

- **DC-CARD-1** A card is `.card` with a 1 px `--rct-line` border, `0.8rem` radius and the card shadow level. Cards have an always-present frame and body; the header is plain (title, optional status and actions).
- **DC-CARD-2** Disclosures are native `<details>`/`<summary>` and are allowed only where this document names them: the advanced diagnostics and technical-names sections of the Energy Manager (`admin-energy-setup.js`, `energy.html`), the slot details of the charging plan, and the mobile metric description of a parameter row. A new disclosure is added to this list in the same change.
- **DC-CARD-3** Cards in the same grid row have equal height (stretch) without a fixed `height`; the body fills it. A card that takes a whole row uses `.card-span-all`. `.settings-grid` has at most four columns and no track narrower than a quarter of the row (R6).
- **DC-CARD-4** A KPI tile (`.card.kpi-card`) is an icon left and a label/value column right. The column has `min-width: 0`; words never break mid-word; value and unit stay on one line and the font scales down on a phone. Empty or missing content shows "n/a" and does not collapse the tile.
- **DC-CARD-5** Size a card from its content: minimum plus growth, wrapping by default. Truncation (`text-overflow: ellipsis`) is allowed only on the footer line, the status badge and the inverter name, and keeps the full value in the accessible name or `title`.

### 6.6 Data tables

- **DC-TABLE-1** A wide semantic table that must stay readable on a phone carries `.data-table-stack` and a `data-label` on every cell. Above 575.98 px it is an ordinary table; at or below, every row becomes a stacked block with the label beside the value. The shared rule and its `content: attr(data-label)` mechanism live once in `admin.css`. The token table and the charging-plan slot table use it; neither uses `overflow-wrap: anywhere` or `word-break: break-all` as its mobile layout. Check: R13.
- **DC-TABLE-2** A parameter table page (Prometheus, Inverters, any future `param_table`) renders the card through the macro `param_table()` in `_param_table.html`. The card fills the viewport down to the footer, the pager is pinned at the card bottom, and the page sets no height, `vh` or inline height of its own; only the shared `.param-card` and `.param-table-wrap` rules do. Inverters is the reference height. Check: R11.
- **DC-TABLE-3** A column that is hidden on a phone repeats its content where it still matters: the metric description of a parameter row moves into a native `<details class="metric-description-mobile">` in the name cell below 768 px.

### 6.7 Charts and data visualisation

- **DC-CHART-1** Charts are Chart.js on a `<canvas>` inside a relatively positioned box with a minimum height (`.dashboard-chart-box`, 10 rem). A chart has a text equivalent (a status line near it) and the canvas carries `role="img"` with an `aria-label`, as the dashboard chart canvases do.
- **DC-CHART-2** Series colours come from CSS custom properties read with `getComputedStyle` (DC-TOK-3): `--spot-market-today-line`, `--spot-market-tomorrow-line`, `--spot-market-yesterday-line`, `--rct-plan-power`, `--rct-plan-soc`, `--consumption-measured`, `--consumption-history`. Axis and grid colours read `--bs-body-color` and `--rct-line`.
- **DC-CHART-3** A chart redraws its colours on a theme change: `onThemeChanged` in `admin-core.js` (listening to `rct-theme-change`) for the Energy charts, and the listener or mutation observer inside `dashboard-battery-health.js` and `dashboard-charging-schedule.js`. A new chart registers the same way. Check: `tests/js/theme.test.js` covers the theme module only; the redraw has no dedicated check (open, 10.5).
- **DC-CHART-4** Colour MUST NOT be the only series cue (DC-PRIN-2). The day-ahead chart separates Today (solid) from Tomorrow (dashed). A new multi-series chart adds a dash pattern, marker shape or direct label. The legend uses `usePointStyle`.
- **DC-CHART-5** The charging-plan power and target-SoC series use the shared plan colours so every plan chart (Energy Manager, dashboard schedule, battery health) shows the same hue for the same quantity.
- **DC-CHART-6** A chart state line states loading, empty and error in text; an empty chart shows the empty state, not a blank canvas.

### 6.8 Overlays and stacking

- **DC-STACK-1** `z-index` comes from this scale; a new global layer takes a slot here in the same change:

| Layer | z-index | Owner |
| --- | --- | --- |
| Footer | 1020 | `.admin-footer` |
| Mobile drawer | 1029 | `.mobile-nav` |
| Header | 1030 | `.admin-navbar` |
| Dialog backdrop, dialog | 1050, 1055 (Bootstrap) | Bootstrap |
| Search results list | 1056 | Weather search |
| Confirmation dialog | 1075 | `#confirm-modal` |
| Toast region | 1100 | `.toast-region` |

  A local stacking value (1 or 2 inside a component) is allowed and stays inside its own stacking context.
- **DC-OVL-1** A centred dialog is sized against the visible viewport with `max-height` that keeps it fully visible; a fixed pixel size that can overflow is not allowed.
- **DC-OVL-2** An anchor-relative overlay (search results) is positioned from its anchor, overlays the controls below instead of shifting the form, and has a `max-height` with its own scroll.
- **DC-OVL-3** A toast is placed in the toast region and never centred like a dialog.

## 7 Responsive behaviour and accessibility

### 7.1 Responsive behaviour

- **DC-RESP-1** Every page reflows to a 320 px viewport without horizontal page scroll, in light and dark, and keeps all content and function. A horizontal scroller is allowed only where 5.2 lists it. Checks: `tests/e2e/live_resize.mjs`, `tests/e2e/mobile_layout.mjs`, `tests/e2e/ios_viewport_audit.mjs` (element wider than the viewport outside a scroller, nested scroll trap, text controls under 16 px, targets under 44 px).
- **DC-RESP-2** A live viewport resize (no reload) reflows the Overview and Energy pages and keeps the content structure (`live_resize.mjs`).
- **DC-RESP-3** Below 768 px the Overview stacks widgets in document order in a two-column grid; chart widgets and the inverter list span both columns. The stored layout is untouched and the layout controls are hidden (9.1).
- **DC-RESP-4** Touch targets: 44 px minimum on phones (DC-FORM-5). Desktop sizes stay unchanged.

### 7.2 Accessibility

- **DC-A11Y-1** Landmarks: `header`, `nav` (two, each with `aria-label`), one `main#main-content`, `footer`. The `html` element has `lang="en"`.
- **DC-A11Y-2** A heading hierarchy has one `h1` per page (`.page-heading`). Section titles are `h2.h5`; About keeps `h6` card headers until it is harmonised (R2).
- **DC-A11Y-3** An icon that carries meaning has an accessible name; a decorative icon is `aria-hidden="true"`. A link that opens a new tab says so (`visually-hidden` text and `rel="noopener noreferrer"`).
- **DC-A11Y-4** A live region announces what changed once: toasts (polite), the section notice (alert for errors only), the reconnect text (assertive). A field error does not carry `role="alert"` of its own.
- **DC-A11Y-5** Secondary text uses `--bs-secondary-color`, not Bootstrap's `#6c757d`, which fails 4.5:1 on the light page and 3:1 on dark cards. `admin.css` overrides `.text-secondary` once (DC-EXT-7).
- **DC-A11Y-6** Automated scanning (`tests/e2e/axe_scan.mjs`) must report no serious or critical violation on the login page, the Overview and the Energy tabs, in light and dark. Moderate and minor findings are reported, not blocking. Passing it is not WCAG conformance; full validation needs manual testing with assistive technology and expert review.

### 7.3 Themes, forced colours and reduced motion

- **DC-THEME-1** `theme.js` is the first script in `<head>`, before the stylesheets. It resolves the stored preference (`rct-admin-theme`: `auto`, `light`, `dark`; anything else is `auto`) and sets `data-bs-theme` and `data-theme-pref` on `<html>` before first paint. The page MUST NOT render one theme and correct it after mount.
- **DC-THEME-2** The theme switch cycles Auto, Light, Dark. Its accessible name states the current mode and the next one. `auto` follows `prefers-color-scheme` live, without a reload.
- **DC-THEME-3** The page declares `<meta name="color-scheme" content="light dark">`, and Bootstrap sets `color-scheme` per `data-bs-theme`, so native controls and scrollbars follow the active theme.
- **DC-THEME-4** A visual state exists in every supported theme. A component reads a semantic token; only the token definition reads the raw value. A new theme-dependent value is added to both the `:root` and the dark block.
- **DC-THEME-5** Under `forced-colors: active` status badges, dots, the SoC bar, flow icons and the log console use system colours (`CanvasText`, `Highlight`, `GrayText`, `Canvas`) and a visible border. A new component whose state is carried by background or fill adds a forced-colors rule or a border in the same change.
- **DC-THEME-6** Animations stop under `prefers-reduced-motion: reduce` (DC-TOK-6). An animation leaves the element in its resting state: toasts end at `translateX(0)`, the grid failsafe keyframe ends visible.

## 8 Interaction, state and content conventions

### 8.1 Interaction states

- **DC-INT-1** Hover is a background change on navigation items and a Bootstrap hover on buttons. Active navigation uses `aria-current`. Disabled controls use the shared look and `disabled`.
- **DC-INT-2** Cursor: `grab` and `grabbing` only on the dashboard drag handle in edit mode; a clickable element shows the pointer.
- **DC-INT-3** A busy control shows a spinner (`.spinner-border`) or `aria-busy` and prevents a duplicate submit. A loading region shows a state line, not a blank area.
- **DC-INT-4** Copy actions (token copy) report the result by toast and fall back to a dialog where the clipboard API is blocked (`test_token_copy_has_a_browser_regression_check_for_the_http_modal_fallback`).

### 8.2 Load stability and live updates

- **DC-LIVE-1** A poll that fails, returns nothing or is "connecting" MUST NOT rebuild or shrink the Overview. Values stay until they are older than `VALUE_MAX_AGE_MS` (20 s), then read "n/a"; the layout never changes. The last-known-good Overview is dimmed to 0.8 opacity under an inline banner (`.is-offline`, `.dashboard-offline-banner`). Check: `tests/e2e/dashboard_resilience.mjs`.
- **DC-LIVE-2** The Overview grid is hidden until the stored layout is applied, with a 4 s CSS failsafe so a failed script cannot leave it hidden.
- **DC-LIVE-3** Live data on the Energy Manager shows a freshness note only after a failed poll. A value update patches in place and keeps focus and scroll.
- **DC-LIVE-4** The connection monitor probes health and drives the reconnect dialog from every page (`connection.js`).

### 8.3 Content voice

- **DC-VOICE-1** Titles are sentence case: "Target database settings", not "Target Database Settings". Names (`TSDB`, `InfluxDB`, `QuestDB`, `Prometheus`, `Log`) are the only capitalised words. Check: R2.
- **DC-VOICE-2** Buttons name the action ("Apply changes", "Verify hardware"), never "OK". A confirmation names the consequence of the action.
- **DC-VOICE-3** The TSDB cards carry no decorative icons and no yellow banner; an incomplete form is reported by a toast and `aria-invalid` on the field (R3).
- **DC-VOICE-4** User-visible text, console output and log messages are English.

## 9 Feature-specific contracts and justified exceptions

### Exception index

| ID | Rule | Home |
| --- | --- | --- |
| DC-EX-TOK-1 | DC-TOK-2 | 4.1 |
| DC-EX-FOCUS-1 | DC-FOCUS-1 | 5.5 |
| DC-EX-FOCUS-2 | DC-FOCUS-1 | 5.5 |

### 9.1 Overview dashboard

- **DC-DASH-1** The Overview is a GridStack grid of 12 columns with `float: true`: a tile stays in the free cell where it is dropped. Cell height and margin are constants in `dashboard.js`; `dashboard.css` reads the GridStack margin variables and pulls the grid out by them so tile edges align with the heading.
- **DC-DASH-2** Widget identity comes from `app/admin/widget_ids.json`. The `dashboard.html` blocks, `DEFAULT_LAYOUT` in `dashboard.js` and `_DASHBOARD_WIDGETS` in `api.py` MUST agree; `tests/test_dashboard_widget_registry.py` is the guard. A new widget adds its ID to the JSON, the registry and the allowlist in one change.
- **DC-DASH-3** The layout persists through `PUT dashboard-layout` with `{ version: 1, widgets }` and loads through `GET dashboard-layout` (DC-PD-7). The server validates it; an unknown ID is rejected.
- **DC-DASH-4** Edit mode is entered with "Edit Widgets" and left with "Done" (`#dashboard-edit-done`). The buttons swap; neither is an `aria-pressed` toggle. In edit mode a tile gets a dashed outline, a drag strip and resize handles; outside edit mode there are none. The drag strip is an overlay (`position: absolute`, 1.1 rem) so toggling edit mode never shifts tile content. A resize handle keeps a 24 px target.
- **DC-DASH-5** Below 768 px editing is unavailable (buttons hidden, hint shown) and tiles stack (DC-RESP-3).
- **DC-DASH-6** The inverter overview tile grows with its content and never scrolls (`overflow: visible`). A chart widget has a minimum chart height and a state line (DC-CHART-6).
- **DC-DASH-7** A KPI tile and a widget card reuse the card and badge components. A widget MUST NOT define its own status colours.
- **DC-DASH-8** Dashboard CSS may use `!important` only to override GridStack's inline geometry (static stacked layout, handle display); each block names GridStack in its comment (DC-TOK-4).

### 9.2 Energy Manager and energy flow

- **DC-ENERGY-1** The page is a stack of cards in a tab set (6.4): Inverter, Charging plan, Spot Market, Weather. Charging plan exists only while an inverter is Manual or External.
- **DC-ENERGY-2** The flow graphic is an SVG shared by the Energy page and the Overview inverter card. Colour marks the flow: green for energy reaching the house or grid, whether generated, exported or drawn; inactive icons share one neutral grey. Direction is also carried by an arrowhead on every active line, never by colour alone.
- **DC-ENERGY-3** Dashed flow dots animate at one constant speed; under `prefers-reduced-motion` the dots stand still and the arrowhead keeps the direction readable.
- **DC-ENERGY-4** A mode option that is selected but blocked by an unfinished setup is outlined instead of solid and says so in text ("pending", "not active yet"). The setup checklist appears only while a step is unmet.
- **DC-ENERGY-5** Spot Market and Weather follow one pattern: one top-level settings card with compact groups, a status line, a context line and min/max highlights. A "tomorrow not published yet" note is a warning callout, not muted text.
- **DC-ENERGY-6** The charging-plan header carries the identity group (title and badge, sub-line), the inverter select and the Expert switch; it wraps without overflow. Plan sections are separated by a hairline and spacing, not by nested cards. A plan label comes from the plan's action table; an unknown action has no label and fails the test `test_every_planner_action_has_a_plan_label`.

### 9.3 TSDB, Prometheus, Inverters, API tokens, Settings, About

- **DC-TSDB-1** The TSDB page has no own layout or typography CSS: the only `.tsdb-*` or `.export-*` selectors are the log console's (R1). Use `settings-grid`, `card-body`, `h2.h5`, `setting-row` and `form-text`. Colours in the TSDB, `.settings-grid` and `.btn-primary` rules come from tokens (R8).
- **DC-TSDB-2** The TSDB console is `role="log"` with a visible focus ring (DC-EX-FOCUS-1), tails inside itself and fills the viewport down to the footer on wide screens (5.2). A log line MUST carry its level as text, not only as colour (DC-PRIN-2). Today `data-level` is a colour hook only; this is open (10.5).
- **DC-PARAM-1** Prometheus and Inverters share the parameter table (DC-TABLE-2). Row actions are icon buttons with names and reach 44 px on a phone (`test_parameter_action_buttons_have_touch_target_size`). Reordering keeps focus on a stable action hook.
- **DC-TOKEN-1** The API token table uses `.data-table-stack`. The token value is shown once; the stored suffix is a quiet code chip (`.token-suffix`). Copy follows DC-INT-4. Revoking uses the confirmation dialog (DC-MODAL-3).
- **DC-SETTINGS-1** Settings apply on their own and never mention a restart (DC-MSG-6). A section failure persists as a section notice (DC-MSG-4); success is a toast.
- **DC-ABOUT-1** About shows Application Details, Updates, Dependencies and the Changelog as cards. Machine values are inline code-like text. The page keeps its `h6` card headers until harmonised (R2 exception list). Its layout is owned by `about.css`.
- **DC-AUTH-1** Login and password change are standalone screens: no shell chrome, `auth-shell` container, shared type, theme and controls, card shadow from `--rct-auth-card-shadow`. A failed submit puts focus on the offending field and shows the message in a `role="alert"` region.

## 10 Regression gates, change procedure and references

### 10.1 Regression gates

| Gate | What it proves | Command |
| --- | --- | --- |
| Static design rules R1 to R13 | TSDB CSS scope, titles, icons and banners, no `beforeunload`, apply bar, grid columns, primary button states, tokens, shared fields, step buttons, parameter-card height, no restart wording, stacked tables | `python -m pytest -q tests/test_admin_design_rules.py` |
| Badge rules | Tone tokens, no badge-specific metrics, dot and text kept, flow badge rules, 4.5:1 per tone in both themes | `python -m pytest -q tests/test_admin_design_rules_badges.py` |
| Admin UI wiring | Accessibility wiring, themed confirmation, disclaimer, connection monitor, touch targets | `python -m pytest -q tests/test_admin_ui.py` |
| Widget registry | Dashboard widget IDs agree across JSON, JS and Python | `python -m pytest -q tests/test_dashboard_widget_registry.py` |
| Script unit tests | Theme, dashboard, energy controls, charts, settings | `node --test tests/js/*.test.js` |
| Browser E2E | Flows, layout, contrast, accessibility, iOS classes, resilience | `tests/e2e/*.mjs` (10.2) |

- **DC-GATE-1** The pull-request gate (`.github/workflows/gate.yml`) runs Ruff, pytest and the Node unit tests. A failed design test fails the run.
- **DC-GATE-2** Every static rule has a mutation test: a deliberately violating sample that the rule must reject, so a rule that silently stops matching fails. A new rule adds its sample in the same change.
- **DC-GATE-3** A change to a shared component or token runs the rule file that owns it and, for layout or theme changes, the matching E2E driver in light and dark.

### 10.2 Execution prerequisites

- **DC-EXEC-1** E2E drivers need Playwright. Set `PLAYWRIGHT_DIR` to a directory with `node_modules/playwright`, `PYTHON` to the project venv interpreter, and `COLOR_SCHEME=light|dark`. `BROWSER` is the engine name (`chromium`, `webkit`, `firefox`); leave it unset for Chromium. `axe_scan.mjs` additionally needs `@axe-core/playwright`. Example: `env -u BROWSER PLAYWRIGHT_DIR=<dir> PYTHON=.venv/bin/python node tests/e2e/admin_flow.mjs <tmpdir>`.
- **DC-EXEC-2** E2E output goes to a directory outside the repository. The drivers start an isolated database and a simulated inverter (`fake_inverter.py`).
- **DC-EXEC-3** The E2E drivers are not part of the PR gate. A claim that a layout rule holds names the driver, engine, viewport and theme that proved it.

### 10.3 Evidence levels

- **DC-EVID-1** E-ENGINE (headless Chromium, WebKit, Firefox) proves layout, computed styles and keyboard behaviour for that engine. It does not prove on-screen keyboard behaviour, browser chrome collapse or safe areas on iOS (DC-MEAS-2).
- **DC-EVID-2** A static test proves that a construct exists or is absent in the sources, not that it renders. Both kinds of evidence are named in a report.

### 10.4 Change procedure

- **DC-PROC-1** Before a UI change, find the component and its row in 4.2 and read its normative section. Change the owner, not a copy.
- **DC-PROC-2** The change updates this document and the owning check when it changes a shared rule, adds a component or variant, adds a breakpoint, a z-index layer, a disclosure, a scroll region or an exception. A new exception row needs its test (DC-EXC-1).
- **DC-PROC-3** A change that removes a selector verifies against templates and scripts, not only the stylesheet, that it is unused.
- **DC-PROC-4** Run the static rule files after the last edit; a result from before the last edit is stale. Run the relevant E2E driver for layout or theme changes.

### 10.5 References and open requirements

These deviate from the rules above or lack a check. Each is open until it has a change that closes it; none is an accepted exception.

- **Elevation tokens (DC-TOK-5).** Card, header, footer and toast shadows are literals in `admin.css`. Promote them to three tokens with light and dark values.
- **`.status-dot` colours (DC-TOK-2, DC-STATE-5).** `.status-dot`, `.online` and `.offline` use literal hex colours and no shape cue; the adjacent word carries the state. Move them to the semantic tokens and add a shape repeat (as justUp! does) or fold them into `.status-badge`.
- **Navbar focus colour (DC-FOCUS-3).** `#fff` is a literal; give it a token.
- **Message markers (DC-MSG-1).** Error, success and warning markers are not yet one shared component; markup differs per surface. Add one component and a check that fails on a missing marker.
- **`DC-EX-FOCUS-1` and `DC-EX-FOCUS-2` have no test.** An exception row needs its test (DC-EXC-1); `tests/e2e/tsdb_console_ui.mjs` does not assert the ring. Add the checks or remove the exceptions.
- **TSDB log level (DC-TSDB-2).** The level is a colour hook only. Render the level as text in each line.
- **Chart theme redraw (DC-CHART-3).** No check proves that every chart recolours on a theme change.
- **No external request check (DC-PD-2).** Nothing fails when a template or stylesheet references another origin. Add a static scan of templates and CSS.
- **Reduced-motion coverage.** The global reset covers CSS animation and transition. Script-driven animation and the `body.is-*` blur transitions have no dedicated check.
- **Forced-colors coverage.** Rules exist for badges, dots, SoC bar, flow icons and the console. There is no browser check that runs with `forced-colors: active`.
- **Text enlargement.** No check scales the root font size to 200 % at 1280x720 and 390x844 and asserts that no content or function is lost.
- **iOS evidence (DC-MEAS-2).** All iOS statements are "unverified" until an E-IOS run is recorded.
- **Dark-theme contrast sweep.** `contrast_audit.mjs` covers the Energy tab links and the Expert switch. Other components rely on the token pairs and the badge test.

### 10.6 Normative references

- WCAG 2.2: [1.4.1 Use of Color](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color), [1.4.3 Contrast (Minimum)](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum), [1.4.11 Non-text Contrast](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html), [2.4.7 Focus Visible](https://www.w3.org/WAI/WCAG22/Understanding/focus-visible), [2.4.11 Focus Not Obscured (Minimum)](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum).
- MDN: [`prefers-reduced-motion`](https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-motion), [`forced-colors`](https://developer.mozilla.org/en-US/docs/Web/CSS/@media/forced-colors), [`color-scheme`](https://developer.mozilla.org/en-US/docs/Web/CSS/color-scheme), [`:focus-visible`](https://developer.mozilla.org/en-US/docs/Web/CSS/:focus-visible).

## Appendix A Rationale and change log

### A.1 Rationale

- Shell: the footer is fixed and the document scrolls, unlike a viewport-bound shell. The reserve token `--rct-footer-reserve` exists because the footer height plus the bottom safe-area inset must match everywhere something positions against it; a hard-coded header height was once a few pixels short and made short pages scroll.
- Content zoom: `#main-content` is rendered at 0.9 for density. Header, sidebar and footer stay at 100 %. Touch-target and input-size rules compensate by dividing by `--content-zoom`.
- Secondary text and off-state switches: Bootstrap's defaults (`#6c757d`, the off-state switch outline and knob) fall below 4.5:1 or 3:1 on one of the themes. `admin.css` overrides them once, with the measured reason in a comment.
- Status badge: one badge with a leading dot replaced several per-page variants; tones only change colours so sizes cannot drift. Flow badges are never dimmed because opacity breaks their contrast.
- Toast: it slides through the right edge without a fade so no toast looks half-visible; it sits above the footer so header buttons stay uncovered.
- Blur on the reconnect and disclaimer backdrops: the whole page is unusable behind them, and the effect says so. It is limited to those two states.
- Responsive tables: squeezing wide tables with character-level wrapping made tokens and slot lists unreadable; `.data-table-stack` shows each row as labelled blocks instead, from one shared rule.

### A.2 Change log

- 2026-11-09: initial design contract, derived from the current code and the existing design tests; structure follows the justUp! design contract; open requirements listed in 10.5.

### A.3 Measurement records

Informative snapshots behind requirements. The requirement is the rule, not the number.

- Focus ring contrast (DC-FOCUS-2), computed from the token values on 2026-11-09: light ring `#88171a` against the page `#f8f9fa` 9.12:1 and against the card `#ffffff` 9.61:1; dark ring `#f2878a` against the body `#212529` 6.32:1 and against the tertiary background `#2b3035` 5.46:1; white ring on the brand header 9.61:1.
- Active navigation (DC-NAV-1): white on `#88171a` 9.61:1; white on the hover brand `#6f1215` 11.84:1.
- Dark `.text-primary` (DC-PD-1): `#f1a0a3` on `#212529` 7.56:1.
