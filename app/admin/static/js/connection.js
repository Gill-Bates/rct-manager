//
// app/admin/static/js/connection.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Server connection monitor: a heartbeat probes /health, and after repeated failures a blocking
// "Connection lost" modal is shown while a backed-off probe waits for the server to return.
(() => {
  'use strict';

  const RECONNECT_PROBE_TIMEOUT_MS = 10000;
  const DEBUG = window.RCT_DEBUG === true;

  let reconnectModal = null;
  let activeController = null;
  let loginRedirected = false;
  let pageHidden = document.visibilityState === 'hidden';

  const reconnectState = {
    active: false,
    timer: null,
    inFlight: false,
    delayMs: 2000,
    // /health is token-free and answers 200 until the shutdown begins, so it needs no session.
    pingUrl: '/health',
    failCount: 0,
    failThreshold: 3,
    attempts: 0,
    lastCheck: null,
  };

  const heartbeatState = {
    timer: null,
    delayMs: 15000,
    timeoutMs: 4000,
    inFlight: false,
  };

  // Consumers of rct:reconnect:start/stop must unregister their own listeners on teardown.
  window.RCTReconnect = Object.freeze({
    isActive: () => reconnectState.active,
    start: () => startReconnectMode(true),
    stop: stopReconnectMode,
    destroy: destroyReconnect,
  });

  function getReconnectModal() {
    if (reconnectModal) return reconnectModal;
    const element = document.getElementById('reconnect-modal');
    if (!element || !window.bootstrap?.Modal) return null;
    reconnectModal = new window.bootstrap.Modal(element);
    // Bootstrap ignores hide() while the show transition runs, so a very fast recovery would
    // leave the modal stuck open; close it as soon as the transition has finished.
    element.addEventListener('shown.bs.modal', () => {
      if (!reconnectState.active) safeHideModal(reconnectModal);
    });
    return reconnectModal;
  }

  function redirectToLoginIfNeeded() {
    if (loginRedirected) return;
    if (!window.location.pathname.startsWith('/login')) {
      loginRedirected = true;
      window.location.replace('/login');
    }
  }

  function safeHideModal(modal) {
    try {
      document.activeElement?.blur();
      modal?.hide();
    } catch (error) {
      debugError('modal hide failed', error);
    }
  }

  function abortActiveProbe() {
    if (activeController) {
      activeController.abort();
      activeController = null;
    }
  }

  async function fetchPing({ timeoutMs = 0 } = {}) {
    abortActiveProbe();
    const controller = new AbortController();
    activeController = controller;
    const timeoutId = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      return await fetch(reconnectState.pingUrl, {
        method: 'GET',
        headers: { 'Cache-Control': 'no-cache' },
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      });
    } finally {
      if (timeoutId) clearTimeout(timeoutId);
      if (activeController === controller) activeController = null;
    }
  }

  function handlePingResponse(response) {
    if (response.status === 401) {
      redirectToLoginIfNeeded();
      return 'handled';
    }
    if (response.ok) {
      stopReconnectMode();
      return 'handled';
    }
    return 'error';
  }

  function debugError(context, error) {
    if (error instanceof DOMException && error.name === 'AbortError') return;
    if (DEBUG) console.debug(`[reconnect] ${context}:`, error);
  }

  function clearReconnectTimer() {
    if (reconnectState.timer) {
      clearTimeout(reconnectState.timer);
      reconnectState.timer = null;
    }
  }

  function clearHeartbeatTimer() {
    if (heartbeatState.timer) {
      clearTimeout(heartbeatState.timer);
      heartbeatState.timer = null;
    }
  }

  function scheduleHeartbeat(delay = heartbeatState.delayMs) {
    clearHeartbeatTimer();
    if (reconnectState.active || pageHidden || document.visibilityState === 'hidden') return;
    heartbeatState.timer = setTimeout(heartbeatCheck, delay);
  }

  function scheduleCompensatedHeartbeat(startedAt) {
    const elapsed = performance.now() - startedAt;
    scheduleHeartbeat(Math.max(1000, heartbeatState.delayMs - elapsed));
  }

  function updateReconnectHint() {
    const hint = document.getElementById('reconnect-hint');
    if (!hint) return;
    hint.textContent = reconnectState.lastCheck
      ? `Attempt ${reconnectState.attempts}, last check ${reconnectState.lastCheck.toLocaleTimeString('en-GB')}.`
      : 'Waiting for the first attempt …';
  }

  function stopReconnectMode() {
    clearReconnectTimer();
    reconnectState.active = false;
    reconnectState.inFlight = false;
    reconnectState.delayMs = 2000;
    reconnectState.failCount = 0;
    document.body?.classList.remove('is-reconnecting');
    if (reconnectModal) safeHideModal(reconnectModal);
    window.dispatchEvent(new CustomEvent('rct:reconnect:stop'));
    scheduleHeartbeat();
  }

  async function probeReconnect() {
    if (!reconnectState.active) return;
    if (reconnectState.inFlight) return;
    if (navigator.onLine === false) {
      clearReconnectTimer();
      reconnectState.delayMs = Math.min(Math.round(reconnectState.delayMs * 1.5), 30000);
      reconnectState.timer = setTimeout(probeReconnect, reconnectState.delayMs);
      return;
    }

    clearReconnectTimer();
    reconnectState.inFlight = true;
    try {
      const response = await fetchPing({ timeoutMs: RECONNECT_PROBE_TIMEOUT_MS });
      if (handlePingResponse(response) === 'handled') return;
    } catch (error) {
      // Keep polling while the server is down.
      debugError('probe error', error);
    } finally {
      reconnectState.inFlight = false;
      reconnectState.attempts++;
      reconnectState.lastCheck = new Date();
      updateReconnectHint();
    }

    if (reconnectState.active && !pageHidden) {
      const jitter = 0.8 + (Math.random() * 0.4);
      reconnectState.delayMs = Math.min(Math.round(reconnectState.delayMs * 1.5 * jitter), 30000);
      reconnectState.timer = setTimeout(probeReconnect, reconnectState.delayMs);
    }
  }

  async function heartbeatCheck() {
    const startedAt = performance.now();

    if (reconnectState.active || pageHidden || document.visibilityState === 'hidden' || heartbeatState.inFlight) {
      scheduleHeartbeat();
      return;
    }

    if (navigator.onLine === false) {
      startReconnectMode(true);
      return;
    }

    heartbeatState.inFlight = true;
    try {
      const response = await fetchPing({ timeoutMs: heartbeatState.timeoutMs });
      if (handlePingResponse(response) === 'handled') {
        if (response.ok) {
          reconnectState.failCount = 0;
          scheduleCompensatedHeartbeat(startedAt);
        }
        return;
      }
    } catch (error) {
      if (!startReconnectMode()) scheduleCompensatedHeartbeat(startedAt);
      debugError('heartbeat error', error);
      return;
    } finally {
      heartbeatState.inFlight = false;
    }

    if (!startReconnectMode()) scheduleCompensatedHeartbeat(startedAt);
  }

  function startReconnectMode(force = false) {
    if (reconnectState.active) return true;
    if (!force) reconnectState.failCount++;
    if (!force && reconnectState.failCount < reconnectState.failThreshold) return false;

    enterReconnectMode();
    void probeReconnect();
    return true;
  }

  function enterReconnectMode() {
    reconnectState.active = true;
    reconnectState.inFlight = false;
    reconnectState.attempts = 0;
    reconnectState.lastCheck = null;
    updateReconnectHint();
    clearHeartbeatTimer();
    document.body?.classList.add('is-reconnecting');

    // Stale toasts would describe a connection that is gone; the modal is the one message now.
    document.getElementById('toast-region')?.replaceChildren();

    getReconnectModal()?.show();
    window.dispatchEvent(new CustomEvent('rct:reconnect:start'));
  }

  function onVisibilityChange() {
    pageHidden = document.visibilityState === 'hidden';
    if (reconnectState.active) {
      // A probe that ended while hidden schedules no retry; resume it on return (an in-flight probe
      // reschedules itself, and probeReconnect() ignores a second concurrent call).
      if (!pageHidden) {
        clearReconnectTimer();
        void probeReconnect();
      }
      return;
    }
    if (document.visibilityState === 'visible') scheduleHeartbeat(1000);
    else clearHeartbeatTimer();
  }

  function onOnline() {
    if (reconnectState.active) {
      clearReconnectTimer();
      void probeReconnect();
      return;
    }
    scheduleHeartbeat(500);
  }

  function onOffline() {
    if (reconnectState.active) return;
    startReconnectMode(true);
  }

  function onPageShow(event) {
    pageHidden = false;
    if (!event?.persisted) return;
    if (reconnectState.active) {
      void probeReconnect();
      return;
    }
    scheduleHeartbeat(1000);
  }

  function onPageHide() {
    pageHidden = true;
    abortActiveProbe();
    clearHeartbeatTimer();
    clearReconnectTimer();
    heartbeatState.inFlight = false;
    reconnectState.inFlight = false;
  }

  function destroyReconnect() {
    abortActiveProbe();
    clearHeartbeatTimer();
    clearReconnectTimer();
    reconnectState.active = false;
    reconnectState.inFlight = false;
    reconnectState.delayMs = 2000;
    reconnectState.failCount = 0;
    heartbeatState.inFlight = false;
    pageHidden = false;
    document.body?.classList.remove('is-reconnecting');
    if (reconnectModal) {
      safeHideModal(reconnectModal);
      reconnectModal.dispose();
      reconnectModal = null;
    }
    document.removeEventListener('visibilitychange', onVisibilityChange);
    window.removeEventListener('online', onOnline);
    window.removeEventListener('offline', onOffline);
    window.removeEventListener('pageshow', onPageShow);
    window.removeEventListener('pagehide', onPageHide);
  }

  document.getElementById('reconnect-retry')?.addEventListener('click', () => {
    if (!reconnectState.active) return;
    clearReconnectTimer();
    void probeReconnect();
  });
  document.getElementById('reconnect-reload')?.addEventListener('click', () => window.location.reload());

  document.addEventListener('visibilitychange', onVisibilityChange);
  window.addEventListener('online', onOnline);
  window.addEventListener('offline', onOffline);
  window.addEventListener('pageshow', onPageShow);
  window.addEventListener('pagehide', onPageHide);

  scheduleHeartbeat();
})();
