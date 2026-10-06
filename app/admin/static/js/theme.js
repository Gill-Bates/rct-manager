//
// app/admin/static/js/theme.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

(function () {
  const saved = localStorage.getItem('rct-admin-theme');
  const theme = saved === 'dark' || saved === 'light' ? saved : (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.documentElement.setAttribute('data-bs-theme', theme);
})();
