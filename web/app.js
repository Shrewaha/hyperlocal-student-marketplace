/*
 * HyperLocal Student Marketplace -- browser and Android client.
 *
 * Talks to the same JSON API as the JavaFX desktop app, so neither knows the
 * other exists. No framework and no build step: this file is served as
 * written, which keeps it quick on a phone and a student's data plan, and
 * lets the APK bundle it unchanged.
 *
 * The token lives in sessionStorage, so closing the tab signs you out. Every
 * storage read and write is guarded, because storage throws outright in a
 * private window rather than politely returning nothing.
 */

(function () {
  "use strict";

  /* ------------------------------------------------------------ plumbing */

  var API_KEY = "marketplace.api";
  var TOKEN_KEY = "marketplace.token";

  function store(area, key, value) {
    try {
      if (value === null) area.removeItem(key);
      else if (value === undefined) return area.getItem(key);
      else area.setItem(key, value);
    } catch (e) {
      /* Private browsing, or site data blocked. The app still works; it just
         forgets things between loads. */
    }
    return null;
  }

  function apiBase() {
    var saved = store(localStorage, API_KEY);
    var base = saved || window.MARKETPLACE_API || "";
    return base.replace(/\/+$/, "");
  }

  var state = {
    user: null,
    token: store(sessionStorage, TOKEN_KEY) || null,
    categories: [],
    areas: [],
    unread: 0,
    thread: null,
    category: "", // active chip on the browse screen
    myAreaOnly: false,
    view: "browse",
  };

  var money = new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });

  function rupees(n) {
    if (n === null || n === undefined || n === "") return "—";
    return money.format(Number(n));
  }

  function $(sel) {
    return document.querySelector(sel);
  }

  function show(el, on) {
    if (el) el.hidden = !on;
  }

  function text(el, value) {
    if (el) el.textContent = value;
  }

  /* Built as elements, never as HTML strings: a shop name is not markup. */
  function el(tag, className, content) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = content;
    return node;
  }

  function icon(symbolId, cls) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    if (cls) svg.setAttribute("class", cls);
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#" + symbolId);
    svg.appendChild(use);
    return svg;
  }

  /* --------------------------------------------------------- categories */

  /*
   * Each category owns a colour and a glyph. With no product photography in
   * the API, this is what fills the space a photo would occupy -- a flat
   * saturated block reads as a decision, where a tinted grey rectangle reads
   * as an image that failed to load.
   */
  var CATEGORY_LOOK = {
    Electronics: { glyph: "g-electronics", color: "var(--cat-electronics)" },
    Stationery: { glyph: "g-stationery", color: "var(--cat-stationery)" },
    "Lab Components": { glyph: "g-lab", color: "var(--cat-lab)" },
    Books: { glyph: "g-books", color: "var(--cat-books)" },
    "Computer Parts": { glyph: "g-computer", color: "var(--cat-computer)" },
    Tools: { glyph: "g-tools", color: "var(--cat-tools)" },
    Other: { glyph: "g-other", color: "var(--cat-other)" },
  };

  function look(category) {
    return CATEGORY_LOOK[category] || CATEGORY_LOOK.Other;
  }

  function artBlock(category, className) {
    var l = look(category);
    var box = el("div", className);
    box.style.setProperty("--cat", l.color);
    box.appendChild(icon(l.glyph));
    return box;
  }

  /* --------------------------------------------------------------- fetch */

  var slowTimer = null;

  function request(path, options) {
    options = options || {};
    var base = apiBase();
    if (!base) return Promise.reject(new Error("No server address set."));

    var headers = { Accept: "application/json" };
    if (options.body) headers["Content-Type"] = "application/json";
    if (options.auth !== false && state.token) {
      headers.Authorization = "Bearer " + state.token;
    }

    /* A free-tier host sleeps when idle and takes most of a minute to wake.
       Without a word on screen that looks like the app is broken. */
    clearTimeout(slowTimer);
    slowTimer = setTimeout(function () {
      if (!$("#waking")) {
        var note = el(
          "div",
          "waking",
          "Waking the server… the free host sleeps when idle, so the first request can take a minute."
        );
        note.id = "waking";
        $("#main").prepend(note);
      }
    }, 2500);

    var method = options.method || "GET";

    /*
     * A sleeping instance drops the connection on the request that wakes it,
     * and anything in flight beside it. fetch rejects with a TypeError, which
     * on one try is indistinguishable from a real outage -- so a read is
     * tried again before giving up.
     *
     * Reads only. Retrying a POST could file the same offer twice, which is
     * worse than an error message.
     */
    var retries = method === "GET" ? 2 : 0;

    function attempt(left) {
      return fetch(base + path, {
        method: method,
        headers: headers,
        body: options.body ? JSON.stringify(options.body) : undefined,
      })
        .then(function (res) {
          return res
            .json()
            .catch(function () {
              return {};
            })
            .then(function (data) {
              if (!res.ok) {
                var detail = data && data.detail;
                if (Array.isArray(detail)) {
                  detail = detail
                    .map(function (d) {
                      return d.msg || "";
                    })
                    .join(", ");
                }
                throw new Error(detail || "Request failed (" + res.status + ")");
              }
              return data;
            });
        })
        .catch(function (err) {
          if (!(err instanceof TypeError)) throw err;
          if (left > 0) {
            return new Promise(function (resolve) {
              setTimeout(resolve, 1500);
            }).then(function () {
              return attempt(left - 1);
            });
          }
          throw new Error(
            "Could not reach the server at " + base + ". Check the address, and that it is running."
          );
        });
    }

    return attempt(retries).finally(function () {
      clearTimeout(slowTimer);
      var note = $("#waking");
      if (note) note.remove();
    });
  }

  /* ------------------------------------------------------------ the tabs */

  /*
   * Which tabs exist depends on who is signed in. A shop has no use for
   * "Post", and a signed-out visitor has nothing but browsing and signing in.
   */
  var TABS = {
    out: [
      { view: "browse", label: "Browse", glyph: "t-browse" },
      { view: "auth", label: "Sign in", glyph: "t-user" },
    ],
    student: [
      { view: "browse", label: "Browse", glyph: "t-browse" },
      { view: "post", label: "Post", glyph: "t-post" },
      { view: "mine", label: "Requests", glyph: "t-list" },
      { view: "chats", label: "Chats", glyph: "t-chat", badge: true },
      { view: "account", label: "Account", glyph: "t-user" },
    ],
    shop: [
      { view: "browse", label: "Browse", glyph: "t-browse" },
      { view: "requests", label: "Requests", glyph: "t-list" },
      { view: "offers-mine", label: "Offers", glyph: "t-post" },
      { view: "chats", label: "Chats", glyph: "t-chat", badge: true },
      { view: "account", label: "Account", glyph: "t-user" },
    ],
  };

  function tabSet() {
    if (!state.user) return TABS.out;
    return state.user.role === "shop" ? TABS.shop : TABS.student;
  }

  /* Screens reached from a tab rather than being one. The tab that leads
     here stays lit, so the bar never goes blank mid-journey. */
  var PARENT_OF = { compare: "mine", thread: "chats" };

  function paintTabs() {
    var nav = $("#tabs");
    nav.textContent = "";
    var active = PARENT_OF[state.view] || state.view;
    tabSet().forEach(function (t) {
      var b = el("button", "tab");
      b.type = "button";
      b.appendChild(icon(t.glyph));
      b.appendChild(el("span", null, t.label));
      if (t.badge && state.unread > 0) {
        b.appendChild(el("span", "tab__badge", state.unread > 9 ? "9+" : String(state.unread)));
      }
      if (active === t.view) b.setAttribute("aria-current", "page");
      b.addEventListener("click", function () {
        go(t.view);
      });
      nav.appendChild(b);
    });
  }

  /* ----------------------------------------------------------- navigation */

  var VIEWS = [
    "browse",
    "auth",
    "post",
    "mine",
    "requests",
    "offers-mine",
    "compare",
    "chats",
    "thread",
    "account",
  ];

  function go(name) {
    if (name === "home") name = state.user ? tabSet()[0].view : "browse";
    state.view = name;

    VIEWS.forEach(function (v) {
      show($("#view-" + v), v === name);
    });

    // Search and chips belong to browsing and would be noise anywhere else.
    show($("#browse-tools"), name === "browse");
    // The best-price bar belongs to the comparison screen alone.
    if (name !== "compare") show($("#bestbar"), false);

    paintTabs();
    window.scrollTo({ top: 0, behavior: "instant" });

    if (name === "browse") {
      paintBrowseIntro();
      loadProducts();
    }
    if (name === "mine") loadMyRequests();
    if (name === "requests") loadOpenRequests();
    if (name === "offers-mine") loadMyOffers().then(loadMyProducts).catch(function () {});
    if (name === "chats") loadThreads();
    if (name === "account") paintAccount();
    // Anywhere else, a quiet check so a reply that arrived meanwhile shows up.
    if (name !== "chats" && name !== "thread") refreshUnread();
  }

  /*
   * The browse screen speaks to whoever is reading it, and always offers the
   * one thing they can do next. Previously it told everybody to post and
   * showed no control for it: students had the Post tab, shops had no such
   * tab and no explanation, and a signed-out visitor had neither.
   */
  function paintBrowseIntro() {
    var cta = $("#browse-cta");
    var role = state.user && state.user.role;

    if (role === "shop") {
      text($("#browse-title"), "What students are buying.");
      text(
        $("#browse-sub"),
        "This is every shop's listings, yours included. Requests waiting for a price are under Requests."
      );
      text(cta, "See open requests");
      cta.onclick = function () {
        go("requests");
      };
      return;
    }

    text($("#browse-title"), "Shops come to you.");
    text(
      $("#browse-sub"),
      "Post what you need with your budget. Nearby shops reply with prices."
    );
    text(cta, "Post what you need");
    cta.onclick = function () {
      if (state.user) {
        go("post");
      } else {
        // Straight into signing up as a student, since that is who posts.
        openAuth(true);
      }
    };
  }

  function paintAccount() {
    if (!state.user) return;
    text($("#account-name"), state.user.shop_name || state.user.name);
    text(
      $("#account-meta"),
      [state.user.role === "shop" ? "Shop" : "Student", state.user.area, state.user.email]
        .filter(Boolean)
        .join(" · ")
    );
  }

  /* ------------------------------------------------------------ catalogue */

  function loadCategories() {
    return request("/api/categories", { auth: false })
      .then(function (data) {
        state.categories = data.categories || [];
        document.querySelectorAll("[data-categories]").forEach(function (sel) {
          sel.textContent = "";
          state.categories.forEach(function (c) {
            sel.appendChild(new Option(c, c));
          });
        });
        paintChips();
      })
      .catch(function () {
        /* Forms still submit; the server validates the category anyway. */
      });
  }

  function paintChips() {
    var box = $("#chips");
    box.textContent = "";
    var all = [""].concat(state.categories);
    all.forEach(function (c) {
      var b = el("button", "chip", c || "All");
      b.type = "button";
      b.setAttribute("aria-pressed", String(state.category === c));
      b.addEventListener("click", function () {
        state.category = c;
        paintChips();
        loadProducts();
      });
      box.appendChild(b);
    });
  }

  /* ---------------------------------------------------------------- areas */

  /*
   * Area decides which shops hear about a request, and it is typed by hand.
   * Someone who invents a spelling nobody else uses gets a marketplace of
   * one: their request reaches no shop, and that looks exactly like a request
   * nobody has answered yet. Suggesting the areas in use prevents it.
   */
  function loadAreas() {
    return request("/api/areas", { auth: false })
      .then(function (data) {
        state.areas = data.areas || [];
        var list = $("#known-areas");
        if (!list) return;
        list.textContent = "";
        state.areas.forEach(function (a) {
          var opt = document.createElement("option");
          opt.value = a.area;
          opt.label = a.shops === 1 ? "1 shop" : a.shops + " shops";
          list.appendChild(opt);
        });
      })
      .catch(function () {});
  }

  function areaKey(s) {
    return (s || "").trim().replace(/\s+/g, " ").toLowerCase();
  }

  function areaMatch(typed) {
    var key = areaKey(typed);
    if (!key) return null;
    for (var i = 0; i < state.areas.length; i++) {
      if (areaKey(state.areas[i].area) === key) return state.areas[i];
    }
    return null;
  }

  function paintAreaHint() {
    var hint = $("#area-hint");
    if (!hint) return;
    var form = $("#auth-form");
    var typed = form.elements.area ? form.elements.area.value : "";
    var role = form.elements.role ? form.elements.role.value : "student";

    hint.className = "hint-line";
    if (!registering || !typed.trim()) {
      hint.textContent = "";
      return;
    }

    var found = areaMatch(typed);
    if (!found) {
      hint.className = "hint-line hint-line--warn";
      hint.textContent =
        state.areas.length === 0
          ? "First one here. Shops that register with this exact name will see your requests."
          : "New area. Pick a suggested one if yours is listed, so you match the shops already there.";
      return;
    }

    if (role === "shop") {
      hint.className = "hint-line hint-line--ok";
      hint.textContent =
        found.people === 1 ? "1 person here already." : found.people + " people here already.";
      return;
    }

    if (found.shops === 0) {
      hint.className = "hint-line hint-line--warn";
      hint.textContent = "No shops here yet, so requests may go unanswered for now.";
    } else {
      hint.className = "hint-line hint-line--ok";
      hint.textContent =
        found.shops === 1
          ? "1 shop here will see your requests."
          : found.shops + " shops here will see your requests.";
    }
  }

  /* --------------------------------------------------------------- browse */

  /* An empty screen is a place to act, not just a notice. Where there is an
     obvious next step, it arrives as a button rather than as instructions
     pointing at a tab. */
  function emptyState(node, title, body, action) {
    node.textContent = "";
    node.appendChild(el("strong", null, title));
    node.appendChild(document.createTextNode(body));
    if (action) {
      var btn = el("button", "btn btn--primary btn--sm", action.label);
      btn.type = "button";
      btn.style.marginTop = "0.9rem";
      btn.addEventListener("click", action.onClick);
      node.appendChild(document.createElement("br"));
      node.appendChild(btn);
    }
    show(node, true);
  }

  function loadProducts() {
    var params = new URLSearchParams();
    var q = $("#search").elements.q.value.trim();
    if (q) params.set("q", q);
    if (state.category) params.set("category", state.category);

    var grid = $("#products");
    var empty = $("#products-empty");
    grid.textContent = "";
    show(empty, false);

    return request("/api/products?" + params.toString(), { auth: false })
      .then(function (data) {
        var items = data.products || [];
        if (!items.length) {
          var filtering = q || state.category;
          emptyState(
            empty,
            filtering ? "Nothing matches." : "Nothing listed yet.",
            filtering
              ? "No shop has listed this. Post it as a request and they will quote you a price."
              : "No shop has listed anything yet. You can still post what you need and wait for offers.",
            state.user && state.user.role === "shop"
              ? null
              : {
                  label: "Post what you need",
                  onClick: function () {
                    if (state.user) go("post");
                    else openAuth(true);
                  },
                }
          );
          return;
        }
        items.forEach(function (p) {
          var card = el("article", "tile");
          var art = artBlock(p.category, "tile__art");
          if (p.stock === 0) art.appendChild(el("span", "tile__badge", "OUT OF STOCK"));
          card.appendChild(art);

          var body = el("div", "tile__body");
          body.appendChild(el("h3", "tile__name", p.name));
          body.appendChild(
            el("span", "tile__meta", [p.shop_name || p.shop_contact, p.area].filter(Boolean).join(" · "))
          );
          var foot = el("div", "tile__foot");
          foot.appendChild(el("span", "price", rupees(p.price)));
          body.appendChild(foot);
          card.appendChild(body);
          grid.appendChild(card);
        });
      })
      .catch(function (err) {
        emptyState(empty, "Could not load listings.", err.message);
      });
  }

  /* ----------------------------------------------------------------- auth */

  var registering = false;

  /* Open the account screen in a known mode, so a button elsewhere can send
     someone to "create an account" rather than to a sign-in form they have
     no credentials for. */
  function openAuth(register) {
    registering = !!register;
    text($("#auth-msg"), "");
    paintAuthMode();
    go("auth");
  }

  function paintAuthMode() {
    text($("#auth-title"), registering ? "Create an account" : "Sign in");
    text($("#auth-go"), registering ? "Create account" : "Sign in");
    text($("#auth-switch"), registering ? "I already have an account" : "Create an account");
    show($("#reg-only"), registering);
    $("#auth-form").elements.password.autocomplete = registering
      ? "new-password"
      : "current-password";
    paintRoleFields();
    paintAreaHint();
  }

  function paintRoleFields() {
    var form = $("#auth-form");
    var role = form.elements.role ? form.elements.role.value : "student";
    show($("#shop-name-field"), registering && role === "shop");
  }

  function afterSignIn(data) {
    state.token = data.token;
    state.user = data.user;
    store(sessionStorage, TOKEN_KEY, data.token);
    go("home");
    refreshUnread(true);
  }

  function signIn(email, password) {
    return request("/api/login", {
      method: "POST",
      auth: false,
      body: { email: email, password: password },
    }).then(afterSignIn);
  }

  function restoreSession() {
    if (!state.token) return Promise.resolve();
    return request("/api/me")
      .then(function (user) {
        state.user = user;
      })
      .catch(function () {
        state.token = null;
        store(sessionStorage, TOKEN_KEY, null);
      });
  }

  /* -------------------------------------------------------------- student */

  function loadMyRequests() {
    var list = $("#my-requests");
    var empty = $("#my-requests-empty");
    list.textContent = "";
    show(empty, false);

    return request("/api/requests/mine")
      .then(function (data) {
        var items = data.requests || [];
        if (!items.length) {
          emptyState(
            empty,
            "No requests yet.",
            "Describe what you need and shops in your area are emailed straight away.",
            {
              label: "Post what you need",
              onClick: function () {
                go("post");
              },
            }
          );
          return;
        }
        items.forEach(function (r) {
          var li = el("li");
          li.appendChild(artBlock(r.category, "row__art"));

          var main = el("div", "row__main");
          main.appendChild(el("div", "row__name", r.title));
          main.appendChild(
            el(
              "div",
              "row__meta",
              [
                r.budget ? "budget " + rupees(r.budget) : null,
                r.offer_count + (r.offer_count === 1 ? " offer" : " offers"),
              ]
                .filter(Boolean)
                .join(" · ")
            )
          );

          var side = el("div", "row__side");
          if (r.best_price !== null && r.best_price !== undefined) {
            side.appendChild(el("span", "price price--best", rupees(r.best_price)));
          }
          var view = el("button", "btn btn--sm", r.offer_count ? "Compare" : "Waiting");
          view.type = "button";
          view.disabled = !r.offer_count;
          if (r.offer_count) view.className = "btn btn--sm btn--primary";
          view.addEventListener("click", function () {
            openOffers(r.id);
          });
          side.appendChild(view);

          li.appendChild(main);
          li.appendChild(side);
          list.appendChild(li);
        });
      })
      .catch(function (err) {
        emptyState(empty, "Could not load your requests.", err.message);
      });
  }

  /* ----------------------------------------------------------------- shop */

  function loadOpenRequests() {
    var list = $("#open-requests");
    var empty = $("#open-requests-empty");
    list.textContent = "";
    show(empty, false);

    return request("/api/requests/open?my_area_only=" + (state.myAreaOnly ? "true" : "false"))
      .then(function (data) {
        var items = data.requests || [];
        if (!items.length) {
          emptyState(
            empty,
            "Nothing open right now.",
            state.myAreaOnly
              ? "No student in your area has posted yet. Turn off the filter to see everything."
              : "When a student posts a request it shows up here."
          );
          return;
        }
        items.forEach(function (r) {
          var li = el("li");
          li.appendChild(artBlock(r.category, "row__art"));

          var main = el("div", "row__main");
          main.appendChild(el("div", "row__name", r.title));
          main.appendChild(
            el(
              "div",
              "row__meta",
              [
                r.student_name,
                r.student_area,
                r.specs,
                r.offer_count + (r.offer_count === 1 ? " offer" : " offers"),
              ]
                .filter(Boolean)
                .join(" · ")
            )
          );

          var side = el("div", "row__side");
          if (r.budget) side.appendChild(el("span", "price", rupees(r.budget)));
          var btn = el(
            "button",
            r.i_offered ? "btn btn--sm" : "btn btn--sm btn--primary",
            r.i_offered ? "Offer again" : "Send offer"
          );
          btn.type = "button";
          btn.addEventListener("click", function () {
            openOfferDialog(r);
          });
          side.appendChild(btn);

          li.appendChild(main);
          li.appendChild(side);
          list.appendChild(li);
        });
      })
      .catch(function (err) {
        emptyState(empty, "Could not load open requests.", err.message);
      });
  }

  function loadMyOffers() {
    var list = $("#my-offers");
    var empty = $("#my-offers-empty");
    list.textContent = "";
    show(empty, false);

    return request("/api/offers/mine")
      .then(function (data) {
        var items = data.offers || [];
        if (!items.length) {
          emptyState(
            empty,
            "No offers sent.",
            "Students are waiting for prices. Answer one and it shows up here.",
            {
              label: "See open requests",
              onClick: function () {
                go("requests");
              },
            }
          );
          return;
        }
        items.forEach(function (o) {
          var li = el("li");
          var main = el("div", "row__main");
          main.appendChild(el("div", "row__name", o.product_name));
          main.appendChild(
            el("div", "row__meta", "for “" + o.request_title + "”")
          );

          var side = el("div", "row__side");
          side.appendChild(
            el("span", o.is_best ? "price price--best" : "price", rupees(o.price))
          );
          if (o.is_best) {
            side.appendChild(el("span", "saving", "Cheapest"));
          } else if (o.best_price !== null && o.best_price !== undefined) {
            side.appendChild(
              el("span", "delta", "beaten by " + rupees(Number(o.price) - Number(o.best_price)))
            );
          }

          li.appendChild(main);
          li.appendChild(side);
          list.appendChild(li);
        });
      })
      .catch(function (err) {
        emptyState(empty, "Could not load your offers.", err.message);
      });
  }

  function loadMyProducts() {
    var grid = $("#my-products");
    grid.textContent = "";
    return request("/api/products/mine")
      .then(function (data) {
        (data.products || []).forEach(function (p) {
          var card = el("article", "tile");
          card.appendChild(artBlock(p.category, "tile__art"));
          var body = el("div", "tile__body");
          body.appendChild(el("h3", "tile__name", p.name));
          body.appendChild(el("span", "tile__meta", "stock " + p.stock));
          var foot = el("div", "tile__foot");
          foot.appendChild(el("span", "price", rupees(p.price)));
          body.appendChild(foot);
          card.appendChild(body);
          grid.appendChild(card);
        });
      })
      .catch(function () {});
  }

  /* ----------------------------------------------- the comparison screen */

  function openOffers(requestId) {
    go("compare");

    var list = $("#offers");
    var empty = $("#offers-empty");
    var summary = $("#offers-summary");
    var slot = $("#best-slot");
    list.textContent = "";
    summary.textContent = "";
    slot.textContent = "";
    show(empty, false);
    show($("#bestbar"), false);

    return request("/api/requests/" + requestId + "/offers")
      .then(function (data) {
        text($("#offers-title"), data.request.title);
        text(
          $("#offers-sub"),
          [data.request.category, data.request.specs].filter(Boolean).join(" · ")
        );

        var under =
          data.request.budget && data.best_price !== null
            ? Number(data.request.budget) - Number(data.best_price)
            : null;

        [
          ["Offers", String(data.offer_count)],
          ["Budget", rupees(data.request.budget)],
          [under !== null && under >= 0 ? "You save" : "Over budget", under === null ? "—" : rupees(Math.abs(under))],
        ].forEach(function (pair) {
          var box = el("div");
          box.appendChild(el("dt", null, pair[0]));
          box.appendChild(el("dd", null, pair[1]));
          summary.appendChild(box);
        });

        var offers = data.offers || [];
        if (!offers.length) {
          emptyState(
            empty,
            "No offers yet.",
            "Shops in your area have been emailed about this request."
          );
          return;
        }

        /* The cheapest offer gets a card of its own above the list -- it is
           the answer the student opened this screen for. */
        var best = offers[0];
        var card = el("div", "best-card");
        card.appendChild(el("span", "best-card__tag", "CHEAPEST"));
        card.appendChild(el("div", "best-card__shop", best.shop_name));
        card.appendChild(
          el(
            "div",
            "best-card__meta",
            [best.product_name, best.area, best.specs, best.note].filter(Boolean).join(" · ")
          )
        );
        var price = el("div", "best-card__price");
        price.appendChild(el("span", "price price--lg price--best", rupees(best.price)));
        if (under !== null && under >= 0) {
          price.appendChild(el("span", "saving", rupees(under) + " under budget"));
        }
        card.appendChild(price);
        card.appendChild(contactRow(best, requestId));
        slot.appendChild(card);

        offers.slice(1).forEach(function (o) {
          var li = el("li");
          var main = el("div", "row__main");
          main.appendChild(el("div", "row__name", o.shop_name));
          main.appendChild(
            el(
              "div",
              "row__meta",
              [o.product_name, o.area, o.specs, o.note].filter(Boolean).join(" · ")
            )
          );
          main.appendChild(contactRow(o, requestId));
          var side = el("div", "row__side");
          side.appendChild(el("span", "price", rupees(o.price)));
          side.appendChild(el("span", "delta", "+" + rupees(o.price_diff_vs_best)));
          li.appendChild(main);
          li.appendChild(side);
          list.appendChild(li);
        });

        text($("#bestbar-shop"), best.shop_name);
        text($("#bestbar-price"), rupees(best.price));
        show($("#bestbar"), true);
      })
      .catch(function (err) {
        emptyState(empty, "Could not load the offers.", err.message);
      });
  }

  /* -------------------------------------------------------------- contact */

  /*
   * Three ways to reach a shop, in the order people actually use them here.
   * WhatsApp and a phone call are how a student and a local shop settle a
   * price; the in-app thread is for when neither is answering, and email is
   * the fallback nobody asked for but somebody will want.
   */
  function contactRow(offer, requestId) {
    var box = el("div", "contact");

    if (offer.phone) {
      var wa = el("a", "btn btn--primary", "WhatsApp");
      /* wa.me wants digits only, no plus. */
      wa.href = "https://wa.me/" + offer.phone.replace(/\D/g, "");
      wa.target = "_blank";
      wa.rel = "noopener";
      box.appendChild(wa);

      var call = el("a", "btn", "Call");
      call.href = "tel:" + offer.phone.replace(/[^\d+]/g, "");
      box.appendChild(call);
    }

    var chat = el("button", "btn", "Message");
    chat.type = "button";
    chat.addEventListener("click", function () {
      openThread(requestId, offer.shop_id, offer.shop_name);
    });
    box.appendChild(chat);

    if (offer.email) {
      var mail = el("a", "btn", "Email");
      mail.href = "mailto:" + offer.email;
      box.appendChild(mail);
    }

    return box;
  }

  /* ------------------------------------------------------------- messages */

  var lastUnreadCheck = 0;

  function refreshUnread(force) {
    if (!state.user) {
      state.unread = 0;
      return Promise.resolve();
    }
    var now = Date.now();
    if (!force && now - lastUnreadCheck < 15000) return Promise.resolve();
    lastUnreadCheck = now;
    return request("/api/messages/threads")
      .then(function (data) {
        state.unread = data.unread_total || 0;
        paintTabs();
      })
      .catch(function () {});
  }

  function loadThreads() {
    var list = $("#threads");
    var empty = $("#threads-empty");
    list.textContent = "";
    show(empty, false);

    return request("/api/messages/threads")
      .then(function (data) {
        state.unread = data.unread_total || 0;
        paintTabs();

        var items = data.threads || [];
        if (!items.length) {
          emptyState(
            empty,
            "No messages yet.",
            state.user && state.user.role === "shop"
              ? "A student can start a conversation from one of your offers."
              : "Open a request you have offers on and message a shop from there."
          );
          return;
        }
        items.forEach(function (t) {
          var li = el("li");
          var main = el("div", "row__main");
          main.appendChild(el("div", "row__name", t.with_name));
          main.appendChild(
            el("div", "row__meta", t.title + " · " + (t.last_body || ""))
          );
          var side = el("div", "row__side");
          if (t.unread > 0) side.appendChild(el("span", "count", String(t.unread)));
          var open = el("button", "btn btn--sm btn--primary", "Open");
          open.type = "button";
          open.addEventListener("click", function () {
            openThread(t.request_id, t.shop_id, t.with_name);
          });
          side.appendChild(open);
          li.appendChild(main);
          li.appendChild(side);
          list.appendChild(li);
        });
      })
      .catch(function (err) {
        emptyState(empty, "Could not load messages.", err.message);
      });
  }

  function openThread(requestId, shopId, withName) {
    state.thread = { request_id: requestId, shop_id: shopId, with_name: withName };
    go("thread");
    return loadThread();
  }

  function loadThread() {
    if (!state.thread) return Promise.resolve();
    var box = $("#thread-messages");
    box.textContent = "";

    return request(
      "/api/messages/thread?request_id=" +
        state.thread.request_id +
        "&shop_id=" +
        state.thread.shop_id
    )
      .then(function (data) {
        text($("#thread-title"), state.thread.with_name || "Conversation");
        text(
          $("#thread-sub"),
          [data.title, data.budget ? "budget " + rupees(data.budget) : null]
            .filter(Boolean)
            .join(" · ")
        );

        if (!data.messages.length) {
          box.appendChild(
            el("p", "empty", "No messages yet. Say what you need and they will see it here.")
          );
        }
        data.messages.forEach(function (m) {
          var bubble = el("div", m.mine ? "bubble bubble--mine" : "bubble", m.body);
          bubble.appendChild(el("span", "bubble__when", m.mine ? "You" : m.sender_name));
          box.appendChild(bubble);
        });
        box.scrollIntoView({ block: "end" });
        /* Reading it cleared the unread count server-side; reflect that. */
        return refreshUnread(true);
      })
      .catch(function (err) {
        box.appendChild(el("p", "empty", err.message));
      });
  }

  /* -------------------------------------------------------------- sheets */

  var offerTarget = null;

  function openOfferDialog(req) {
    offerTarget = req;
    var form = $("#offer-form");
    form.reset();
    text($("#offer-for"), req.title + (req.budget ? " · budget " + rupees(req.budget) : ""));
    form.elements.product_name.value = req.title;
    $("#offer-dialog").showModal();
  }

  function openApiDialog() {
    $("#api-form").elements.api.value = apiBase();
    $("#api-dialog").showModal();
  }

  /* ------------------------------------------------------------- wiring */

  document.addEventListener("click", function (e) {
    var nav = e.target.closest("[data-nav]");
    if (nav) {
      e.preventDefault();
      go(nav.getAttribute("data-nav"));
    }
  });

  $("#compare-back").addEventListener("click", function () {
    go("home");
  });

  $("#nav-out").addEventListener("click", function () {
    request("/api/logout", { method: "POST" }).catch(function () {});
    state.token = null;
    state.user = null;
    state.unread = 0;
    state.thread = null;
    store(sessionStorage, TOKEN_KEY, null);
    go("browse");
  });

  $("#nav-api").addEventListener("click", openApiDialog);
  $("#no-api-set").addEventListener("click", openApiDialog);

  $("#api-dialog").addEventListener("close", function () {
    var dlg = $("#api-dialog");
    if (dlg.returnValue === "cancel") return;
    if (dlg.returnValue === "clear") store(localStorage, API_KEY, null);
    else {
      var value = $("#api-form").elements.api.value.trim().replace(/\/+$/, "");
      if (!value) return;
      store(localStorage, API_KEY, value);
    }
    window.location.reload();
  });

  $("#search").addEventListener("submit", function (e) {
    e.preventDefault();
    loadProducts();
  });

  /* Search as you type, but not on every keystroke. */
  var searchTimer = null;
  $("#search").addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadProducts, 350);
  });

  $("#auth-switch").addEventListener("click", function () {
    registering = !registering;
    text($("#auth-msg"), "");
    paintAuthMode();
  });

  $("#auth-form").addEventListener("change", function (e) {
    if (e.target.name === "role") {
      paintRoleFields();
      paintAreaHint();
    }
  });

  $("#auth-form").addEventListener("input", function (e) {
    if (e.target.name === "area") paintAreaHint();
  });

  $("#auth-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var f = e.target.elements;
    var msg = $("#auth-msg");
    msg.className = "msg";
    text(msg, "Working…");

    var email = f.email.value.trim();
    var password = f.password.value;

    var failed = function (err) {
      msg.className = "msg msg--bad";
      text(msg, err.message);
    };
    var done = function () {
      text(msg, "");
    };

    if (!registering) {
      signIn(email, password).then(done, failed);
      return;
    }

    var role = f.role.value;
    request("/api/register", {
      method: "POST",
      auth: false,
      body: {
        name: f.name.value.trim(),
        email: email,
        password: password,
        role: role,
        area: f.area.value.trim() || null,
        shop_name: role === "shop" ? f.shop_name.value.trim() : null,
        phone: role === "shop" ? f.phone.value.trim() || null : null,
      },
    })
      .then(function () {
        return signIn(email, password);
      })
      .then(done, failed);
  });

  $("#request-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var f = e.target.elements;
    var msg = $("#request-msg");
    msg.className = "msg";
    text(msg, "Posting…");

    request("/api/requests", {
      method: "POST",
      body: {
        title: f.title.value.trim(),
        category: f.category.value,
        description: f.description.value.trim() || null,
        specs: f.specs.value.trim() || null,
        budget: f.budget.value ? Number(f.budget.value) : null,
      },
    }).then(
      function (data) {
        e.target.reset();
        var n = data && data.shops_notified;
        var where = (data && data.area) || "your area";
        if (n === 0) {
          /* Not an error -- the request is saved and any shop browsing can
             still answer it. But saying so beats an empty offers list that
             never explains itself. */
          msg.className = "msg msg--warn";
          text(
            msg,
            "Posted, but no shops have registered in " +
              where +
              " yet. It is still visible to every shop browsing open requests."
          );
        } else {
          msg.className = "msg msg--ok";
          text(
            msg,
            n === 1
              ? "Posted. 1 shop in " + where + " has been emailed."
              : "Posted. " + n + " shops in " + where + " have been emailed."
          );
        }
      },
      function (err) {
        msg.className = "msg msg--bad";
        text(msg, err.message);
      }
    );
  });

  $("#product-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var f = e.target.elements;
    var msg = $("#product-msg");
    msg.className = "msg";
    text(msg, "Adding…");

    request("/api/products", {
      method: "POST",
      body: {
        name: f.name.value.trim(),
        category: f.category.value,
        specs: f.specs.value.trim() || null,
        price: Number(f.price.value),
        stock: Number(f.stock.value || 1),
      },
    }).then(
      function () {
        e.target.reset();
        msg.className = "msg msg--ok";
        text(msg, "Listed.");
        loadMyProducts();
      },
      function (err) {
        msg.className = "msg msg--bad";
        text(msg, err.message);
      }
    );
  });

  $("#offer-dialog").addEventListener("close", function () {
    var dlg = $("#offer-dialog");
    if (dlg.returnValue !== "send" || !offerTarget) return;
    var f = $("#offer-form").elements;

    request("/api/offers", {
      method: "POST",
      body: {
        request_id: offerTarget.id,
        product_name: f.product_name.value.trim(),
        specs: f.specs.value.trim() || null,
        price: Number(f.price.value),
        note: f.note.value.trim() || null,
      },
    }).then(
      function () {
        loadOpenRequests().catch(function () {});
      },
      function (err) {
        window.alert(err.message);
      }
    );
  });

  $("#thread-back").addEventListener("click", function () {
    go("chats");
  });

  $("#thread-form").addEventListener("submit", function (e) {
    e.preventDefault();
    if (!state.thread) return;
    var input = e.target.elements.body;
    var body = input.value.trim();
    if (!body) return;
    input.value = "";

    request("/api/messages", {
      method: "POST",
      body: {
        request_id: state.thread.request_id,
        shop_id: state.thread.shop_id,
        body: body,
      },
    }).then(loadThread, function (err) {
      // Put the text back rather than losing what they typed.
      input.value = body;
      window.alert(err.message);
    });
  });

  $("#area-toggle").addEventListener("click", function () {
    state.myAreaOnly = !state.myAreaOnly;
    this.setAttribute("aria-pressed", String(state.myAreaOnly));
    loadOpenRequests();
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refreshUnread(true);
  });

  /* ----------------------------------------------------------------- boot */

  if (!apiBase()) {
    show($("#no-api"), true);
    paintAuthMode();
    paintBrowseIntro();
    paintTabs();
    return;
  }

  paintAuthMode();
  loadCategories()
    .then(loadAreas)
    .then(restoreSession)
    .then(function () {
      go(state.user ? "home" : "browse");
      return refreshUnread(true);
    });
})();
