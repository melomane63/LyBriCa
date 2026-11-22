(function () {
    "use strict";

    const BUTTON_ID = "lybrica-toolbar-button";
    const PANEL_ID = "lybrica-floating-panel";
    const OVERLAY_ID = "lybrica-overlay";

    // Leave empty if LyBriCa runs on the same machine as Material Skin.
    // Otherwise, enter the hostname or IP address of the machine running LyBriCa.
    // Example: "lybrica.local" or "192.168.1.50".
    const LYBRICA_HOST = "";
    const LYBRICA_PORT = 5010;
    const LYBRICA_URL =`http://${LYBRICA_HOST || location.hostname}:${LYBRICA_PORT}/`;
    const PLAYER_API_URL = LYBRICA_URL + "api/player";

    let lybricaMac = null;
    let store = null;
    let button = null;
    let lybricaFrame = null;

    // ----- Button visibility according to the selected player -----

    function updateButtonVisibility() {
        if (!button || !store || !lybricaMac) return;

        const selectedMac = store.state.player?.id?.toLowerCase();

        button.style.display =
            selectedMac === lybricaMac ? "" : "none";
    }

    // ----- Material Skin theme synchronisation -----

    function syncLybricaTheme() {
        if (!lybricaFrame?.contentWindow) return;

        const toolbar = document.querySelector(".v-toolbar");
        const list = document.querySelector(".v-list");

        if (!toolbar) return;

        const toolbarStyle = getComputedStyle(toolbar);
        const listStyle = list
            ? getComputedStyle(list)
            : toolbarStyle;

        const rootStyle = getComputedStyle(document.documentElement);

        const theme = {
            type: "lybrica-theme",
            bg: toolbarStyle.backgroundColor,
            area: listStyle.backgroundColor,
            line: toolbarStyle.borderBottomColor,
            fg: toolbarStyle.color,
            accent: rootStyle
                .getPropertyValue("--accent-color")
                .trim()
        };

        lybricaFrame.contentWindow.postMessage(theme, "*");
    }

    // ----- Retrieving the Vuex store -----

    function findStore() {
        if (store) return;

        const stores = [...new Set(
            [...document.querySelectorAll("*")]
                .map(el => el.__vue__?.$store)
                .filter(Boolean)
        )];

        if (!stores.length) return;

        store = stores[0];

        store.watch(
            state => state.player?.id,
            () => {
                updateButtonVisibility();

                // Let Material Skin update its theme first
                requestAnimationFrame(() => {
                    requestAnimationFrame(syncLybricaTheme);
                });
            }
        );

        updateButtonVisibility();
    }

    // ----- LyBriCa player detection -----

    async function loadLybricaPlayer() {
        try {
            const response = await fetch(
                PLAYER_API_URL,
                { cache: "no-store" }
            );

            if (!response.ok) {
                throw new Error("HTTP " + response.status);
            }

            const data = await response.json();

            if (!data.mac) {
                throw new Error("Missing player MAC");
            }

            lybricaMac = data.mac.toLowerCase();

            updateButtonVisibility();

        } catch (error) {
            console.warn(
                "[LyBriCa] Player detection failed:",
                error
            );

            lybricaMac = null;
            updateButtonVisibility();
        }
    }

    // ----- Closing LyBriCa: Escape, click outside, or message from the page -----

    function onKeyDown(event) {
        if (event.key === "Escape") closeLybrica();
    }

    // Escape pressed INSIDE the iframe: the LyBriCa page tells us through postMessage
    // (keyboard events from the iframe do not reach this page, the two pages are on different ports)
    function onMessage(event) {
        if (!lybricaFrame ||
            event.source !== lybricaFrame.contentWindow) {
            return;
        }

        if (event.data?.type === "lybrica-close") {
            closeLybrica();
            return;
        }

        if (event.data?.type === "lybrica-ready") {
            lybricaFrame.style.opacity = "1";
        }
    }

    function closeLybrica() {
        document.getElementById(PANEL_ID)?.remove();
        document.getElementById(OVERLAY_ID)?.remove();
        document.removeEventListener("keydown", onKeyDown, true);
        window.removeEventListener("message", onMessage);
        lybricaFrame = null;
    }

    // ----- Opening / closing LyBriCa -----

    function toggleLybrica() {
        if (document.getElementById(PANEL_ID)) {
            closeLybrica();
            return;
        }

        // Transparent overlay behind the window: a click on it closes LyBriCa
        const overlay = document.createElement("div");
        overlay.id = OVERLAY_ID;

        Object.assign(overlay.style, {
            position: "fixed",
            inset: "0",
            background: "transparent",
            zIndex: "99998"
        });

        overlay.addEventListener("pointerdown", closeLybrica);

        const panel = document.createElement("div");
        panel.id = PANEL_ID;

        Object.assign(panel.style, {
            position: "fixed",
            top: "76px",
            right: "20px",
            width: "680px",
            height: "520px",
            maxWidth: "calc(100vw - 30px)",
            maxHeight: "calc(100vh - 95px)",
            border: "1px solid #888",
            borderRadius: "8px",
            boxShadow: "0 5px 25px rgba(0,0,0,.4)",
            zIndex: "99999",
            overflow: "hidden",
            display: "flex",
            flexDirection: "column"
        });

        const frame = document.createElement("iframe");

        frame.src = LYBRICA_URL;
        frame.title = "LyBriCa";
        frame.style.width = "100%";
        frame.style.flex = "1";
        frame.style.border = "0";

        // Lets LyBriCa use the dark theme
        frame.style.colorScheme = "dark";

        lybricaFrame = frame;

        frame.style.opacity = "0";

        frame.addEventListener("load", () => {
            syncLybricaTheme();
        });


        panel.append(frame);
        document.body.append(overlay, panel);

        document.addEventListener("keydown", onKeyDown, true);
        window.addEventListener("message", onMessage);
    }

    // ----- Adding the button to the toolbar -----

    function addLybricaButton() {
        const existing = document.getElementById(BUTTON_ID);

        if (existing) {
            button = existing;
            updateButtonVisibility();
            return;
        }

        const volume = document.querySelector(".vol-full-slider");

        if (!volume) return;

        button = document.createElement("button");
        button.type = "button";
        button.id = BUTTON_ID;
        button.className = "v-btn v-btn--icon toolbar-button";
        button.title = "CamillaDSP";
        button.setAttribute("aria-label", "LyBriCa");

        button.innerHTML =
            '<span class="v-btn__content">' +
            '<i class="v-icon material-icons">tune</i>' +
            '</span>';

        button.addEventListener("click", toggleLybrica);

        button.style.display = "none";

        volume.insertAdjacentElement("beforebegin", button);

        updateButtonVisibility();
    }

    // ----- Initialisation -----

    function init() {
        addLybricaButton();
        findStore();
        loadLybricaPlayer();

        const observer = new MutationObserver(() => {
            addLybricaButton();
            findStore();
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener(
            "DOMContentLoaded",
            init,
            { once: true }
        );
    } else {
        init();
    }

})();
