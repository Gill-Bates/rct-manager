//
// app/api/docs_static/docs-init.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Swagger UI bootstrap, served self-hosted so the /docs page keeps its default-src 'self' CSP
// without an inline <script>: FastAPI's get_swagger_ui_html() emits this initialization inline,
// which a CSP-enforcing browser blocks. The configuration mirrors the swagger_ui_parameters the
// page used before; `ui` is published as a global because docs-nav.js builds its sidebar from it.
window.ui = SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  layout: "BaseLayout",
  deepLinking: true,
  showExtensions: true,
  showCommonExtensions: true,
  tryItOutEnabled: true,
  docExpansion: "list",
  filter: true,
  tagsSorter: "alpha",
  operationsSorter: "alpha",
  defaultModelsExpandDepth: -1,
  displayRequestDuration: true,
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
});
