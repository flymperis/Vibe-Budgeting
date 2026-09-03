(function () {
    const root = document.documentElement;
    const themeToggle = document.getElementById("theme-dark-toggle");
    // Side menu (desktop), "More" sheet entries, and the bottom tab bar
    // (mobile) all drive the same panels, so they share one handler and stay
    // in sync via data-target.
    const menuButtons = document.querySelectorAll(".menu-item, .bottom-nav-item[data-target]");
    const panels = document.querySelectorAll(".panel");
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
                "panel-summary": "summary",
                "panel-yearly": "yearly",
                "panel-reports": "reports",
                "panel-investments": "investments",
                "panel-settings": "settings"
            };
            const panelValue = panelMap[target];
            if (panelValue) {
                syncShellUrl(panelValue);
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

    (function setupHomeQuickModals() {
        const modalConfigs = [
            { modal: document.getElementById("home-modal-expense"), button: document.getElementById("home-btn-expense") },
            { modal: document.getElementById("home-modal-income"), button: document.getElementById("home-btn-income") },
            { modal: document.getElementById("recurring-modal-add"), button: document.getElementById("recurring-btn-add") },
        ].filter(function (entry) {
            return entry.modal && entry.button;
        });
        if (!modalConfigs.length) {
            return;
        }

        function focusFirstField(modal) {
            const field = modal.querySelector(
                "textarea, input:not([type='hidden']):not([type='checkbox']), select, button[type='submit']"
            );
            if (field) {
                field.focus();
            }
        }

        function closeAll() {
            modalConfigs.forEach(function (entry) {
                entry.modal.hidden = true;
                entry.modal.setAttribute("aria-hidden", "true");
                entry.button.setAttribute("aria-expanded", "false");
                entry.button.classList.remove("is-active");
            });
            document.body.classList.remove("home-modal-open");
        }

        function openModal(modal, button) {
            closeAll();
            modal.hidden = false;
            modal.setAttribute("aria-hidden", "false");
            button.setAttribute("aria-expanded", "true");
            button.classList.add("is-active");
            document.body.classList.add("home-modal-open");
            focusFirstField(modal);
        }

        modalConfigs.forEach(function (entry) {
            entry.button.addEventListener("click", function () {
                if (entry.modal.hidden) {
                    openModal(entry.modal, entry.button);
                } else {
                    closeAll();
                }
            });
        });

        document.querySelectorAll("[data-home-modal-close]").forEach(function (el) {
            el.addEventListener("click", closeAll);
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && document.body.classList.contains("home-modal-open")) {
                closeAll();
            }
        });
    })();

    (function setupMoreSheet() {
        const sheet = document.getElementById("more-sheet");
        const trigger = document.getElementById("more-nav-btn");
        if (!sheet || !trigger) {
            return;
        }

        const MORE_PANELS = [
            "panel-recurring",
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
            const first = sheet.querySelector(".menu-item");
            if (first) {
                first.focus();
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
