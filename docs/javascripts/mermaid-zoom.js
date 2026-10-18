/* Click-to-enlarge lightbox for Mermaid diagrams (Material for MkDocs). */
(function () {
    "use strict";

    // Material renders into a closed shadow root; record roots so they stay reachable.
    // Must run before the first diagram renders, hence the early extra_javascript entry.
    var roots = new WeakMap();
    var attach = Element.prototype.attachShadow;
    Element.prototype.attachShadow = function (init) {
        var root = attach.call(this, init);
        if (this.classList.contains("mermaid")) {
            roots.set(this, root);
        }
        return root;
    };

    var overlay = null;
    var opener = null;

    function close() {
        if (!overlay) {
            return;
        }
        overlay.remove();
        overlay = null;
        document.removeEventListener("keydown", onKey, true);
        document.documentElement.classList.remove("rct-zoom-open");
        if (opener) {
            opener.focus();
            opener = null;
        }
    }

    function onKey(event) {
        if (event.key === "Escape") {
            event.preventDefault();
            close();
        } else if (event.key === "Tab" && overlay) {
            // Only the close button is focusable; keep focus inside the dialog.
            event.preventDefault();
            overlay.querySelector("button").focus();
        }
    }

    function open(host) {
        var source = roots.get(host) || host.shadowRoot || host;
        if (!source.querySelector("svg")) {
            return;
        }
        opener = host;
        overlay = document.createElement("div");
        overlay.className = "rct-zoom";
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.setAttribute("aria-label", "Enlarged diagram");

        var button = document.createElement("button");
        button.type = "button";
        button.className = "rct-zoom__close";
        button.setAttribute("aria-label", "Close enlarged diagram");
        button.textContent = "×";

        // Own shadow root keeps the diagram's embedded styles from leaking into the page.
        var stage = document.createElement("div");
        stage.className = "rct-zoom__stage";
        var shadow = stage.attachShadow({ mode: "open" });
        Array.prototype.forEach.call(source.childNodes, function (node) {
            shadow.appendChild(node.cloneNode(true));
        });
        var fit = document.createElement("style");
        fit.textContent = "svg{width:100%;height:100%;max-width:none!important;max-height:none!important}";
        shadow.appendChild(fit);

        overlay.addEventListener("click", close);
        overlay.appendChild(stage);
        overlay.appendChild(button);
        document.body.appendChild(overlay);
        document.documentElement.classList.add("rct-zoom-open");
        document.addEventListener("keydown", onKey, true);
        button.focus();
    }

    function bind() {
        document.querySelectorAll(".mermaid").forEach(function (host) {
            if (host.dataset.rctZoom || !(roots.get(host) || host.shadowRoot || host.querySelector("svg"))) {
                return;
            }
            host.dataset.rctZoom = "1";
            host.tabIndex = 0;
            host.setAttribute("role", "button");
            host.setAttribute("aria-label", "Enlarge diagram");
            host.addEventListener("click", function () { open(host); });
            host.addEventListener("keydown", function (event) {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    open(host);
                }
            });
        });
    }

    // Diagrams render asynchronously after navigation; rebind as they appear.
    function start() {
        bind();
        new MutationObserver(bind).observe(document.body, { childList: true, subtree: true });
    }

    if (typeof document$ !== "undefined") {
        // navigation.instant swaps the content; close any stale overlay.
        document$.subscribe(function () { close(); bind(); });
    }
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
