(function () {
    const root = document.documentElement;
    const themeToggle = document.getElementById("theme-dark-toggle");
    // Side menu (desktop), "More" sheet entries, and the bottom tab bar
    // (mobile) all drive the same panels, so they share one handler and stay
    // in sync via data-target.
    const menuButtons = document.querySelectorAll(".menu-item, .bottom-nav-item[data-target]");
    const panels = document.querySelectorAll(".panel");

    // Mirrors the active panel onto <body> for CSS that needs to key off it
    // but should not depend on :has() support: .is-home-panel shows the
    // mobile FAB, and .is-flat-panel makes the panel shell transparent on
    // mobile for panels built from grouped-list cards, so the cards sit
    // directly on the page background.
    const FLAT_PANELS = ["panel-home", "panel-transfer"];

    function syncHomePanelBodyClass() {
        const activePanel = document.querySelector(".panel.active");
        const activeId = activePanel ? activePanel.id : "";
        document.body.classList.toggle("is-home-panel", activeId === "panel-home");
        document.body.classList.toggle("is-flat-panel", FLAT_PANELS.indexOf(activeId) !== -1);
    }
    syncHomePanelBodyClass();

    // Panels switch client-side without a reload, so live investment prices
    // (fetched only when the server itself renders panel=investments) can be
    // stale/missing the first time a user reaches Investments from another
    // panel. Each investments-section carries data-prices-loaded, rendered by
    // the server; when the section we're switching to says "0", force a full
    // reload so the server fetches live prices. The server always sets
    // data-prices-loaded="1" when it renders panel=investments, so this can't
    // loop, and it never fires on a normal full load of that panel (the
    // section is already marked loaded by the time this code runs).
    function investmentsSectionNeedsReload(sectionId) {
        const section = sectionId
            ? document.getElementById(sectionId)
            : document.querySelector(".investments-section.active") || document.getElementById("investments-crypto");
        return !!(section && section.getAttribute("data-prices-loaded") === "0");
    }

    const savedTheme = localStorage.getItem("theme");
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const theme = savedTheme || (prefersDark ? "dark" : "light");

    root.setAttribute("data-theme", theme);
    if (themeToggle) {
        themeToggle.checked = theme === "dark";
        themeToggle.addEventListener("change", function () {
            const next = themeToggle.checked ? "dark" : "light";
            root.setAttribute("data-theme", next);
            localStorage.setItem("theme", next);
        });
    }

    function syncRecurringDuration(form) {
        if (!form) {
            return;
        }
        const toggle = form.querySelector("[data-recurring-indefinite-toggle]");
        const wrap = form.querySelector("[data-recurring-months-wrap]");
        const input = form.querySelector("[data-recurring-months-input]");
        if (!toggle || !wrap || !input) {
            return;
        }
        const indefinite = !!toggle.checked;
        wrap.hidden = indefinite;
        input.disabled = indefinite;
        input.required = !indefinite;
        if (indefinite) {
            input.value = "";
        }
    }

    function bindRecurringDurationControls(scope) {
        const forms = (scope || document).querySelectorAll(".recurring-add-form, .recurring-rule-form");
        forms.forEach(function (form) {
            const toggle = form.querySelector("[data-recurring-indefinite-toggle]");
            if (!toggle || toggle.dataset.boundRecurringDuration === "1") {
                syncRecurringDuration(form);
                return;
            }
            toggle.dataset.boundRecurringDuration = "1";
            toggle.addEventListener("change", function () {
                syncRecurringDuration(form);
            });
            syncRecurringDuration(form);
        });
    }

    bindRecurringDurationControls(document);

    (function setupLazyEditDetails() {
        const templates = {
            expense: document.getElementById("expense-edit-form-template"),
            income: document.getElementById("income-edit-form-template"),
        };
        const actionPaths = {
            expense: "/expenses/",
            income: "/income/",
        };

        function mountEditForm(details) {
            const kind = details.getAttribute("data-edit-kind");
            const entryId = details.getAttribute("data-edit-id");
            const slot = details.querySelector(".inline-edit-slot");
            const tpl = templates[kind];
            if (!kind || !entryId || !slot || !tpl || slot.dataset.loaded === "1") {
                return;
            }
            let payload = {};
            try {
                payload = JSON.parse(details.getAttribute("data-edit-payload") || "{}");
            } catch (err) {
                payload = {};
            }
            const form = tpl.content.firstElementChild.cloneNode(true);
            form.action = actionPaths[kind] + entryId + "/edit";
            const notesField = form.querySelector('[name="notes"]');
            const amountField = form.querySelector('[name="amount"]');
            const categoryField = form.querySelector('[name="category_id"]');
            const accountField = form.querySelector('[name="account_id"]');
            const dateField = form.querySelector(
                kind === "expense" ? '[name="spent_at"]' : '[name="received_at"]'
            );
            if (notesField) {
                notesField.value = payload.notes || "";
            }
            if (amountField && payload.amount !== undefined && payload.amount !== null) {
                amountField.value = payload.amount;
            }
            if (categoryField && payload.category_id !== undefined) {
                categoryField.value = String(payload.category_id);
            }
            if (accountField && payload.account_id !== undefined) {
                accountField.value = String(payload.account_id);
            }
            if (dateField) {
                dateField.value = payload.spent_at || payload.received_at || "";
            }
            slot.appendChild(form);
            slot.dataset.loaded = "1";
        }

        document.querySelectorAll("details.lazy-edit-details").forEach(function (details) {
            details.addEventListener("toggle", function () {
                if (details.open) {
                    mountEditForm(details);
                }
            });
        });
    })();

    const settingsSections = document.querySelectorAll(".settings-section");
    const settingsSectionMap = {
        "settings-general": "general",
        "settings-banks": "banks",
        "settings-expenses": "expenses",
        "settings-income": "income",
        "settings-integrations": "integrations",
        "settings-export": "export",
        "settings-migration": "migration"
    };

    function readShellMonth() {
        const fallbackMonth = document.body.dataset.monthFilter || "";
        function ymFromPair(yearEl, monthEl) {
            if (!yearEl || !monthEl || !yearEl.value || !monthEl.value) {
                return "";
            }
            return `${yearEl.value}-${String(monthEl.value).padStart(2, "0")}`;
        }
        const activePanel = document.querySelector(".panel.active");
        if (activePanel) {
            const y = activePanel.querySelector(".shell-cal-year");
            const m = activePanel.querySelector(".shell-cal-month");
            const pair = ymFromPair(y, m);
            if (pair) {
                return pair;
            }
        }
        const anyY = document.querySelector(".shell-cal-year");
        const anyM = document.querySelector(".shell-cal-month");
        const fallbackPair = ymFromPair(anyY, anyM);
        if (fallbackPair) {
            return fallbackPair;
        }
        return fallbackMonth;
    }

    function syncShellUrl(panelKey) {
        const params = new URLSearchParams(window.location.search);
        params.set("panel", panelKey);
        const monthValue = readShellMonth();
        if (monthValue) {
            params.set("month", monthValue);
        }
        if (panelKey === "settings") {
            const activeSubBtn = document.querySelector('.menu-sub-item.active[data-target="panel-settings"]');
            const sectionKey = activeSubBtn ? activeSubBtn.getAttribute("data-section-key") : "general";
            params.set("settings_section", sectionKey);
        } else {
            params.delete("settings_section");
        }
        if (panelKey === "investments") {
            const activeSubBtn = document.querySelector('.menu-sub-item.active[data-target="panel-investments"]');
            const invKey = activeSubBtn ? activeSubBtn.getAttribute("data-section-key") : "crypto";
            params.set("investments_section", invKey);
        } else {
            params.delete("investments_section");
        }
        if (panelKey === "yearly") {
            const yearlyYear = document.getElementById("yearly-year-input");
            const fallbackYear = document.body.dataset.yearFilter || "";
            const y = yearlyYear && yearlyYear.value ? yearlyYear.value : String(fallbackYear);
            if (y) {
                params.set("year", y);
            }
        } else {
            params.delete("year");
        }
        if (panelKey === "reports") {
            const activeReportsSub = document.querySelector('.menu-sub-item.active[data-target="panel-reports"]');
            const reportsSecKey = activeReportsSub ? activeReportsSub.getAttribute("data-section-key") : "overview";
            params.set("reports_section", reportsSecKey);
            // Every report section carries its own year picker; they are kept
            // in step by the server, so whichever is on screen is authoritative.
            const reportsYear =
                document.getElementById("reports-year-" + reportsSecKey) ||
                document.querySelector(".reports-section.active select[name='report_year']") ||
                document.querySelector("select[name='report_year']");
            const fallbackReportYear = document.body.dataset.reportYear || "";
            const ry = reportsYear && reportsYear.value ? reportsYear.value : String(fallbackReportYear);
            if (ry) {
                params.set("report_year", ry);
            }
            const reportAccount = document.querySelector("select[name='report_account']");
            if (reportAccount && reportAccount.value) {
                params.set("report_account", reportAccount.value);
            } else {
                params.delete("report_account");
            }
        } else {
            params.delete("report_year");
            params.delete("reports_section");
            params.delete("report_account");
        }
        if (panelKey === "expenses") {
            const expM = document.getElementById("exp-cal-month");
            const expY = document.getElementById("exp-cal-year");
            if (expM && expY && expM.value && expY.value) {
                params.set("exp_month", `${expY.value}-${String(expM.value).padStart(2, "0")}`);
            } else {
                params.delete("exp_month");
            }
            const expCat = document.getElementById("exp-category-filter");
            if (expCat && expCat.value) {
                params.set("exp_category", expCat.value);
            } else {
                params.delete("exp_category");
            }
        } else {
            params.delete("exp_month");
            params.delete("exp_page");
            params.delete("exp_category");
        }
        if (panelKey === "income") {
            const incM = document.getElementById("inc-cal-month");
            const incY = document.getElementById("inc-cal-year");
            if (incM && incY && incM.value && incY.value) {
                params.set("inc_month", `${incY.value}-${String(incM.value).padStart(2, "0")}`);
            } else {
                params.delete("inc_month");
            }
            const incCat = document.getElementById("inc-category-filter");
            if (incCat && incCat.value) {
                params.set("inc_category", incCat.value);
            } else {
                params.delete("inc_category");
            }
        } else {
            params.delete("inc_month");
            params.delete("inc_page");
            params.delete("inc_category");
        }
        const nextUrl = `${window.location.pathname}?${params.toString()}`;
        window.history.replaceState({}, "", nextUrl);
    }

    const investSections = document.querySelectorAll(".investments-section");
    const investSectionMap = {
        "investments-crypto": "crypto",
        "investments-stocks": "stocks"
    };

    const reportsSections = document.querySelectorAll(".reports-section");

    const subMenuItems = document.querySelectorAll(".menu-sub-item");
    const subMenuGroups = document.querySelectorAll(".menu-sub-items");

    subMenuItems.forEach(function (subBtn) {
        subBtn.addEventListener("click", function () {
            const panelTarget = subBtn.getAttribute("data-target");
            const sectionId = subBtn.getAttribute("data-section");

            menuButtons.forEach(function (item) {
                item.classList.toggle("active", item.getAttribute("data-target") === panelTarget);
            });
            panels.forEach(function (panel) {
                panel.classList.toggle("active", panel.id === panelTarget);
            });
            syncHomePanelBodyClass();

            subMenuGroups.forEach(function (group) {
                const parentBtn = group.previousElementSibling;
                group.classList.toggle("active", parentBtn && parentBtn.getAttribute("data-target") === panelTarget);
            });

            subMenuItems.forEach(function (s) {
                s.classList.toggle("active", s === subBtn);
            });

            if (panelTarget === "panel-settings") {
                settingsSections.forEach(function (section) {
                    section.classList.toggle("active", section.id === sectionId);
                });
            }
            if (panelTarget === "panel-investments") {
                investSections.forEach(function (section) {
                    section.classList.toggle("active", section.id === sectionId);
                });
            }
            if (panelTarget === "panel-reports") {
                reportsSections.forEach(function (section) {
                    section.classList.toggle("active", section.id === sectionId);
                });
            }

            const panelMap2 = {
                "panel-investments": "investments",
                "panel-settings": "settings",
                "panel-reports": "reports"
            };
            var panelKey = panelMap2[panelTarget];
            if (panelKey) {
                syncShellUrl(panelKey);
                if (panelKey === "investments" && investmentsSectionNeedsReload(sectionId)) {
                    window.location.reload();
                    return;
                }
            }
        });
    });

    menuButtons.forEach(function (menuButton) {
        menuButton.addEventListener("click", function () {
            const target = menuButton.getAttribute("data-target");
            // Compare by target, not identity: the same panel is reachable
            // from the side menu, the bottom bar and the "More" sheet, and all
            // of them should light up together.
            menuButtons.forEach(function (item) {
                item.classList.toggle("active", item.getAttribute("data-target") === target);
            });
            panels.forEach(function (panel) {
                panel.classList.toggle("active", panel.id === target);
            });
            syncHomePanelBodyClass();

            subMenuGroups.forEach(function (group) {
                const parentBtn = group.previousElementSibling;
                group.classList.toggle(
                    "active",
                    parentBtn && parentBtn.getAttribute("data-target") === target
                );
            });

            const panelMap = {
                "panel-home": "home",
                "panel-expenses": "expenses",
                "panel-income": "income",
                "panel-recurring": "recurring",
                "panel-transfer": "transfer",
                "panel-budget": "budget",
                "panel-summary": "summary",
                "panel-yearly": "yearly",
                "panel-reports": "reports",
                "panel-investments": "investments",
                "panel-settings": "settings"
            };
            const panelValue = panelMap[target];
            if (panelValue) {
                syncShellUrl(panelValue);
                if (panelValue === "investments" && investmentsSectionNeedsReload()) {
                    window.location.reload();
                    return;
                }
            }
        });
    });

    (function setupStockSearch() {
        const searchInput = document.getElementById("stock-search-input");
        const resultsBox = document.getElementById("stock-search-results");
        const symbolInput = document.getElementById("stock-symbol");
        const tickerInput = document.getElementById("stock-ticker");
        const nameInput = document.getElementById("stock-name");
        if (!searchInput || !resultsBox || !symbolInput || !tickerInput || !nameInput) return;

        let debounceTimer = null;
        let lastItems = [];

        function applyStockPick(c) {
            symbolInput.value = c.symbol;
            tickerInput.value = c.ticker;
            nameInput.value = c.name;
            searchInput.value = c.symbol + " — " + c.name;
            resultsBox.hidden = true;
        }

        function renderResults(items) {
            lastItems = items;
            if (!items.length) {
                resultsBox.hidden = true;
                return;
            }
            resultsBox.innerHTML = "";
            items.forEach(function (c) {
                const item = document.createElement("button");
                item.type = "button";
                item.className = "crypto-search-item";
                item.innerHTML =
                    '<span class="crypto-search-item-name"><strong>' + c.ticker + "</strong> " + c.name + "</span>" +
                    '<span class="crypto-search-item-id muted">' + c.symbol + (c.type ? " · " + c.type : "") + "</span>";
                item.addEventListener("mousedown", function (e) {
                    e.preventDefault();
                    applyStockPick(c);
                });
                item.addEventListener("click", function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    applyStockPick(c);
                });
                resultsBox.appendChild(item);
            });
            resultsBox.hidden = false;
        }

        function fetchStockSearch(q, onDone) {
            fetch("/stocks/search?q=" + encodeURIComponent(q))
                .then(function (r) { return r.json(); })
                .then(function (items) { onDone(items || []); })
                .catch(function () { onDone([]); });
        }

        function runSearch(q) {
            if (q.length < 1) {
                resultsBox.hidden = true;
                return;
            }
            fetchStockSearch(q, renderResults);
        }

        searchInput.addEventListener("input", function () {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(function () {
                runSearch(searchInput.value.trim());
            }, 350);
        });

        searchInput.addEventListener("keydown", function (e) {
            if (e.key === "Enter" && lastItems.length) {
                e.preventDefault();
                applyStockPick(lastItems[0]);
            }
        });

        symbolInput.addEventListener("blur", function () {
            const sym = symbolInput.value.trim().toUpperCase();
            if (sym.length < 3) return;
            if (tickerInput.value && nameInput.value && symbolInput.value.toUpperCase() === sym) return;
            fetchStockSearch(sym, function (items) {
                const exact = items.find(function (i) { return i.symbol.toUpperCase() === sym; });
                const pick = exact || (items.length === 1 ? items[0] : null);
                if (pick) applyStockPick(pick);
            });
        });

        document.addEventListener("click", function (e) {
            if (!resultsBox.contains(e.target) && e.target !== searchInput) {
                resultsBox.hidden = true;
            }
        });
    })();

    (function setupCryptoSearch() {
        const searchInput = document.getElementById("crypto-search-input");
        const resultsBox = document.getElementById("crypto-search-results");
        const coinIdInput = document.getElementById("crypto-coin-id");
        const symbolInput = document.getElementById("crypto-symbol");
        const nameInput = document.getElementById("crypto-name");
        if (!searchInput || !resultsBox) return;

        let debounceTimer = null;

        searchInput.addEventListener("input", function () {
            clearTimeout(debounceTimer);
            const q = searchInput.value.trim();
            if (q.length < 2) {
                resultsBox.hidden = true;
                return;
            }
            debounceTimer = setTimeout(function () {
                fetch("/crypto/search?q=" + encodeURIComponent(q))
                    .then(function (r) { return r.json(); })
                    .then(function (coins) {
                        if (!coins.length) {
                            resultsBox.hidden = true;
                            return;
                        }
                        resultsBox.innerHTML = "";
                        coins.forEach(function (c) {
                            const item = document.createElement("button");
                            item.type = "button";
                            item.className = "crypto-search-item";
                            item.innerHTML =
                                (c.thumb ? '<img src="' + c.thumb + '" class="crypto-search-thumb" alt="">' : "") +
                                '<span class="crypto-search-item-name"><strong>' + c.symbol.toUpperCase() + "</strong> " + c.name + "</span>" +
                                '<span class="crypto-search-item-id muted">' + c.id + "</span>";
                            item.addEventListener("click", function () {
                                coinIdInput.value = c.id;
                                symbolInput.value = c.symbol.toUpperCase();
                                nameInput.value = c.name;
                                searchInput.value = "";
                                resultsBox.hidden = true;
                            });
                            resultsBox.appendChild(item);
                        });
                        resultsBox.hidden = false;
                    })
                    .catch(function () {
                        resultsBox.hidden = true;
                    });
            }, 300);
        });

        document.addEventListener("click", function (e) {
            if (!resultsBox.contains(e.target) && e.target !== searchInput) {
                resultsBox.hidden = true;
            }
        });
    })();

    (function setupSelectPickers() {
        // Progressive enhancement: the <select> stays in the DOM (still named,
        // still validated, still what actually gets submitted) but is visually
        // replaced by a row-style button that opens a shared iOS-style picker
        // sheet. Selects opt in with [data-picker].
        const selects = document.querySelectorAll("select[data-picker]");
        if (!selects.length) {
            return;
        }

        const sheet = document.createElement("div");
        // Reuses .home-modal's own fixed/backdrop/bottom-sheet-on-mobile CSS;
        // .picker-sheet only adds the z-index bump that stacks it above it.
        sheet.className = "home-modal picker-sheet";
        sheet.hidden = true;
        sheet.setAttribute("aria-hidden", "true");
        sheet.setAttribute("role", "dialog");
        sheet.setAttribute("aria-modal", "true");
        sheet.innerHTML =
            '<button type="button" class="home-modal-backdrop" data-picker-close aria-label="Close"></button>' +
            '<div class="home-modal-dialog picker-sheet-dialog">' +
            '<div class="home-modal-handle" aria-hidden="true"></div>' +
            '<div class="home-modal-header">' +
            '<button type="button" class="home-modal-close" data-picker-close aria-label="Close">' +
            '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>' +
            "</button>" +
            '<h3 class="home-modal-title picker-sheet-title"></h3>' +
            '<span class="picker-sheet-spacer" aria-hidden="true"></span>' +
            "</div>" +
            '<ul class="picker-sheet-list"></ul>' +
            "</div>";
        // Appended once, to <body>, and reused for every enhanced select - it
        // stacks above whichever .home-modal happens to be open underneath it.
        document.body.appendChild(sheet);

        const titleEl = sheet.querySelector(".picker-sheet-title");
        const listEl = sheet.querySelector(".picker-sheet-list");
        const closeBtn = sheet.querySelector(".home-modal-close");
        let activeTrigger = null;

        function optionLabel(select) {
            const opt = select.options[select.selectedIndex];
            return opt ? opt.textContent : "";
        }

        function avatarLetter(text) {
            const trimmed = (text || "").trim();
            return trimmed ? trimmed.charAt(0).toUpperCase() : "?";
        }

        // Account name -> bank badge, rendered server-side by banks.py.
        let bankBadges = {};
        try {
            const badgeData = document.getElementById("bank-badges");
            bankBadges = badgeData ? JSON.parse(badgeData.textContent) || {} : {};
        } catch (err) {
            bankBadges = {};
        }

        function buildAvatar(option, select) {
            const avatar = document.createElement("span");
            avatar.className = "picker-sheet-avatar";
            avatar.setAttribute("aria-hidden", "true");
            const isAccount = /account_id$/.test(select.name || "");
            const bank = isAccount ? bankBadges[option.textContent] : null;
            if (bank && bank.logo) {
                avatar.classList.add("bank-avatar", "bank-avatar-logo");
                const img = document.createElement("img");
                img.src = bank.logo;
                img.alt = "";
                avatar.appendChild(img);
            } else if (bank) {
                avatar.classList.add("bank-avatar");
                if (bank.label.length > 2) {
                    avatar.classList.add("bank-avatar-long");
                }
                avatar.style.setProperty("--bank-bg", bank.bg);
                avatar.style.setProperty("--bank-fg", bank.fg);
                avatar.textContent = bank.label;
            } else {
                avatar.textContent = avatarLetter(option.textContent);
            }
            return avatar;
        }

        function closePicker() {
            if (sheet.hidden) {
                return;
            }
            sheet.hidden = true;
            sheet.setAttribute("aria-hidden", "true");
            if (activeTrigger) {
                activeTrigger.focus();
            }
            activeTrigger = null;
        }

        function buildOptionRow(option, select) {
            const li = document.createElement("li");
            const optBtn = document.createElement("button");
            optBtn.type = "button";
            optBtn.className = "picker-sheet-option";
            optBtn.setAttribute("role", "option");
            const selected = option.value === select.value;
            optBtn.setAttribute("aria-selected", selected ? "true" : "false");
            optBtn.innerHTML =
                '<span class="picker-sheet-name"></span>' +
                '<span class="picker-sheet-check" aria-hidden="true">' + (selected ? "✓" : "") + "</span>";
            optBtn.insertBefore(buildAvatar(option, select), optBtn.firstChild);
            optBtn.querySelector(".picker-sheet-name").textContent = option.textContent;
            optBtn.addEventListener("click", function () {
                select.value = option.value;
                select.dispatchEvent(new Event("change", { bubbles: true }));
                closePicker();
            });
            li.appendChild(optBtn);
            return li;
        }

        function renderOptions(select) {
            listEl.innerHTML = "";
            for (let i = 0; i < select.children.length; i += 1) {
                const child = select.children[i];
                if (child.tagName === "OPTGROUP") {
                    const header = document.createElement("li");
                    header.className = "picker-sheet-group-label";
                    header.textContent = child.label;
                    listEl.appendChild(header);
                    for (let j = 0; j < child.children.length; j += 1) {
                        listEl.appendChild(buildOptionRow(child.children[j], select));
                    }
                } else if (child.tagName === "OPTION") {
                    listEl.appendChild(buildOptionRow(child, select));
                }
            }
        }

        function openPicker(select, trigger, title) {
            activeTrigger = trigger;
            titleEl.textContent = title;
            renderOptions(select);
            sheet.hidden = false;
            sheet.setAttribute("aria-hidden", "false");
            closeBtn.focus();
        }

        sheet.querySelectorAll("[data-picker-close]").forEach(function (el) {
            el.addEventListener("click", closePicker);
        });

        // Registered ahead of setupHomeQuickModals below, so its keydown
        // listener runs first: stopImmediatePropagation keeps the Escape key
        // from also closing the .home-modal underneath the picker.
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && !sheet.hidden) {
                event.stopImmediatePropagation();
                closePicker();
            }
        });

        selects.forEach(function (select) {
            const label = select.id ? document.querySelector("label[for='" + select.id + "']") : null;
            const title = label ? label.textContent.trim() : select.getAttribute("aria-label") || "";

            const trigger = document.createElement("button");
            trigger.type = "button";
            trigger.className = "picker-trigger";
            if (select.id) {
                trigger.id = select.id + "-trigger";
            }
            trigger.innerHTML =
                '<span class="picker-trigger-value"></span>' +
                '<span class="picker-trigger-chevron" aria-hidden="true"></span>';
            trigger.querySelector(".picker-trigger-value").textContent = optionLabel(select);

            select.insertAdjacentElement("afterend", trigger);
            // Visually hidden, not display:none, so `required` validation and
            // its native error bubble still work; out of the tab order because
            // the button above is now the focusable, operable control.
            select.classList.add("picker-select-source");
            select.setAttribute("tabindex", "-1");
            select.setAttribute("aria-hidden", "true");
            if (label && trigger.id) {
                label.setAttribute("for", trigger.id);
            }

            trigger.addEventListener("click", function () {
                openPicker(select, trigger, title);
            });

            select.addEventListener("change", function () {
                trigger.querySelector(".picker-trigger-value").textContent = optionLabel(select);
            });
        });
    })();

    (function setupTransferForm() {
        // Runs after setupSelectPickers above, which is what actually creates
        // the #transfer-from-trigger / #transfer-to-trigger buttons this
        // reaches into.
        const form = document.getElementById("transfer-add-form");
        const fromSelect = document.getElementById("transfer-from");
        const toSelect = document.getElementById("transfer-to");
        if (!form || !fromSelect || !toSelect) {
            return;
        }
        const swapBtn = document.getElementById("transfer-swap-btn");
        const hint = document.getElementById("transfer-same-account-hint");
        const submitBtn = document.getElementById("transfer-submit-btn");

        function renderValue(select) {
            const trigger = document.getElementById(select.id + "-trigger");
            const valueEl = trigger ? trigger.querySelector(".picker-trigger-value") : null;
            if (!valueEl) {
                return;
            }
            const option = select.options[select.selectedIndex];
            valueEl.classList.add("picker-trigger-value--stacked");
            valueEl.innerHTML = "";
            const nameEl = document.createElement("span");
            nameEl.className = "transfer-account-name";
            nameEl.textContent = option ? option.textContent : "";
            valueEl.appendChild(nameEl);
            const balance = option ? option.getAttribute("data-balance") : "";
            if (balance) {
                const balanceEl = document.createElement("span");
                balanceEl.className = "transfer-account-balance";
                balanceEl.textContent = balance;
                valueEl.appendChild(balanceEl);
            }
        }

        function checkSameAccount() {
            const same = !!fromSelect.value && fromSelect.value === toSelect.value;
            if (hint) {
                hint.hidden = !same;
            }
            if (submitBtn) {
                submitBtn.disabled = same;
            }
        }

        [fromSelect, toSelect].forEach(function (select) {
            renderValue(select);
            select.addEventListener("change", function () {
                renderValue(select);
                checkSameAccount();
            });
        });
        checkSameAccount();

        if (swapBtn) {
            swapBtn.addEventListener("click", function () {
                const fromValue = fromSelect.value;
                const toValue = toSelect.value;
                fromSelect.value = toValue;
                toSelect.value = fromValue;
                // The server does the real "different accounts" check too;
                // this is just so the swap and the hint/submit state agree
                // immediately without a round trip.
                fromSelect.dispatchEvent(new Event("change", { bubbles: true }));
                toSelect.dispatchEvent(new Event("change", { bubbles: true }));
            });
        }
    })();

    (function setupHomeQuickModals() {
        // The FAB is a second trigger for the same expense sheet the quick-bar
        // button opens, so both are listed for that one modal/kind pair.
        const modalConfigs = [
            { modal: document.getElementById("home-modal-expense"), buttons: [document.getElementById("home-btn-expense"), document.getElementById("home-fab")], kind: "expense" },
            { modal: document.getElementById("home-modal-income"), buttons: [document.getElementById("home-btn-income")], kind: "income" },
            { modal: document.getElementById("recurring-modal-add"), buttons: [document.getElementById("recurring-btn-add")], kind: "recurring" },
            { modal: document.getElementById("budget-modal-copy"), buttons: [document.getElementById("budget-copy-month-btn")], kind: "budget-copy" },
        ].map(function (entry) {
            entry.buttons = entry.buttons.filter(Boolean);
            return entry;
        }).filter(function (entry) {
            return entry.modal && entry.buttons.length;
        });
        if (!modalConfigs.length) {
            return;
        }

        function findEntry(kind) {
            return modalConfigs.filter(function (entry) {
                return entry.kind === kind;
            })[0];
        }

        function focusFirstField(modal) {
            // Search inside the form only: the header's Save button is also a
            // button[type="submit"] and sits before the form in the DOM, so a
            // modal-wide search picked it (display:none on desktop, so focus
            // silently left the dialog; visible on mobile, so it got focused
            // instead of the amount field).
            const form = modal.querySelector("form") || modal;
            const field = form.querySelector(
                "input.home-modal-amount-input, textarea, input:not([type='hidden']):not([type='checkbox']), select"
            );
            if (field) {
                field.focus();
            }
        }

        function closeAll() {
            modalConfigs.forEach(function (entry) {
                entry.modal.hidden = true;
                entry.modal.setAttribute("aria-hidden", "true");
                entry.buttons.forEach(function (button) {
                    button.setAttribute("aria-expanded", "false");
                    button.classList.remove("is-active");
                });
            });
            document.body.classList.remove("home-modal-open");
        }

        function openModal(modal, buttons) {
            closeAll();
            modal.hidden = false;
            modal.setAttribute("aria-hidden", "false");
            buttons.forEach(function (button) {
                button.setAttribute("aria-expanded", "true");
                button.classList.add("is-active");
            });
            document.body.classList.add("home-modal-open");
            focusFirstField(modal);
        }

        modalConfigs.forEach(function (entry) {
            entry.buttons.forEach(function (button) {
                button.addEventListener("click", function () {
                    if (entry.modal.hidden) {
                        openModal(entry.modal, entry.buttons);
                    } else {
                        closeAll();
                    }
                });
            });
        });

        document.querySelectorAll("[data-home-modal-close]").forEach(function (el) {
            el.addEventListener("click", closeAll);
        });

        // Segmented Expense/Income control inside those two sheets: swap which
        // sheet is open without touching the escape/backdrop/focus plumbing above.
        document.querySelectorAll("[data-home-modal-switch]").forEach(function (el) {
            el.addEventListener("click", function () {
                const target = findEntry(el.getAttribute("data-home-modal-switch"));
                if (!target) {
                    return;
                }
                const current = modalConfigs.filter(function (entry) {
                    return !entry.modal.hidden;
                })[0];
                // Carry the amount and notes across so switching Expense/Income
                // mid-entry doesn't throw away what was already typed.
                if (current && current !== target) {
                    const fromAmount = current.modal.querySelector(".home-modal-amount-input");
                    const toAmount = target.modal.querySelector(".home-modal-amount-input");
                    if (fromAmount && toAmount) {
                        // Income has no sign of its own, and the expense sheet
                        // only applies its -/+ toggle at submit time, so the
                        // field itself can hold a "-" the user typed. Carry the
                        // magnitude across either direction rather than a
                        // negative value landing in income's min="0" input.
                        const amountValue = fromAmount.value;
                        if (amountValue === "") {
                            toAmount.value = "";
                        } else {
                            const parsed = parseFloat(amountValue);
                            toAmount.value = isNaN(parsed) ? amountValue : Math.abs(parsed);
                        }
                    }
                    const fromNotes = current.modal.querySelector("textarea[name='notes']");
                    const toNotes = target.modal.querySelector("textarea[name='notes']");
                    if (fromNotes && toNotes) {
                        toNotes.value = fromNotes.value;
                    }
                }
                openModal(target.modal, target.buttons);
            });
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && document.body.classList.contains("home-modal-open")) {
                closeAll();
            }
        });
    })();

    (function setupAmountSignToggles() {
        // Expenses are signed - negative spends, positive refunds (see
        // helpers.normalize_expense_amount) - but type="number" has no way to
        // type a leading "+", so a plain amount field left refunds as the only
        // reachable positive value. This round -/+ toggle picks the sign for
        // whatever was typed as a positive number; an explicit "-" always wins.
        const toggles = document.querySelectorAll("[data-amount-sign-toggle]");
        if (!toggles.length) {
            return;
        }

        function fieldFor(toggle) {
            return toggle.closest("[data-amount-sign-field]");
        }

        function amountInputFor(toggle) {
            const field = fieldFor(toggle);
            return field ? field.querySelector("input[type='number']") : null;
        }

        function applyState(toggle, isRefund) {
            toggle.setAttribute("aria-pressed", isRefund ? "true" : "false");
            toggle.setAttribute("aria-label", "Amount sign: " + (isRefund ? "refund" : "expense"));
            toggle.textContent = isRefund ? "+" : "−";
        }

        toggles.forEach(function (toggle) {
            applyState(toggle, toggle.getAttribute("aria-pressed") === "true");

            toggle.addEventListener("click", function () {
                applyState(toggle, toggle.getAttribute("aria-pressed") !== "true");
            });

            const form = toggle.closest("form");
            const amountInput = amountInputFor(toggle);
            if (!form || !amountInput) {
                return;
            }
            // Runs after the browser's own constraint validation (a required/
            // invalid field never gets this far) and before the form's data is
            // collected for the request.
            form.addEventListener("submit", function () {
                const parsed = parseFloat(amountInput.value);
                const isRefund = toggle.getAttribute("aria-pressed") === "true";
                if (!isNaN(parsed) && parsed > 0 && !isRefund) {
                    amountInput.value = String(-parsed);
                }
            });
        });

        // The Home expense sheet always reopens on "-" (spending), regardless
        // of how it was left last time.
        const homeExpenseModal = document.getElementById("home-modal-expense");
        const homeExpenseToggle = homeExpenseModal
            ? homeExpenseModal.querySelector("[data-amount-sign-toggle]")
            : null;
        if (homeExpenseModal && homeExpenseToggle) {
            new MutationObserver(function () {
                if (!homeExpenseModal.hidden) {
                    applyState(homeExpenseToggle, false);
                }
            }).observe(homeExpenseModal, { attributes: true, attributeFilter: ["hidden"] });
        }
    })();

    (function setupMoreSheet() {
        const sheet = document.getElementById("more-sheet");
        const trigger = document.getElementById("more-nav-btn");
        if (!sheet || !trigger) {
            return;
        }

        const MORE_PANELS = [
            "panel-recurring",
            "panel-budget",
            "panel-transfer",
            "panel-summary",
            "panel-yearly",
            "panel-investments",
            "panel-settings",
        ];

        function close() {
            sheet.hidden = true;
            sheet.setAttribute("aria-hidden", "true");
            trigger.setAttribute("aria-expanded", "false");
            document.body.classList.remove("home-modal-open");
        }

        function open() {
            sheet.hidden = false;
            sheet.setAttribute("aria-hidden", "false");
            trigger.setAttribute("aria-expanded", "true");
            document.body.classList.add("home-modal-open");
            // Focus the close button rather than the first row: focusing a
            // list item drew a full-width default focus ring around it, which
            // read as a rendering glitch rather than a deliberate highlight.
            const closeBtn = sheet.querySelector(".home-modal-close");
            if (closeBtn) {
                closeBtn.focus();
            }
        }

        trigger.addEventListener("click", function () {
            if (sheet.hidden) {
                open();
            } else {
                close();
            }
        });

        sheet.querySelectorAll("[data-more-sheet-close]").forEach(function (el) {
            el.addEventListener("click", close);
        });

        // tapping the dimmed backdrop closes the sheet
        sheet.addEventListener("click", function (event) {
            if (event.target === sheet) {
                close();
            }
        });

        // picking a panel closes the sheet; the shared menu handler has
        // already switched panels by the time this runs
        sheet.querySelectorAll(".menu-item").forEach(function (item) {
            item.addEventListener("click", close);
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && !sheet.hidden) {
                close();
            }
        });

        // Keep "More" highlighted whenever one of its panels is the active one.
        function syncMoreActive() {
            const active = document.querySelector(".panel.active");
            trigger.classList.toggle("active", !!active && MORE_PANELS.indexOf(active.id) !== -1);
        }
        document.querySelectorAll(".menu-item, .bottom-nav-item[data-target]").forEach(function (item) {
            item.addEventListener("click", syncMoreActive);
        });
        syncMoreActive();
    })();

    (function setupAiModelLoader() {
        const loadBtn = document.getElementById("ai-load-models");
        const baseUrlInput = document.getElementById("ai-base-url");
        const modelInput = document.getElementById("ai-model");
        const modelList = document.getElementById("ai-model-options");
        const statusEl = document.getElementById("ai-models-status");
        if (!loadBtn || !baseUrlInput || !modelList) {
            return;
        }

        loadBtn.addEventListener("click", function () {
            const baseUrl = baseUrlInput.value.trim();
            if (!baseUrl) {
                if (statusEl) {
                    statusEl.textContent = "Enter a Base URL first.";
                }
                return;
            }
            loadBtn.disabled = true;
            if (statusEl) {
                statusEl.textContent = "Loading models…";
            }
            fetch("/settings/integrations/models?base_url=" + encodeURIComponent(baseUrl))
                .then(function (resp) {
                    return resp.json().then(function (data) {
                        return { ok: resp.ok, data: data };
                    });
                })
                .then(function (result) {
                    if (!result.ok) {
                        throw new Error(result.data.error || "Could not load models.");
                    }
                    const models = result.data.models || [];
                    modelList.innerHTML = "";
                    models.forEach(function (name) {
                        const opt = document.createElement("option");
                        opt.value = name;
                        modelList.appendChild(opt);
                    });
                    if (statusEl) {
                        statusEl.textContent = models.length
                            ? models.length + " model(s) loaded."
                            : "Connected, but no models are installed.";
                    }
                    if (models.length === 1 && modelInput && !modelInput.value.trim()) {
                        modelInput.value = models[0];
                    }
                })
                .catch(function (err) {
                    if (statusEl) {
                        statusEl.textContent = err.message || "Could not load models.";
                    }
                })
                .finally(function () {
                    loadBtn.disabled = false;
                });
        });
    })();

    (function setupBudgetTools() {
        const form = document.getElementById("budget-defaults-form");
        if (!form) return;

        const rowsContainer = document.getElementById("budget-default-rows");
        const distributeTotal = document.getElementById("budget-distribute-total");
        const distributeBtn = document.getElementById("budget-distribute-btn");
        const statusEl = document.getElementById("budget-tools-status");

        function setStatus(msg) {
            if (statusEl) statusEl.textContent = msg || "";
        }

        function allRowEls() {
            return rowsContainer ? Array.prototype.slice.call(rowsContainer.querySelectorAll("[data-category-row]")) : [];
        }

        function isFixed(row) {
            const checkbox = row.querySelector("[data-fixed-toggle]");
            return !!(checkbox && checkbox.checked);
        }

        function formatEuros(amount) {
            const sign = amount < 0 ? "-" : "";
            const fixed = Math.abs(amount).toFixed(2).replace(".", ",");
            const parts = fixed.split(",");
            parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ".");
            return sign + parts.join(",") + " €";
        }

        const otherAmountInput = form.querySelector('input[name="other_amount"]');
        const totalLiveAmountEl = document.getElementById("budget-total-live-amount");

        // Every category input plus Everything else. Blank/invalid fields
        // count as 0.
        function parseAmount(input) {
            if (!input) return 0;
            const value = parseFloat(input.value);
            return isFinite(value) ? value : 0;
        }

        function updateLiveTotal() {
            if (!totalLiveAmountEl) return;
            const rowsTotal = allRowEls().reduce(function (sum, row) {
                return sum + parseAmount(row.querySelector(".budget-default-input"));
            }, 0);
            const total = rowsTotal + parseAmount(otherAmountInput);
            totalLiveAmountEl.textContent = formatEuros(total);
        }

        updateLiveTotal();

        if (rowsContainer) {
            rowsContainer.addEventListener("input", function (event) {
                if (event.target && event.target.matches && event.target.matches(".budget-default-input")) {
                    updateLiveTotal();
                }
            });
        }

        if (otherAmountInput) {
            otherAmountInput.addEventListener("input", updateLiveTotal);
        }

        // Keep the row's visual "fixed" cue in sync while the checkbox is
        // toggled, before the form is even saved.
        if (rowsContainer) {
            rowsContainer.addEventListener("change", function (event) {
                const checkbox = event.target;
                if (!checkbox || !checkbox.matches || !checkbox.matches("[data-fixed-toggle]")) return;
                const row = checkbox.closest("[data-category-row]");
                if (row) row.classList.toggle("budget-default-row-fixed", checkbox.checked);
            });
        }

        if (distributeBtn) {
            distributeBtn.addEventListener("click", function () {
                const rawTotal = parseFloat((distributeTotal && distributeTotal.value) || "");
                if (!isFinite(rawTotal) || rawTotal <= 0) {
                    setStatus("Enter a total to distribute first.");
                    return;
                }
                const total = Math.round(rawTotal * 100) / 100;

                const allRows = allRowEls();
                const fixedRows = allRows.filter(isFixed);
                const fixedTotal = fixedRows.reduce(function (sum, row) {
                    const input = row.querySelector(".budget-default-input");
                    const value = parseFloat((input && input.value) || "0");
                    return sum + (isFinite(value) ? value : 0);
                }, 0);
                const remaining = Math.round((total - fixedTotal) * 100) / 100;

                if (remaining < 0) {
                    setStatus("Fixed amounts (" + formatEuros(fixedTotal) + ") exceed the total — nothing changed.");
                    return;
                }

                const eligibleRows = allRows.filter(function (row) { return !isFixed(row); });
                const withHistory = eligibleRows
                    .map(function (row) {
                        const avg = parseFloat(row.getAttribute("data-avg") || "0");
                        const input = row.querySelector(".budget-default-input");
                        return { avg: avg, input: input };
                    })
                    .filter(function (c) { return c.avg > 0 && c.input; });
                const withoutHistoryCount = eligibleRows.length - withHistory.length;

                if (remaining === 0) {
                    setStatus(fixedRows.length
                        ? "Fixed amounts already cover the whole total — nothing left to split."
                        : "Nothing to split — enter a larger total.");
                    return;
                }

                if (!withHistory.length) {
                    setStatus("No non-fixed categories have spending history to distribute by.");
                    return;
                }

                // Largest-remainder method, in whole cents: floor every
                // share first (never negative), then hand out the leftover
                // cents one at a time to the shares with the biggest
                // fractional remainder. This always sums to exactly the
                // total, unlike rounding each share to the nearest cent and
                // dumping the whole leftover/deficit onto the largest share,
                // which could push that share below zero.
                const avgSum = withHistory.reduce(function (sum, c) { return sum + c.avg; }, 0);
                const totalCents = Math.round(remaining * 100);
                let shares = withHistory.map(function (c) {
                    const rawCents = (c.avg / avgSum) * totalCents;
                    const flooredCents = Math.floor(rawCents);
                    return { input: c.input, cents: flooredCents, fraction: rawCents - flooredCents };
                });
                let leftoverCents = totalCents - shares.reduce(function (sum, s) { return sum + s.cents; }, 0);
                shares
                    .slice()
                    .sort(function (a, b) { return b.fraction - a.fraction; })
                    .slice(0, leftoverCents)
                    .forEach(function (s) { s.cents += 1; });
                shares.forEach(function (s) {
                    s.input.value = (s.cents / 100).toFixed(2);
                });
                updateLiveTotal();

                let message = "";
                if (fixedRows.length) {
                    message += "Kept " + fixedRows.length + " fixed categor" + (fixedRows.length === 1 ? "y" : "ies") +
                        " (" + formatEuros(fixedTotal) + "); split " + formatEuros(remaining) +
                        " across " + shares.length + " categor" + (shares.length === 1 ? "y" : "ies") + " — review and Save.";
                } else {
                    message += "Changed " + shares.length + " categor" + (shares.length === 1 ? "y" : "ies") +
                        " with spending history to split " + formatEuros(remaining) + " — review and Save.";
                }
                message += withoutHistoryCount > 0
                    ? " Other categories without history and Everything else were left as they are."
                    : " Everything else was left as it is.";
                setStatus(message);
            });
        }

        // "Copy from another month" — prefills the (still unsaved) form from
        // a month that has its own override amounts; nothing is written to
        // the server here, the user reviews and presses the existing Save.
        const copySnapshotsEl = document.getElementById("budget-month-snapshots");
        const copyModal = document.getElementById("budget-modal-copy");
        const copyMonthSelect = document.getElementById("budget-copy-month-select");
        const copyPreviewList = document.getElementById("budget-copy-preview-list");
        const copyPreviewTotal = document.getElementById("budget-copy-preview-total");
        const copyPrefillBtn = document.getElementById("budget-copy-prefill-btn");
        const manualDetails = document.getElementById("budget-manual");

        if (copySnapshotsEl && copyModal && copyMonthSelect) {
            let snapshots = [];
            try {
                snapshots = JSON.parse(copySnapshotsEl.textContent || "[]") || [];
            } catch (e) {
                snapshots = [];
            }

            if (snapshots.length) {
                const MONTH_NAMES = [
                    "January", "February", "March", "April", "May", "June",
                    "July", "August", "September", "October", "November", "December"
                ];

                function monthLabel(ym) {
                    const parts = (ym || "").split("-");
                    const monthIndex = parseInt(parts[1], 10) - 1;
                    const name = MONTH_NAMES[monthIndex];
                    return name ? name + " " + parts[0] : ym;
                }

                function findSnapshot(ym) {
                    return snapshots.filter(function (s) { return s.ym === ym; })[0] || snapshots[0];
                }

                snapshots.forEach(function (snap) {
                    const option = document.createElement("option");
                    option.value = snap.ym;
                    option.textContent = monthLabel(snap.ym);
                    copyMonthSelect.appendChild(option);
                });

                function categoryIdFor(input) {
                    return input ? input.name.replace(/^amount_/, "") : "";
                }

                function renderPreview() {
                    const snap = findSnapshot(copyMonthSelect.value);
                    if (!snap || !copyPreviewList) return;
                    copyPreviewList.innerHTML = "";
                    let total = 0;
                    allRowEls().forEach(function (row) {
                        const input = row.querySelector(".budget-default-input");
                        const categoryId = categoryIdFor(input);
                        const li = document.createElement("li");
                        const nameSpan = document.createElement("span");
                        nameSpan.textContent = row.getAttribute("data-name") || "";
                        const valueSpan = document.createElement("span");
                        valueSpan.className = "muted";
                        if (isFixed(row)) {
                            const current = parseAmount(input);
                            valueSpan.textContent = "kept (fixed) " + formatEuros(current);
                            total += current;
                        } else if (snap.amounts && Object.prototype.hasOwnProperty.call(snap.amounts, categoryId)) {
                            const amount = parseFloat(snap.amounts[categoryId]);
                            valueSpan.textContent = isFinite(amount) ? formatEuros(amount) : "unchanged";
                            valueSpan.className = "";
                            total += isFinite(amount) ? amount : parseAmount(input);
                        } else {
                            valueSpan.textContent = "unchanged";
                            total += parseAmount(input);
                        }
                        li.appendChild(nameSpan);
                        li.appendChild(valueSpan);
                        copyPreviewList.appendChild(li);
                    });
                    total += parseAmount(otherAmountInput);
                    if (copyPreviewTotal) {
                        copyPreviewTotal.textContent = "Total after prefill: " + formatEuros(total);
                    }
                }

                copyMonthSelect.addEventListener("change", renderPreview);
                renderPreview();

                if (copyPrefillBtn) {
                    copyPrefillBtn.addEventListener("click", function () {
                        const snap = findSnapshot(copyMonthSelect.value);
                        if (!snap) return;
                        allRowEls().forEach(function (row) {
                            if (isFixed(row)) return;
                            const input = row.querySelector(".budget-default-input");
                            const categoryId = categoryIdFor(input);
                            if (!input || !snap.amounts || !Object.prototype.hasOwnProperty.call(snap.amounts, categoryId)) return;
                            const amount = parseFloat(snap.amounts[categoryId]);
                            input.value = isFinite(amount) ? amount.toFixed(2) : "";
                        });
                        if (manualDetails) manualDetails.open = true;
                        updateLiveTotal();
                        // Reuse the shared modal-close handler (resets aria
                        // state, removes the body class) instead of
                        // duplicating it here.
                        const closeBtn = copyModal && copyModal.querySelector("[data-home-modal-close]");
                        if (closeBtn) closeBtn.click();
                        setStatus("Prefilled from " + monthLabel(snap.ym) + " — review and Save.");
                    });
                }
            }
        }
    })();

    (function setupBudgetOverrideToggles() {
        // The pencil next to each category row toggles its "change this
        // month" form. Runs unconditionally (not gated on the manual
        // budgets form existing) since the category list can render on its
        // own.
        function togglePanel(btn, panel, open) {
            panel.hidden = !open;
            if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
            if (open) {
                const input = panel.querySelector("input[type='number']");
                if (input) input.focus();
            }
        }

        document.addEventListener("click", function (event) {
            const btn = event.target.closest("[data-budget-edit-toggle]");
            if (!btn) return;
            const panel = document.getElementById(btn.getAttribute("aria-controls") || "");
            if (!panel) return;
            togglePanel(btn, panel, panel.hidden);
        });

        document.addEventListener("keydown", function (event) {
            if (event.key !== "Escape") return;
            const panel = event.target.closest && event.target.closest(".budget-override-panel");
            if (!panel || panel.hidden) return;
            const btn = document.querySelector('[data-budget-edit-toggle][aria-controls="' + panel.id + '"]');
            togglePanel(btn, panel, false);
            if (btn) btn.focus();
        });
    })();
})();

(function () {
    // Forms with a data-confirm attribute (e.g. actions that overwrite data)
    // ask for confirmation before submitting, instead of using inline JS.
    document.addEventListener("submit", function (event) {
        var form = event.target;
        var message = form && form.getAttribute && form.getAttribute("data-confirm");
        if (message && !window.confirm(message)) {
            event.preventDefault();
        }
    });
})();

(function () {
    var meta = document.querySelector('meta[name="csrf-token"]');
    var token = meta ? meta.getAttribute("content") : "";
    if (!token) {
        return;
    }
    function ensureToken(form) {
        if (!form || (form.method || "").toLowerCase() !== "post") {
            return;
        }
        var field = form.querySelector('input[name="_csrf_token"]');
        if (!field) {
            field = document.createElement("input");
            field.type = "hidden";
            field.name = "_csrf_token";
            form.appendChild(field);
        }
        field.value = token;
    }
    document.addEventListener(
        "submit",
        function (event) {
            ensureToken(event.target);
        },
        true
    );
    document.addEventListener("DOMContentLoaded", function () {
        var forms = document.querySelectorAll('form[method="post"], form[method="POST"]');
        for (var i = 0; i < forms.length; i++) {
            ensureToken(forms[i]);
        }
    });
})();

/* Top-bar dropdowns (Reports / Investments / Settings).
   CSS already opens these on hover and keyboard focus; this adds click, which
   is what touch and trackpad-tap users get, plus the usual dismissal rules.
   Panel switching itself is untouched — the existing .menu-item and
   .menu-sub-item handlers still do that. */
(function () {
    var groups = document.querySelectorAll(".menu-group");
    if (!groups.length) {
        return;
    }

    /* Decide which side each dropdown opens towards. Measured rather than
       assumed, because CSS opens these on hover too — there is no script
       running at that moment to fix an overhang after the fact. */
    function alignPanels() {
        if (window.innerWidth <= 900) {
            return; // chip row on mobile, not a popover
        }
        groups.forEach(function (group) {
            var panel = group.querySelector(".menu-sub-items");
            if (!panel) {
                return;
            }
            panel.classList.remove("align-right");
            var restore = panel.getAttribute("style") || "";
            panel.style.display = "grid";
            panel.style.visibility = "hidden";
            var width = panel.offsetWidth;
            panel.setAttribute("style", restore);
            if (group.getBoundingClientRect().left + width > document.documentElement.clientWidth - 8) {
                panel.classList.add("align-right");
            }
        });
    }

    var alignTimer = null;
    window.addEventListener("resize", function () {
        clearTimeout(alignTimer);
        alignTimer = setTimeout(alignPanels, 120);
    });
    alignPanels();

    var hoverTimer = null;
    // Set briefly after a selection. Closing moves focus back to the trigger,
    // which fires focusin and would otherwise reopen the menu immediately.
    var suppressUntil = 0;

    function closeAll(except) {
        groups.forEach(function (group) {
            if (group === except) {
                return;
            }
            group.classList.remove("is-open");
            var btn = group.querySelector(".menu-item");
            if (btn) {
                btn.setAttribute("aria-expanded", "false");
            }
        });
    }

    function open(group) {
        if (Date.now() < suppressUntil) {
            return;
        }
        closeAll(group);
        group.classList.add("is-open");
        var btn = group.querySelector(".menu-item");
        if (btn) {
            btn.setAttribute("aria-expanded", "true");
        }
    }

    groups.forEach(function (group) {
        var button = group.querySelector(".menu-item");
        var panel = group.querySelector(".menu-sub-items");
        if (!button || !panel) {
            return;
        }

        button.setAttribute("aria-haspopup", "true");
        button.setAttribute("aria-expanded", "false");

        // Open only — never toggle shut. Clicking the item also switches panel
        // (handled elsewhere), and closing the menu you just aimed at would
        // undo the point of clicking it.
        button.addEventListener("click", function () {
            open(group);
        });

        // Picking a section is the end of the interaction, so dismiss. Focus
        // goes back to the trigger rather than staying on a button that is
        // about to be hidden, and the suppression window stops that focus
        // change from reopening what we just closed.
        panel.addEventListener("click", function (event) {
            if (!event.target.closest(".menu-sub-item")) {
                return;
            }
            clearTimeout(hoverTimer);
            closeAll(null);
            suppressUntil = Date.now() + 400;
            button.focus();
        });

        // Keyboard equivalent of hover, now that :focus-within no longer opens
        // the menu in CSS.
        group.addEventListener("focusin", function () {
            open(group);
        });

        // Hover opens too, after a short delay so that sweeping the pointer
        // across the bar to reach a further item doesn't flash open every menu
        // it passes over. There is no mouseleave handler on purpose: moving
        // off the group must not close the menu, or it would be impossible to
        // reach the items inside it.
        group.addEventListener("mouseenter", function () {
            clearTimeout(hoverTimer);
            hoverTimer = setTimeout(function () {
                open(group);
            }, 110);
        });

        group.addEventListener("mouseleave", function () {
            clearTimeout(hoverTimer);
        });
    });

    document.addEventListener("click", function (event) {
        if (!event.target.closest(".menu-group")) {
            closeAll(null);
        }
    });

    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") {
            return;
        }
        var open = document.querySelector(".menu-group.is-open");
        if (!open) {
            return;
        }
        closeAll(null);
        var btn = open.querySelector(".menu-item");
        if (btn) {
            btn.focus();
        }
    });
})();

/* Report filters apply on change.
   Replaces an inline <script> that lived in the reports template and only knew
   about the bank section's two selects; this covers every section's pickers. */
(function () {
    document.querySelectorAll(".reports-autosubmit").forEach(function (select) {
        select.addEventListener("change", function () {
            var form = select.closest("form");
            if (!form) {
                return;
            }
            if (typeof form.requestSubmit === "function") {
                form.requestSubmit();
            } else {
                form.submit();
            }
        });
    });
})();
