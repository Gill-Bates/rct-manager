//
// app/admin/static/js/dashboard-widgets.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Dashboard widget registry: the single JS source for widget identity, layout and order.
// dashboard.js derives DEFAULT_LAYOUT from this array (same seven-key {id,x,y,w,h,visible,label}
// shape as before), so the migration is a no-op by construction, and dashboard.html's
// data-widget-id blocks follow it. The values below are copied verbatim from today's DEFAULT_LAYOUT.
//
// This is NOT the only place a widget id lives: the Python allowlist _DASHBOARD_WIDGETS in
// app/admin/api.py stays the backend authority (a hand-written frozenset, no import-time IO), and
// app/admin/widget_ids.json is a shared id manifest. The three id sets are kept from drifting by
// tests/test_dashboard_widget_registry.py, not by runtime derivation, so a new widget id must still
// be added in each place; the anti-drift test fails if they diverge.
//
// The original ten entries retain their identity, size and factory placement. Spot widgets
// extend the catalog and remain opt-in for existing dashboards.

/**
 * @typedef {Object} WidgetDef
 * @property {string}  id            Stable widget id (kebab-case). Matches data-widget-id in the DOM
 *                                   and the server allowlist. NEVER renamed (persisted in layouts).
 * @property {string}  label         Human label (Add-Widget list + widget header).
 * @property {'kpi'|'flow'|'chart'}  kind   Widget kind.
 * @property {{w:number, h:number}}  defaultSize   Default grid cells (w 1..12, h 1..100; x+w<=12).
 * @property {boolean} defaultVisible Whether it is in the factory default layout.
 * @property {number} [defaultX]     Factory x. REQUIRED for every one of today's ten widgets
 *                                   (visible AND hidden). Optional-until-placed applies ONLY to a
 *                                   future, not-yet-built catalog entry, which may omit it until built.
 * @property {number} [defaultY]     Factory y. Same rule as defaultX.
 */

(function () {
  'use strict';

  // The registry array order is the single JS ordering contract: it equals today's DEFAULT_LAYOUT
  // id order and the "Add widget" list order. Values are verbatim from dashboard.js's old literal.
  const registry = Object.freeze([
    Object.freeze({ id: 'pv-power', label: 'PV power', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: true, defaultX: 0, defaultY: 0 }),
    Object.freeze({ id: 'grid-power', label: 'Grid power', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: true, defaultX: 2, defaultY: 0 }),
    Object.freeze({ id: 'house-power', label: 'House consumption', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: true, defaultX: 4, defaultY: 0 }),
    Object.freeze({ id: 'battery-soc', label: 'Battery level', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: true, defaultX: 6, defaultY: 0 }),
    Object.freeze({ id: 'tsdb-status', label: 'TSDB export', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: true, defaultX: 8, defaultY: 0 }),
    Object.freeze({ id: 'metric-count', label: 'Exposed metrics', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: true, defaultX: 10, defaultY: 0 }),
    Object.freeze({ id: 'devices', label: 'Inverter overview', kind: 'flow', defaultSize: Object.freeze({ w: 12, h: 8 }), defaultVisible: true, defaultX: 0, defaultY: 2 }),
    Object.freeze({ id: 'device-count', label: 'Inverters', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: false, defaultX: 0, defaultY: 0 }),
    Object.freeze({ id: 'connected-count', label: 'Connected', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: false, defaultX: 2, defaultY: 0 }),
    Object.freeze({ id: 'days-to-calibration', label: 'Days to calibration', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: false, defaultX: 4, defaultY: 0 }),
    Object.freeze({ id: 'spot-price-current', label: 'Spot price', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: false, defaultX: 0, defaultY: 10 }),
    Object.freeze({ id: 'spot-market-status', label: 'Day-ahead', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: false, defaultX: 2, defaultY: 10 }),
    Object.freeze({ id: 'spot-price-curve', label: 'Day-ahead prices', kind: 'chart', defaultSize: Object.freeze({ w: 6, h: 4 }), defaultVisible: false, defaultX: 0, defaultY: 13 }),
    Object.freeze({ id: 'solar-forecast-status', label: 'Solar forecast', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: false, defaultX: 4, defaultY: 10 }),
    Object.freeze({ id: 'pv-forecast-curve', label: 'PV forecast', kind: 'chart', defaultSize: Object.freeze({ w: 6, h: 4 }), defaultVisible: false, defaultX: 6, defaultY: 13 }),
    Object.freeze({ id: 'charging-schedule', label: 'Charging schedule', kind: 'chart', defaultSize: Object.freeze({ w: 6, h: 4 }), defaultVisible: false, defaultX: 0, defaultY: 16 }),
    Object.freeze({ id: 'battery-usable-kwh', label: 'Battery usable capacity', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: false, defaultX: 0, defaultY: 10 }),
    Object.freeze({ id: 'battery-remaining-percent', label: 'Battery remaining capacity', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: false, defaultX: 2, defaultY: 10 }),
    Object.freeze({ id: 'battery-cycles', label: 'Battery cycles', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: false, defaultX: 4, defaultY: 10 }),
    Object.freeze({ id: 'battery-last-calibration', label: 'Last battery calibration', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: false, defaultX: 6, defaultY: 10 }),
    Object.freeze({ id: 'battery-commissioning-date', label: 'Commissioning date', kind: 'kpi', defaultSize: Object.freeze({ w: 2, h: 2 }), defaultVisible: false, defaultX: 8, defaultY: 10 }),
    Object.freeze({ id: 'battery-degradation', label: 'Battery degradation', kind: 'chart', defaultSize: Object.freeze({ w: 6, h: 4 }), defaultVisible: false, defaultX: 0, defaultY: 13 }),
  ]);

  // Browser path: expose the frozen registry as a global, consumed by dashboard.js (loaded after
  // this script via a plain <script defer>). Node path (tests): also export via module.exports so
  // `node --test` can require() the registry with no DOM and no bundler.
  if (typeof window !== 'undefined') {
    window.RCTDashboardRegistry = Object.freeze({ registry });
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { registry };
  }
})();
