/*
 * HyperLocal Student Marketplace -- browser and Android client.
 *
 * Talks to the same JSON API as the JavaFX desktop app, so neither knows the
 * other exists. No framework and no build step: this file is served as
 * written, which keeps it quick on a phone and a student's data plan, and
 * lets the APK bundle it unchanged.
 *
 * The sign-in token is kept in localStorage so that reopening the app finds
 * you still signed in. sessionStorage would be wiped whenever Android
 * reclaims the WebView's process, and because the tabs are built from the
 * signed-in role, that looked like Chats and Post had gone missing rather
 * than like a logout. Every storage read and write is guarded, because
 * storage throws outright in a private window rather than returning nothing.
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
    token: store(localStorage, TOKEN_KEY) || null,
    categories: [],
    areas: [],
    unread: 0,
    alerts: 0,
    avatar: null,
    cameFrom: "browse",
    thread: null,
    location: null,
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

  /* ------------------------------------------------------------ language */

  /*
   * The shops this is for are counters in Ayodhya, where Devanagari is read
   * far more easily than English. A form you are guessing the meaning of is
   * one you fill in slowly and wrongly, so this is not decoration -- it is
   * whether a shopkeeper lists thirty items or gives up after two.
   */
  var LANG_KEY = "marketplace.lang";

  function lang() {
    var saved = store(localStorage, LANG_KEY);
    return saved === "hi" ? "hi" : "en";
  }

  function tr(key, vars) {
    var table = (window.STRINGS && window.STRINGS[lang()]) || {};
    var fallback = (window.STRINGS && window.STRINGS.en) || {};
    var out = table[key] || fallback[key] || key;
    if (vars) {
      Object.keys(vars).forEach(function (k) {
        out = out.replace("{" + k + "}", vars[k]);
      });
    }
    return out;
  }

  function categoryLabel(c) {
    if (lang() === "hi" && window.CATEGORY_HI && window.CATEGORY_HI[c]) {
      return window.CATEGORY_HI[c];
    }
    return c;
  }

  /* Anything carrying data-t is translated in place, so the markup stays
     readable and the table stays the single source of wording. */
  function paintLanguage() {
    document.querySelectorAll("[data-t]").forEach(function (node) {
      node.textContent = tr(node.getAttribute("data-t"));
    });
    document.documentElement.lang = lang();
    document.querySelectorAll("#lang-pick [data-lang]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-lang") === lang()));
    });
    var name = $("#item-name");
    if (name) name.placeholder = tr("add.name.ph");
  }

  /* -------------------------------------------------------------- avatars */

  /*
   * Someone's picture, or their initial on the brand colour when they have
   * none. A grey silhouette reads as a missing thing; a letter reads as a
   * person who simply has not uploaded one.
   */
  function avatar(user, big) {
    var box = el("span", "avatar" + (big ? " avatar--lg" : ""));
    var name = (user && (user.shop_name || user.name)) || "";
    box.textContent = name.trim().charAt(0) || "?";

    var id = user && (user.id || user.shop_id);
    if (user && user.has_avatar && id) {
      var img = document.createElement("img");
      img.src = apiBase() + "/api/users/" + id + "/avatar";
      img.alt = "";
      img.loading = "lazy";
      img.addEventListener("error", function () {
        img.remove(); /* leaves the initial behind */
      });
      box.appendChild(img);
    }
    return box;
  }

  /* --------------------------------------------------------------- toasts */

  /*
   * Confirmations used to be a line of text under whichever form produced
   * them, which on a phone is frequently below the fold -- people pressed
   * the button, saw nothing move, and pressed it again. A toast is seen.
   */
  function toast(message, tone, ms) {
    var box = $("#toasts");
    if (!box) return;
    var node = el("div", "toast" + (tone ? " toast--" + tone : ""), message);
    box.appendChild(node);
    setTimeout(function () {
      node.remove();
    }, ms || 4000);
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

  function artBlock(category, className, product) {
    var box = el("div", className);

    /* A real photograph takes the place the category block would have had,
       and fills it the same way, so a card with a picture and one without
       are the same shape and the grid stays even. */
    if (product && product.has_image) {
      var img = document.createElement("img");
      img.src = apiBase() + "/api/products/" + product.id + "/image";
      img.alt = "";
      img.loading = "lazy";
      /* A photo that will not load should leave the category block behind
         rather than a broken-image icon. */
      img.addEventListener("error", function () {
        box.textContent = "";
        var l = look(category);
        box.style.setProperty("--cat", l.color);
        box.appendChild(icon(l.glyph));
      });
      box.appendChild(img);
      return box;
    }

    var l = look(category);
    box.style.setProperty("--cat", l.color);
    box.appendChild(icon(l.glyph));
    return box;
  }

  /*
   * Shrink a picture before it is uploaded.
   *
   * A phone camera produces three or four megabytes. Sending that over a
   * student's data plan to a free host, so it can be shown in a card two
   * hundred pixels wide, is wasteful at both ends -- and slow enough that
   * people assume the upload has failed. The longest edge is capped and the
   * result re-encoded as JPEG, which brings it to roughly a tenth.
   */
  function shrinkImage(file, maxEdge, quality) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        URL.revokeObjectURL(url);
        var scale = Math.min(1, maxEdge / Math.max(img.width, img.height));
        var w = Math.round(img.width * scale);
        var h = Math.round(img.height * scale);
        var canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        try {
          resolve(canvas.toDataURL("image/jpeg", quality));
        } catch (e) {
          reject(new Error("That image could not be read."));
        }
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        reject(new Error("That file is not an image this browser can open."));
      };
      img.src = url;
    });
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
      { view: "browse", label: "nav.browse", glyph: "t-browse" },
      { view: "auth", label: "nav.signin", glyph: "t-user" },
    ],
    student: [
      { view: "browse", label: "nav.browse", glyph: "t-browse" },
      { view: "post", label: "nav.post", glyph: "t-post" },
      { view: "mine", label: "nav.requests", glyph: "t-list" },
      { view: "chats", label: "nav.chats", glyph: "t-chat", badge: true },
      { view: "account", label: "nav.account", glyph: "t-user" },
    ],
    shop: [
      { view: "requests", label: "nav.requests", glyph: "t-list" },
      { view: "catalogue", label: "nav.catalogue", glyph: "t-post" },
      { view: "offers-mine", label: "nav.offers", glyph: "t-browse" },
      { view: "chats", label: "nav.chats", glyph: "t-chat", badge: true },
      { view: "account", label: "nav.account", glyph: "t-user" },
    ],
  };

  function tabSet() {
    if (!state.user) return TABS.out;
    return state.user.role === "shop" ? TABS.shop : TABS.student;
  }

  /* Screens reached from a tab rather than being one. The tab that leads
     here stays lit, so the bar never goes blank mid-journey. */
  var PARENT_OF = {
    compare: "mine",
    thread: "chats",
    shop: "browse",
    owner: "account",
  };

  function paintTabs() {
    var nav = $("#tabs");
    nav.textContent = "";
    var active = PARENT_OF[state.view] || state.view;
    tabSet().forEach(function (t) {
      var b = el("button", "tab");
      b.type = "button";
      b.appendChild(icon(t.glyph));
      b.appendChild(el("span", null, tr(t.label)));
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
    "alerts",
    "shop",
    "catalogue",
    "owner",
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
    if (name === "offers-mine") loadMyOffers();
    if (name === "catalogue") loadMyProducts();
    if (name === "owner") loadOwnerUsers();
    if (name === "chats") loadThreads();
    if (name === "alerts") loadAlerts();
    if (name === "account") paintAccount();
    // Anywhere else, a quiet check so anything that arrived meanwhile shows up.
    if (name !== "chats" && name !== "thread") refreshUnread();
    if (name !== "alerts") refreshAlerts();
    show($("#bell"), !!state.user);
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
    var u = state.user;
    var isShop = u.role === "shop";

    text($("#account-name"), u.shop_name || u.name);
    paintAvatarPreview();
    text(
      $("#account-meta"),
      [isShop ? "Shop" : "Student", u.area, u.email].filter(Boolean).join(" · ")
    );

    /* Address, phone, about and a location are a shop's shopfront: they tell
       a student where to go and who they are dealing with. A student needs
       none of them to post a request, so asking would be noise. */
    show($("#profile-shop-name"), isShop);
    show($("#profile-phone"), isShop);
    show($("#profile-address"), isShop);
    show($("#profile-about"), isShop);
    show($("#profile-location"), isShop);
    show($("#nav-owner"), !!u.is_admin);

    var f = $("#profile-form").elements;
    f.name.value = u.name || "";
    f.area.value = u.area || "";
    if (isShop) {
      f.shop_name.value = u.shop_name || "";
      f.phone.value = u.phone || "";
      f.address.value = u.address || "";
      f.about.value = u.about || "";
    }

    state.location =
      u.lat !== null && u.lat !== undefined && u.lng !== null && u.lng !== undefined
        ? { lat: u.lat, lng: u.lng }
        : null;
    paintLocation();
  }

  function paintAvatarPreview() {
    var slot = $("#avatar-preview");
    if (!slot || !state.user) return;
    var shown = state.avatar
      ? { shop_name: state.user.shop_name, name: state.user.name, has_avatar: false }
      : state.user;
    var fresh = avatar(shown, true);
    fresh.id = "avatar-preview";
    /* A picture just chosen has not been uploaded yet, so it is shown from
       the data URL rather than from a URL the server does not serve. */
    if (state.avatar) {
      var img = document.createElement("img");
      img.src = state.avatar;
      img.alt = "";
      fresh.appendChild(img);
    }
    slot.replaceWith(fresh);
    show($("#avatar-clear"), !!(state.avatar || state.user.has_avatar));
  }

  function paintLocation() {
    var hint = $("#location-hint");
    var link = $("#map-link");
    var clear = $("#clear-location");

    if (!state.location) {
      hint.className = "hint-line";
      hint.textContent = "Not set. Students will see your address only.";
      show(link, false);
      show(clear, false);
      return;
    }

    var ll = state.location.lat.toFixed(5) + ", " + state.location.lng.toFixed(5);
    hint.className = "hint-line hint-line--ok";
    hint.textContent = "Saved pin at " + ll;
    link.href = "https://www.google.com/maps/search/?api=1&query=" +
      encodeURIComponent(state.location.lat + "," + state.location.lng);
    show(link, true);
    show(clear, true);
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
          grid.appendChild(productCard(p));
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
    store(localStorage, TOKEN_KEY, data.token);
    go("home");
    refreshUnread(true);
    refreshAlerts(true);
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
        store(localStorage, TOKEN_KEY, null);
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

  /* ------------------------------------------------------- the catalogue */

  var editing = null;          /* the listing being changed, or null to add */
  var itemPhoto = null;        /* a chosen photo, as a data URL */
  var lastCategory = null;     /* shops add like things in a row */

  function openItemSheet(product) {
    editing = product || null;
    itemPhoto = null;
    scannedCode = (product && product.barcode) || null;
    stopScanning();
    text($("#scan-hint"), "");

    var f = $("#item-form");
    text($("#item-msg"), "");
    text($("#item-title"), tr(editing ? "add.edit" : "add.title"));
    show($("#item-delete"), !!editing);
    $("#item-more").open = false;

    f.elements.name.value = editing ? editing.name : "";
    f.elements.price.value = editing ? editing.price : "";
    f.elements.specs.value = editing && editing.specs ? editing.specs : "";
    f.elements.stock.value = editing ? editing.stock : 1;

    paintItemPhoto(editing && editing.has_image
      ? apiBase() + "/api/products/" + editing.id + "/image"
      : null);

    paintCategoryPicker(
      editing ? editing.category : lastCategory || state.categories[0]
    );

    $("#item-dialog").showModal();
    /* The name is what they came to type; put them in it. */
    setTimeout(function () {
      f.elements.name.focus();
    }, 50);
  }

  /* --------------------------------------------------------------- scan */

  /*
   * Reading a barcode with the browser's own detector.
   *
   * BarcodeDetector ships in Chromium, which is what the Android WebView is,
   * so on the phones this is built for there is nothing to download. Where it
   * is missing -- desktop Firefox and Safari -- the button says so and typing
   * carries on working, rather than the page pretending to scan.
   */
  var scanStream = null;
  var scanTimer = null;
  var scannedCode = null;

  function scanSupported() {
    return typeof window.BarcodeDetector === "function";
  }

  function stopScanning() {
    clearInterval(scanTimer);
    scanTimer = null;
    if (scanStream) {
      scanStream.getTracks().forEach(function (track) {
        track.stop();
      });
      scanStream = null;
    }
    var video = $("#scan-video");
    if (video) video.srcObject = null;
    show($("#scanner"), false);
  }

  function startScanning() {
    var hint = $("#scan-hint");
    hint.className = "hint-line";

    if (!scanSupported()) {
      hint.className = "hint-line hint-line--warn";
      hint.textContent = tr("scan.none");
      return;
    }

    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "environment" } })
      .then(function (stream) {
        scanStream = stream;
        var video = $("#scan-video");
        video.srcObject = stream;
        video.play();
        show($("#scanner"), true);
        hint.textContent = tr("scan.looking");

        var detector;
        try {
          detector = new window.BarcodeDetector({
            formats: ["ean_13", "ean_8", "upc_a", "upc_e", "code_128", "code_39"],
          });
        } catch (e) {
          /* On Android the detector is backed by Play Services, so the
             constructor can throw on a device without them even though the
             class itself exists. Checking for the class is not enough. */
          stopScanning();
          hint.className = "hint-line hint-line--warn";
          hint.textContent = tr("scan.none");
          return;
        }

        scanTimer = setInterval(function () {
          detector
            .detect(video)
            .then(function (found) {
              if (!found.length) return;
              var code = found[0].rawValue;
              stopScanning();
              resolveBarcode(code);
            })
            .catch(function () {
              /* A frame that cannot be read is ordinary; the next one is
                 along in a moment. */
            });
        }, 400);
      })
      .catch(function () {
        hint.className = "hint-line hint-line--warn";
        hint.textContent = tr("scan.denied");
      });
  }

  function resolveBarcode(code) {
    var hint = $("#scan-hint");
    scannedCode = code;
    hint.className = "hint-line";
    hint.textContent = "\u2026";

    request("/api/barcode/" + encodeURIComponent(code)).then(
      function (data) {
        var f = $("#item-form").elements;
        if (data.found) {
          f.name.value = data.name;
          if (data.specs && !f.specs.value) f.specs.value = data.specs;
          if (data.category) paintCategoryPicker(data.category);
          hint.className = "hint-line hint-line--ok";
          hint.textContent = tr("scan.known", { name: data.name });
          /* The name is settled; the price is what is left. */
          f.price.focus();
        } else {
          hint.className = "hint-line hint-line--warn";
          hint.textContent = tr("scan.new");
          f.name.focus();
        }
      },
      function (err) {
        hint.className = "hint-line hint-line--warn";
        hint.textContent = err.message;
      }
    );
  }

  function paintItemPhoto(src) {
    var slot = $("#item-photo-slot");
    slot.textContent = src ? "" : "+";
    if (src) {
      var img = document.createElement("img");
      img.src = src;
      img.alt = "";
      slot.appendChild(img);
    }
    text($("#item-photo-label"), tr(src ? "add.retake" : "add.photo"));
  }

  var chosenCategory = null;

  function paintCategoryPicker(selected) {
    chosenCategory = selected || state.categories[0] || "Other";
    var box = $("#item-categories");
    box.textContent = "";
    state.categories.forEach(function (c) {
      var b = el("button", "pick");
      b.type = "button";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(c === chosenCategory));
      b.appendChild(artBlock(c, "pick__art"));
      b.appendChild(el("span", "pick__label", categoryLabel(c)));
      b.addEventListener("click", function () {
        paintCategoryPicker(c);
      });
      box.appendChild(b);
    });
  }

  function saveItem(keepOpen) {
    var f = $("#item-form").elements;
    var msg = $("#item-msg");
    var name = f.name.value.trim();
    var price = f.price.value;

    /* Only two things are required, and each says which is missing rather
       than failing silently on a form validation nobody can see. */
    msg.className = "msg msg--bad";
    if (!name) {
      text(msg, tr("add.needName"));
      f.name.focus();
      return Promise.resolve(false);
    }
    if (price === "" || Number(price) < 0) {
      text(msg, tr("add.needPrice"));
      f.price.focus();
      return Promise.resolve(false);
    }
    text(msg, "");

    var body = {
      name: name,
      category: chosenCategory,
      price: Number(price),
      stock: Number(f.stock.value || 1),
      specs: f.specs.value.trim() || null,
    };
    /* Saving with a code is what teaches the marketplace this name. */
    if (scannedCode) body.barcode = scannedCode;
    if (itemPhoto !== null) body.image = itemPhoto;

    var call = editing
      ? request("/api/products/" + editing.id, { method: "PATCH", body: body })
      : request("/api/products", { method: "POST", body: body });

    return call.then(
      function () {
        lastCategory = chosenCategory;
        toast(tr("add.saved"), "ok");
        loadMyProducts();
        if (keepOpen) {
          /* Cleared and left open, with the category kept: a shop listing
             thirty things is listing thirty similar things. */
          editing = null;
          itemPhoto = null;
          scannedCode = null;
          text($("#scan-hint"), "");
          var g = $("#item-form").elements;
          g.name.value = "";
          g.price.value = "";
          g.specs.value = "";
          g.stock.value = 1;
          paintItemPhoto(null);
          show($("#item-delete"), false);
          text($("#item-title"), tr("add.title"));
          g.name.focus();
        }
        return true;
      },
      function (err) {
        msg.className = "msg msg--bad";
        text(msg, err.message);
        toast(err.message, "bad", 6000);
        return false;
      }
    );
  }

  function deleteItem() {
    if (!editing) return;
    if (!window.confirm(tr("add.deleteSure"))) return;
    var id = editing.id;
    request("/api/products/" + id, { method: "DELETE" }).then(
      function () {
        toast(tr("add.deleted"), "ok");
        $("#item-dialog").close("done");
        loadMyProducts();
      },
      function (err) {
        toast(err.message, "bad", 6000);
      }
    );
  }

  function loadMyProducts() {
    var grid = $("#my-products");
    var empty = $("#my-products-empty");
    grid.textContent = "";
    show(empty, false);

    return request("/api/products/mine")
      .then(function (data) {
        var items = data.products || [];
        text(
          $("#cat-count"),
          items.length === 1
            ? tr("cat.count.one")
            : items.length
            ? tr("cat.count.many", { n: items.length })
            : ""
        );

        if (!items.length) {
          emptyState(empty, tr("cat.empty.title"), tr("cat.empty.body"), {
            label: tr("cat.add"),
            onClick: function () {
              openItemSheet(null);
            },
          });
          return;
        }

        items.forEach(function (p) {
          var card = el("article", "tile");
          card.appendChild(artBlock(p.category, "tile__art", p));
          var body = el("div", "tile__body");
          body.appendChild(el("h3", "tile__name", p.name));
          body.appendChild(el("span", "tile__meta", categoryLabel(p.category)));
          var foot = el("div", "tile__foot");
          foot.appendChild(el("span", "price", rupees(p.price)));
          body.appendChild(foot);

          /* Tapping the card edits it; a shopkeeper correcting a price
             should not have to find a menu. */
          var manage = el("div", "tile__manage");
          var edit = el("button", "btn btn--sm btn--primary", tr("edit.button"));
          edit.type = "button";
          edit.addEventListener("click", function () {
            openItemSheet(p);
          });
          manage.appendChild(edit);
          body.appendChild(manage);

          card.appendChild(body);
          grid.appendChild(card);
        });
      })
      .catch(function (err) {
        emptyState(empty, "Could not load your items.", err.message);
      });
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
        var bestWho = el("div", "best-card__shop");
        var bestLink = el("button", "shoplink", best.shop_name);
        bestLink.type = "button";
        bestLink.addEventListener("click", function () {
          openShop(best.shop_id);
        });
        bestWho.appendChild(bestLink);
        card.appendChild(bestWho);
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
          var nameRow = el("div", "row__name");
          var link = el("button", "shoplink", o.shop_name);
          link.type = "button";
          link.addEventListener("click", function () {
            openShop(o.shop_id);
          });
          nameRow.appendChild(link);
          main.appendChild(nameRow);
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
  /*
   * One implementation, used by an offer and by a shop's own page. The chat
   * button needs a request to belong to, so it only appears where there is
   * one -- a shop page reached from browsing has no conversation to open.
   */
  function contactRowFor(info) {
    var box = el("div", "contact");

    if (info.phone) {
      var wa = el("a", "btn btn--primary", "WhatsApp");
      /* wa.me wants digits only, no plus. */
      wa.href = "https://wa.me/" + info.phone.replace(/\D/g, "");
      wa.target = "_blank";
      wa.rel = "noopener";
      box.appendChild(wa);

      var call = el("a", "btn", "Call");
      call.href = "tel:" + info.phone.replace(/[^\d+]/g, "");
      box.appendChild(call);
    }

    if (info.request_id) {
      var chat = el("button", "btn", "Message");
      chat.type = "button";
      chat.addEventListener("click", function () {
        openThread(info.request_id, info.shop_id, info.shop_name);
      });
      box.appendChild(chat);
    }

    if (info.email) {
      var mail = el("a", "btn", "Email");
      mail.href = "mailto:" + info.email;
      box.appendChild(mail);
    }

    /* Where to actually go, when the shop has said. */
    if (info.lat !== null && info.lat !== undefined &&
        info.lng !== null && info.lng !== undefined) {
      var map = el("a", "btn", "Directions");
      map.href = "https://www.google.com/maps/search/?api=1&query=" +
        encodeURIComponent(info.lat + "," + info.lng);
      map.target = "_blank";
      map.rel = "noopener";
      box.appendChild(map);
    }

    return box;
  }

  function contactRow(offer, requestId) {
    return contactRowFor({
      shop_id: offer.shop_id,
      shop_name: offer.shop_name,
      phone: offer.phone,
      email: offer.email,
      lat: offer.lat,
      lng: offer.lng,
      request_id: requestId,
    });
  }

  /*
   * One product card, shared by browsing and by a shop's page.
   *
   * "Request this" is the point: a student looking at a listed price should
   * be able to ask every shop nearby to beat it without retyping what the
   * thing is called.
   */
  function productCard(p, shopNameOverride) {
    var card = el("article", "tile");
    var art = artBlock(p.category, "tile__art", p);
    if (p.stock === 0) art.appendChild(el("span", "tile__badge", "OUT OF STOCK"));
    card.appendChild(art);

    var body = el("div", "tile__body");
    body.appendChild(el("h3", "tile__name", p.name));

    var shopName = shopNameOverride || p.shop_name || p.shop_contact;
    if (shopNameOverride) {
      body.appendChild(el("span", "tile__meta", p.specs || ""));
    } else {
      var who = el("button", "tile__meta shoplink", shopName);
      who.type = "button";
      who.addEventListener("click", function () {
        openShop(p.shop_id);
      });
      body.appendChild(who);
    }

    var foot = el("div", "tile__foot");
    foot.appendChild(el("span", "price", rupees(p.price)));
    body.appendChild(foot);

    var ask = el("button", "btn btn--sm btn--primary btn--block", "Request this");
    ask.type = "button";
    ask.style.marginTop = "0.5rem";
    ask.addEventListener("click", function () {
      startRequestFor(p);
    });
    body.appendChild(ask);

    card.appendChild(body);
    return card;
  }

  /*
   * Carry a listing into the post form. Signed out, that means signing up
   * first -- so the listing is remembered and filled in on the other side,
   * rather than lost to the detour.
   */
  var pendingRequest = null;

  function startRequestFor(p) {
    pendingRequest = { title: p.name, category: p.category, specs: p.specs || "" };
    if (!state.user) {
      toast("Create an account and we will carry this over.", null, 5000);
      openAuth(true);
      return;
    }
    if (state.user.role === "shop") {
      toast("Shops answer requests rather than posting them.", "warn", 5000);
      return;
    }
    go("post");
    fillPendingRequest();
  }

  function fillPendingRequest() {
    if (!pendingRequest) return;
    var f = $("#request-form").elements;
    f.title.value = pendingRequest.title;
    if (pendingRequest.category) f.category.value = pendingRequest.category;
    if (pendingRequest.specs) f.specs.value = pendingRequest.specs;
    pendingRequest = null;
    toast("Filled in from that listing. Add your budget.", null, 5000);
  }

  /* ------------------------------------------------------------ shop page */

  /*
   * A shop's public page. Reachable without an account, because the moment a
   * student most wants to know who is quoting them is exactly the moment a
   * sign-in wall would send them away.
   */
  /* ------------------------------------------------------------- reviews */

  function starsText(n) {
    var full = Math.round(n || 0);
    return "\u2605\u2605\u2605\u2605\u2605".slice(0, full) +
           "\u2606\u2606\u2606\u2606\u2606".slice(0, 5 - full);
  }

  var myRating = 0;

  function paintStarPicker() {
    var box = $("#rate-pick");
    box.textContent = "";
    for (var i = 1; i <= 5; i++) {
      (function (value) {
        var b = el("button", "star", value <= myRating ? "\u2605" : "\u2606");
        b.type = "button";
        b.setAttribute("role", "radio");
        b.setAttribute("aria-checked", String(value === myRating));
        b.setAttribute("aria-label", value + " out of 5");
        if (value <= myRating) b.classList.add("star--on");
        b.addEventListener("click", function () {
          myRating = value;
          paintStarPicker();
        });
        box.appendChild(b);
      })(i);
    }
  }

  function loadReviews(shopId) {
    var list = $("#shop-reviews");
    var empty = $("#shop-reviews-empty");
    list.textContent = "";
    show(empty, false);

    return request("/api/shops/" + shopId + "/reviews", { auth: false })
      .then(function (data) {
        var box = $("#shop-rating");
        box.textContent = "";
        if (data.count) {
          var r = el("span", "rating");
          r.appendChild(el("span", "rating__stars", starsText(data.average)));
          r.appendChild(el("span", null, String(data.average)));
          r.appendChild(
            el("span", "rating__count",
               data.count === 1 ? "(1)" : "(" + data.count + ")")
          );
          box.appendChild(r);
        } else {
          box.textContent = "No reviews yet";
        }

        var items = data.reviews || [];
        if (!items.length) {
          emptyState(empty, "No reviews yet.",
            "A student can review a shop once it has sent them an offer.");
          return;
        }
        items.forEach(function (v) {
          var li = el("li");
          var main = el("div", "row__main");
          main.appendChild(el("div", "rating__stars", starsText(v.rating)));
          main.appendChild(el("div", "row__name", v.student_name));
          if (v.body) main.appendChild(el("div", "row__meta", v.body));
          li.appendChild(main);
          list.appendChild(li);
        });
      })
      .catch(function () {});
  }

  function openShop(shopId) {
    state.cameFrom = state.view === "shop" ? state.cameFrom : state.view;
    go("shop");

    var grid = $("#shop-products");
    var empty = $("#shop-products-empty");
    var stats = $("#shop-stats");
    var contact = $("#shop-contact");
    grid.textContent = "";
    stats.textContent = "";
    contact.textContent = "";
    show(empty, false);
    /* Cleared first: opening one shop after another that fails to load would
       otherwise leave the previous shop's name above the new one's error. */
    text($("#shop-name"), "Loading\u2026");
    text($("#shop-where"), "");
    show($("#shop-about"), false);
    text($("#shop-count"), "");

    return request("/api/shops/" + shopId, { auth: false })
      .then(function (data) {
        var shop = data.shop;

        var head = $("#shop-avatar");
        head.replaceWith(avatar({ id: shop.id, shop_name: shop.name, has_avatar: shop.has_avatar }, true));
        /* replaceWith drops the id, so put it back for the next visit. */
        document.querySelector(".shopfront .avatar").id = "shop-avatar";

        text($("#shop-name"), shop.name);
        text(
          $("#shop-where"),
          [shop.area, shop.address].filter(Boolean).join(" · ") || "No address given"
        );

        if (shop.about) {
          text($("#shop-about"), shop.about);
          show($("#shop-about"), true);
        } else {
          show($("#shop-about"), false);
        }

        [
          ["Listings", String(data.products.length)],
          ["Offers made", String(data.offers)],
          ["Times cheapest", String(data.cheapest)],
        ].forEach(function (pair) {
          var box = el("div");
          box.appendChild(el("dt", null, pair[0]));
          box.appendChild(el("dd", null, pair[1]));
          stats.appendChild(box);
        });

        /* The same ways in as an offer carries, built from the same fields. */
        contact.appendChild(
          contactRowFor({
            shop_id: shop.id,
            phone: shop.phone,
            lat: shop.lat,
            lng: shop.lng,
          })
        );

        /* Only a student sees the form, and the server decides whether they
           have actually dealt with this shop -- the button is not the
           permission, it is only the invitation. */
        myRating = 0;
        $("#rate-body").value = "";
        text($("#rate-msg"), "");
        paintStarPicker();
        show($("#shop-review-form"), !!(state.user && state.user.role === "student"));
        $("#rate-save").onclick = function () {
          postReview(shop.id);
        };
        loadReviews(shop.id);

        text($("#shop-count"), data.products.length ? "Cheapest first" : "");
        if (!data.products.length) {
          emptyState(empty, "Nothing listed yet.", "This shop answers requests but has not added a catalogue.");
          return;
        }
        data.products.forEach(function (p) {
          grid.appendChild(productCard(p, shop.name));
        });
      })
      .catch(function (err) {
        text($("#shop-name"), "Shop unavailable");
        emptyState(empty, "Could not load that shop.", err.message);
      });
  }

  function postReview(shopId) {
    var msg = $("#rate-msg");
    msg.className = "msg";
    if (!myRating) {
      msg.className = "msg msg--bad";
      text(msg, "Pick a rating first.");
      return;
    }
    text(msg, "Posting\u2026");

    request("/api/shops/" + shopId + "/reviews", {
      method: "POST",
      body: { rating: myRating, body: $("#rate-body").value.trim() || null },
    }).then(
      function () {
        text(msg, "");
        $("#rate-body").value = "";
        toast("Review posted.", "ok");
        loadReviews(shopId);
      },
      function (err) {
        msg.className = "msg msg--bad";
        text(msg, err.message);
      }
    );
  }

  /* ---------------------------------------------------------------- owner */

  /*
   * Only reachable by an account whose email is in the server's ADMIN_EMAILS.
   * The button below is hidden for everyone else, but that is courtesy: the
   * endpoints answer 404 to anyone who is not an owner, so hiding it is not
   * what keeps it shut.
   */
  var ownerFilter = { q: "", role: "" };

  function loadOwnerUsers() {
    var list = $("#owner-users");
    var empty = $("#owner-empty");
    list.textContent = "";
    show(empty, false);

    var params = new URLSearchParams();
    if (ownerFilter.q) params.set("q", ownerFilter.q);
    if (ownerFilter.role) params.set("role", ownerFilter.role);

    return request("/api/admin/users?" + params.toString())
      .then(function (data) {
        var items = data.users || [];
        if (!items.length) {
          emptyState(empty, "Nobody here.", "No account matches that.");
          return;
        }
        items.forEach(function (u) {
          var li = el("li", u.blocked ? "row--blocked" : null);
          li.appendChild(avatar(u));

          var main = el("div", "row__main");
          var nameRow = el("div", "row__name");
          nameRow.appendChild(document.createTextNode(u.shop_name || u.name));
          if (u.blocked) nameRow.appendChild(el("span", "badge-off", " SUSPENDED"));
          if (u.is_admin) nameRow.appendChild(el("span", "badge-owner", " OWNER"));
          main.appendChild(nameRow);
          main.appendChild(
            el("div", "row__meta",
               [u.email, u.area,
                u.role === "shop"
                  ? u.listings + " listed \u00b7 " + u.offers + " offers"
                  : u.requests + " requests"
               ].filter(Boolean).join(" \u00b7 "))
          );
          li.appendChild(main);

          var side = el("div", "row__side");
          if (!u.is_admin) {
            var toggle = el("button", "btn btn--sm", u.blocked ? "Unblock" : "Block");
            toggle.type = "button";
            toggle.addEventListener("click", function () {
              request(
                "/api/admin/users/" + u.id + (u.blocked ? "/unblock" : "/block"),
                { method: "POST" }
              ).then(
                function () {
                  toast(u.blocked ? "Account restored." : "Account suspended.", "ok");
                  loadOwnerUsers();
                },
                function (err) {
                  toast(err.message, "bad", 6000);
                }
              );
            });
            side.appendChild(toggle);

            var del = el("button", "btn btn--sm btn--ghost btn--danger", "Delete");
            del.type = "button";
            del.addEventListener("click", function () {
              /* Spelled out, because this takes their listings, offers and
                 messages with it and cannot be undone. */
              if (!window.confirm(
                    "Delete " + (u.shop_name || u.name) +
                    " and everything they have posted? This cannot be undone.")) {
                return;
              }
              request("/api/admin/users/" + u.id, { method: "DELETE" }).then(
                function () {
                  toast("Account deleted.", "ok");
                  loadOwnerUsers();
                },
                function (err) {
                  toast(err.message, "bad", 6000);
                }
              );
            });
            side.appendChild(del);
          }
          li.appendChild(side);
          list.appendChild(li);
        });
      })
      .catch(function (err) {
        emptyState(empty, "Could not load accounts.", err.message);
      });
  }

  /* --------------------------------------------------------------- alerts */

  var lastAlertCheck = 0;

  function refreshAlerts(force) {
    if (!state.user) {
      state.alerts = 0;
      show($("#bell-dot"), false);
      return Promise.resolve();
    }
    var now = Date.now();
    if (!force && now - lastAlertCheck < 15000) return Promise.resolve();
    lastAlertCheck = now;

    return request("/api/notifications?limit=1")
      .then(function (data) {
        state.alerts = data.unread || 0;
        show($("#bell-dot"), state.alerts > 0);
      })
      .catch(function () {});
  }

  var ALERT_LABEL = {
    offer: "OFFER",
    message: "MESSAGE",
    request: "REQUEST",
  };

  function loadAlerts() {
    var list = $("#alerts");
    var empty = $("#alerts-empty");
    list.textContent = "";
    show(empty, false);

    return request("/api/notifications")
      .then(function (data) {
        var items = data.notifications || [];
        if (!items.length) {
          emptyState(
            empty,
            "Nothing yet.",
            state.user && state.user.role === "shop"
              ? "You will hear when a student in your area posts a request."
              : "You will hear when a shop answers one of your requests."
          );
        } else {
          items.forEach(function (n) {
            var li = el("li", n.read ? null : "alert--unread");
            var main = el("div", "row__main");
            main.appendChild(el("div", "alert__kind", ALERT_LABEL[n.kind] || "UPDATE"));
            main.appendChild(el("div", "row__name", n.title));
            if (n.body) main.appendChild(el("div", "row__meta", n.body));

            /* Each one leads where it came from, so the feed is a way in
               rather than only a record. */
            if (n.kind === "message" && n.request_id && n.shop_id) {
              var open = el("button", "btn btn--sm", "Open chat");
              open.type = "button";
              open.addEventListener("click", function () {
                openThread(n.request_id, n.shop_id, n.title);
              });
              var side = el("div", "row__side");
              side.appendChild(open);
              li.appendChild(main);
              li.appendChild(side);
            } else if (n.kind === "offer" && n.request_id) {
              var cmp = el("button", "btn btn--sm btn--primary", "Compare");
              cmp.type = "button";
              cmp.addEventListener("click", function () {
                openOffers(n.request_id);
              });
              var side2 = el("div", "row__side");
              side2.appendChild(cmp);
              li.appendChild(main);
              li.appendChild(side2);
            } else {
              li.appendChild(main);
            }
            list.appendChild(li);
          });
        }

        /* Opening the feed is the act of reading it. */
        if (data.unread > 0) {
          return request("/api/notifications/read", { method: "POST" }).then(function () {
            state.alerts = 0;
            show($("#bell-dot"), false);
          });
        }
        state.alerts = 0;
        show($("#bell-dot"), false);
      })
      .catch(function (err) {
        emptyState(empty, "Could not load notifications.", err.message);
      });
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
    state.alerts = 0;
    state.thread = null;
    state.location = null;
    state.avatar = null;
    store(localStorage, TOKEN_KEY, null);
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
      signIn(email, password).then(function () {
        done();
        if (pendingRequest && state.user && state.user.role === "student") {
          go("post");
          fillPendingRequest();
        }
      }, failed);
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
      .then(function () {
        done();
        /* They came here from a listing; take them the rest of the way. */
        if (pendingRequest && state.user && state.user.role === "student") {
          go("post");
          fillPendingRequest();
        }
      }, failed);
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
          toast("Posted, but no shops are registered in " + where + " yet.", "warn", 6000);
          msg.className = "msg msg--warn";
          text(
            msg,
            "Posted, but no shops have registered in " +
              where +
              " yet. It is still visible to every shop browsing open requests."
          );
        } else {
          toast(
            n === 1
              ? "Posted. 1 shop in " + where + " was notified."
              : "Posted. " + n + " shops in " + where + " were notified.",
            "ok"
          );
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

  var pendingImage = null;

  $("#nav-owner").addEventListener("click", function () {
    go("owner");
  });

  var ownerSearchTimer = null;
  $("#owner-search").addEventListener("input", function (e) {
    ownerFilter.q = e.target.value.trim();
    clearTimeout(ownerSearchTimer);
    ownerSearchTimer = setTimeout(loadOwnerUsers, 350);
  });

  $("#owner-search").addEventListener("submit", function (e) {
    e.preventDefault();
    loadOwnerUsers();
  });

  document.querySelectorAll("#owner-filter [data-role]").forEach(function (b) {
    b.addEventListener("click", function () {
      ownerFilter.role = b.getAttribute("data-role");
      document.querySelectorAll("#owner-filter [data-role]").forEach(function (o) {
        o.setAttribute("aria-pressed", String(o === b));
      });
      loadOwnerUsers();
    });
  });

  $("#owner-add").addEventListener("click", function () {
    $("#shop-form").reset();
    text($("#shop-form-msg"), "");
    $("#shop-dialog").showModal();
  });

  $("#shop-dialog").addEventListener("close", function () {
    if ($("#shop-dialog").returnValue !== "save") return;
    var f = $("#shop-form").elements;
    request("/api/admin/shops", {
      method: "POST",
      body: {
        name: f.name.value.trim(),
        email: f.email.value.trim(),
        password: f.password.value,
        shop_name: f.shop_name.value.trim(),
        area: f.area.value.trim() || null,
        phone: f.phone.value.trim() || null,
      },
    }).then(
      function () {
        toast("Shop added. Give them the email and password.", "ok", 6000);
        loadOwnerUsers();
      },
      function (err) {
        toast(err.message, "bad", 6000);
      }
    );
  });

  $("#cat-add").addEventListener("click", function () {
    openItemSheet(null);
  });

  $("#item-scan").addEventListener("click", function () {
    if ($("#scanner").hidden) startScanning();
    else stopScanning();
  });

  $("#scan-stop").addEventListener("click", stopScanning);

  /* The camera must not keep running behind a closed sheet. */
  $("#item-dialog").addEventListener("close", stopScanning);

  $("#item-photo-btn").addEventListener("click", function () {
    $("#item-photo").click();
  });

  $("#item-photo").addEventListener("change", function (e) {
    var file = e.target.files && e.target.files[0];
    if (!file) return;
    text($("#item-msg"), "");
    shrinkImage(file, 900, 0.72).then(
      function (dataUrl) {
        itemPhoto = dataUrl;
        paintItemPhoto(dataUrl);
      },
      function (err) {
        toast(err.message, "bad", 6000);
      }
    );
  });

  /*
   * The two save buttons submit the form, which would close the sheet before
   * the request finished -- and "save and add another" must not close it at
   * all. So both are handled here and the dialog is closed only on success.
   */
  $("#item-save").addEventListener("click", function (e) {
    e.preventDefault();
    saveItem(false).then(function (ok) {
      if (ok) $("#item-dialog").close("done");
    });
  });

  $("#item-save-more").addEventListener("click", function (e) {
    e.preventDefault();
    saveItem(true);
  });

  $("#item-cancel").addEventListener("click", function (e) {
    e.preventDefault();
    $("#item-dialog").close("cancel");
  });

  $("#item-delete").addEventListener("click", function (e) {
    e.preventDefault();
    deleteItem();
  });

  document.querySelectorAll("#lang-pick [data-lang]").forEach(function (b) {
    b.addEventListener("click", function () {
      store(localStorage, LANG_KEY, b.getAttribute("data-lang"));
      paintLanguage();
      paintTabs();
      /* Anything already drawn carries the old wording. */
      if (state.view === "catalogue") loadMyProducts();
    });
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
        toast("Offer sent to the student.", "ok");
        loadOpenRequests().catch(function () {});
      },
      function (err) {
        toast(err.message, "bad", 6000);
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
      toast(err.message, "bad", 6000);
    });
  });

  $("#shop-back").addEventListener("click", function () {
    go(state.cameFrom || "browse");
  });

  $("#avatar-input").addEventListener("change", function (e) {
    var hint = $("#avatar-hint");
    var file = e.target.files && e.target.files[0];
    hint.className = "hint-line";
    hint.textContent = "";
    if (!file) return;

    hint.textContent = "Preparing\u2026";
    /* Square-ish and small: it is shown at 36px in a row and 72px on a
       profile, so anything larger is bytes nobody sees. */
    shrinkImage(file, 400, 0.78).then(
      function (dataUrl) {
        state.avatar = dataUrl;
        paintAvatarPreview();
        hint.className = "hint-line hint-line--ok";
        hint.textContent = "Ready. Save the profile to keep it.";
      },
      function (err) {
        hint.className = "hint-line hint-line--warn";
        hint.textContent = err.message;
      }
    );
  });

  $("#avatar-clear").addEventListener("click", function () {
    state.avatar = "";          /* empty string means remove, not untouched */
    var slot = $("#avatar-preview");
    var fresh = avatar({ shop_name: state.user.shop_name, name: state.user.name }, true);
    fresh.id = "avatar-preview";
    slot.replaceWith(fresh);
    show($("#avatar-clear"), false);
    text($("#avatar-hint"), "Will be removed when you save.");
  });

  $("#bell").addEventListener("click", function () {
    go("alerts");
  });

  $("#locate").addEventListener("click", function () {
    var hint = $("#location-hint");
    if (!navigator.geolocation) {
      hint.className = "hint-line hint-line--warn";
      hint.textContent = "This device cannot report a location.";
      return;
    }
    hint.className = "hint-line";
    hint.textContent = "Finding you\u2026";
    navigator.geolocation.getCurrentPosition(
      function (pos) {
        state.location = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        paintLocation();
        toast("Location captured. Save the profile to keep it.", "ok");
      },
      function (err) {
        hint.className = "hint-line hint-line--warn";
        /* Denial is the common case and is not a fault, so it reads as a
           choice rather than an error. */
        hint.textContent =
          err.code === 1
            ? "Permission refused. You can still give an address above."
            : "Could not get a location. You can still give an address above.";
      },
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 }
    );
  });

  $("#clear-location").addEventListener("click", function () {
    state.location = null;
    paintLocation();
    toast("Pin removed. Save the profile to confirm.", "warn");
  });

  $("#profile-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var f = e.target.elements;
    var isShop = state.user && state.user.role === "shop";
    var msg = $("#profile-msg");
    msg.className = "msg";
    text(msg, "Saving\u2026");

    var body = {
      name: f.name.value.trim(),
      area: f.area.value.trim(),
    };
    /* null means untouched; "" means remove. Only send it when one of those
       was actually chosen. */
    if (state.avatar !== null) body.avatar = state.avatar;
    if (isShop) {
      body.shop_name = f.shop_name.value.trim();
      body.phone = f.phone.value.trim();
      body.address = f.address.value.trim();
      body.about = f.about.value.trim();
      /* An absent lat and lng means "leave the pin alone", which is right
         for a form that edits the address and nothing else. Removing one
         therefore has to be said outright. */
      if (state.location) {
        body.lat = state.location.lat;
        body.lng = state.location.lng;
      } else if (state.user.lat !== null && state.user.lat !== undefined) {
        body.clear_location = true;
      }
    }

    request("/api/me", { method: "PATCH", body: body }).then(
      function (user) {
        state.user = user;
        state.avatar = null;
        text(msg, "");
        text($("#avatar-hint"), "");
        paintAccount();
        paintTabs();
        toast("Profile saved.", "ok");
      },
      function (err) {
        msg.className = "msg msg--bad";
        text(msg, err.message);
        toast(err.message, "bad");
      }
    );
  });

  $("#area-toggle").addEventListener("click", function () {
    state.myAreaOnly = !state.myAreaOnly;
    this.setAttribute("aria-pressed", String(state.myAreaOnly));
    loadOpenRequests();
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      refreshUnread(true);
      refreshAlerts(true);
    }
  });

  /* ----------------------------------------------------------------- boot */

  if (!apiBase()) {
    show($("#no-api"), true);
    paintLanguage();
    paintAuthMode();
    paintBrowseIntro();
    paintTabs();
    return;
  }

  paintLanguage();
  paintAuthMode();
  loadCategories()
    .then(loadAreas)
    .then(restoreSession)
    .then(function () {
      go(state.user ? "home" : "browse");
      return refreshUnread(true).then(function () {
        return refreshAlerts(true);
      });
    });
})();
