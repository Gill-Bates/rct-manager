//
// app/admin/static/js/admin.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

  function startDashboardLayout() {
    if (window.RCTDashboard) return window.RCTDashboard.initLayout();
    return new Promise((resolve, reject) => {
      document.addEventListener('rct:dashboard-ready', () => {
        window.RCTDashboard.initLayout().then(resolve, reject);
      }, { once: true });
    });
  }

  // Lets dashboard.js hold the grid back until the first tile contents exist, so nothing resizes
  // after the first paint. Set as a flag too: the layout may finish before or after this runs.
  function markDashboardDataReady() {
    document.body.dataset.dashboardData = 'ready';
    document.dispatchEvent(new CustomEvent('rct:dashboard-data-ready'));
  }

  async function bootstrapAdminPage() {
    if (['login', 'change-password'].includes(page)) {
      // The handler is already registered; the session only fetches the CSRF token up front.
      try { await ensureSession(); }
      catch { setAuthInitError('The login form could not be prepared. Please reload the page.'); }
      return;
    }
    try {
      await session();
      if (page === 'dashboard') {
        initDashboardPolling();
        // dashboard.js owns the GridStack layout; it is independent of the polling above and of
        // data loading, so it is started without awaiting it.
        startDashboardLayout().catch((error) => toast(messageFrom(error), 'danger'));
        // The settings read (GET /admin/api/settings) is sensitive and only feeds the inverters
        // modal, which may never open. Load it lazily on first open instead of on every dashboard
        // load (SEC-06).
        initInvertersModalLazy();
        await Promise.all([loadDashboard().finally(markDashboardDataReady), loadMetricCount()]);
      }
      else if (page === 'tokens') initTokens();
      else if (page === 'energy') initEnergy();
      else if (['settings', 'inverters', 'prometheus', 'tsdb'].includes(page)) {
        await initSettings();
        if (page === 'inverters' || page === 'prometheus') await initParameters();
      }
    } catch (error) { toast(messageFrom(error), 'danger'); }
  }

  // dashboard.js loads after this file and needs the shared request/toast helpers; admin.js stays
  // the single owner of the CSRF token and the fetch wrapper (abort/401/toast handling included).
  window.RCTAdmin = Object.freeze({ api, toast, messageFrom, element, confirmAction });

  function start() {
    initShell();
    // Registered during page parse, before any await: without JS the form falls back to its own
    // method="post", and with JS the handler is there from the first keystroke on.
    if (page === 'login') initAuthForm('login-form', submitLogin);
    else if (page === 'change-password') initAuthForm('change-password-form', submitPasswordChange);
    bootstrapAdminPage().catch((error) => toast(messageFrom(error), 'danger')); // last-resort net
  }

  start();
