//
// app/admin/static/js/dashboard-widgets.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Dashboard widget registry: the single source of truth for widget identity, replacing the
// three hand-synced hardcode sites (the dashboard.html data-widget-id blocks, DEFAULT_LAYOUT in
// dashboard.js, and _DASHBOARD_WIDGETS in app/admin/api.py). dashboard.js derives DEFAULT_LAYOUT
// from this array (same seven-key {id,x,y,w,h,visible,label} shape as before), so the migration
// is a no-op by construction. The values below are copied verbatim from today's DEFAULT_LAYOUT.
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
    Object.freeze({ id: 'spot-price-curve', label: 'Day-ahead prices', kind: 'chart', defaultSize: Object.freeze({ w: 6, h: 4 }), defaultVisible: false, defaultX: 0, defaultY: 12 }),
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
