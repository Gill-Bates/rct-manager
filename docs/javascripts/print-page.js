(function () {
    "use strict";

    function addPrintButton() {
        var content = document.querySelector(".md-content__inner");
        if (!content || content.querySelector(".rct-print-button")) {
            return;
        }

        var button = document.createElement("button");
        button.type = "button";
        button.className = "md-content__button rct-print-button";
        button.textContent = "Print / PDF";
        button.title = "Print or save this page as PDF";
        button.addEventListener("click", function () { window.print(); });
        content.prepend(button);
    }

    if (typeof document$ !== "undefined") {
        document$.subscribe(addPrintButton);
    } else if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", addPrintButton);
    } else {
        addPrintButton();
    }
})();
