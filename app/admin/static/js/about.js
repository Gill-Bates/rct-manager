//
// app/admin/static/js/about.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const button = $('btn-check-updates');
  if (!button) return;

  function versionParts(value) {
    const match = String(value || '').match(/^v?(\d+(?:\.\d+)*)/i);
    return match ? match[1].split('.').map(Number) : [0];
  }

  function currentIsNewer(current, latest) {
    const a = versionParts(current);
    const b = versionParts(latest);
    for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
      if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) > (b[i] || 0);
    }
    return false;
  }

  function releaseUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol === 'https:' && url.hostname === 'github.com' &&
          url.pathname.startsWith('/Gill-Bates/rct-rest-api/releases/tag/')) return url.href;
    } catch { /* Missing or invalid release URL. */ }
    return null;
  }

  function showStatus(kind, icon, message) {
    const badge = $('update-status-badge');
    const alert = document.createElement('div');
    alert.className = `alert alert-${kind} py-1 mb-0 small`;
    const symbol = document.createElement('span');
    symbol.className = 'material-icons align-middle me-1';
    symbol.textContent = icon;
    alert.append(symbol, document.createTextNode(message));
    badge.replaceChildren(alert);
  }

  async function checkForUpdates(force = false) {
    button.disabled = true;
    $('update-check-loading').classList.remove('d-none');
    $('update-check-result').classList.add('d-none');
    $('update-available-section').classList.add('d-none');
    $('update-error').classList.add('d-none');
    $('update-release-link').removeAttribute('href');

    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 15000);
      let response;
      try {
        response = await fetch(`/admin/api/check-updates${force ? '?force=true' : ''}`, {
          credentials: 'same-origin', signal: controller.signal,
        });
      } finally {
        clearTimeout(timeout);
      }
      if (!response.ok) throw new Error(`Update check failed (${response.status}).`);
      const data = await response.json();
      $('update-current-version').textContent = data.current_version || '–';
      $('update-latest-version').textContent = data.latest_version || '–';

      const published = data.published_at ? new Date(data.published_at) : null;
      $('update-published-row').classList.toggle('d-none', !published || Number.isNaN(published.getTime()));
      if (published && !Number.isNaN(published.getTime())) {
        $('update-published-at').textContent = published.toLocaleDateString(undefined, {
          year: 'numeric', month: 'short', day: 'numeric',
        });
      }

      if (data.error) {
        showStatus('warning', 'warning_amber', 'Unable to check for updates.');
        $('update-error').textContent = data.error;
        $('update-error').classList.remove('d-none');
      } else if (currentIsNewer(data.current_version, data.latest_version)) {
        showStatus('warning', 'warning_amber', 'You are currently using a pre-release. This is not recommended for production!');
      } else if (data.update_available) {
        showStatus('success', 'new_releases', `Update available! Version ${data.latest_version} is ready.`);
        const url = releaseUrl(data.release_url);
        if (url) {
          $('update-release-link').href = url;
          $('update-available-section').classList.remove('d-none');
        }
      } else {
        showStatus('secondary', 'check_circle', "You're running the latest version.");
      }
    } catch (error) {
      showStatus('warning', 'warning_amber', 'Unable to check for updates.');
      $('update-error').textContent = error.name === 'AbortError' ? 'Connection timeout' : error.message;
      $('update-error').classList.remove('d-none');
    } finally {
      $('update-check-loading').classList.add('d-none');
      $('update-check-result').classList.remove('d-none');
      button.disabled = false;
    }
  }

  button.addEventListener('click', () => checkForUpdates(true));
  checkForUpdates();
})();
