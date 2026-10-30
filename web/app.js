/*
 * HyperLocal Student Marketplace -- browser client.
 *
 * Talks to the same JSON API as the JavaFX desktop app, so neither one knows
 * the other exists. No framework and no build step: this file is served as
 * written, which keeps the page quick on a phone and a student's data plan.
 *
 * The token lives in sessionStorage rather than localStorage, so closing the
 * tab signs you out. Every read and write is guarded, because storage throws
 * outright in a private window instead of politely returning nothing.
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
      /* Private browsing, or site data blocked. The app still works; it
         just forgets things between page loads. */
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
    myAreaOnly: false,
    lastRequestId: null,
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

  /* An element builder, so that nothing user-supplied is ever concatenated
     into HTML. A shop name is not markup. */
  function el(tag, className, content) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = content;
    return node;
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
       Without a word on screen that looks like the page is broken. */
    clearTimeout(slowTimer);
    slowTimer = setTimeout(function () {
      var note = $("#waking");
      if (!note) {
        note = el("div", "waking", "Waking the server… the free host sleeps when idle, so the first request can take up to a minute.");
        note.id = "waking";
        $("#main").prepend(note);
      }
    }, 2500);

    var method = options.method || "GET";

    /*
     * A sleeping instance drops the connection on the request that wakes it,
     * and anything else in flight alongside it. fetch rejects with a
     * TypeError for that, which is indistinguishable from a real outage on
     * one try -- so a read is simply tried again before giving up.
     *
     * Only reads. Retrying a POST could send the same offer twice, and a
     * duplicate offer is worse than an error message.
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

  /* ----------------------------------------------------------- navigation */

  var views = ["browse", "auth", "student", "shop", "offers"];

  function go(name) {
    if (name === "home") name = state.user ? state.user.role : "auth";
    views.forEach(function (v) {
      show($("#view-" + v), v === name);
    });
    window.scrollTo({ top: 0, behavior: "instant" });

    if (name === "browse") loadProducts();
    if (name === "student") loadMyRequests();
    if (name === "shop") {
      /* One at a time. Three parallel requests at an instance that is still
         waking gets some of them dropped, and the screen fills with errors
         for what is really just a cold start. */
      loadOpenRequests()
        .then(loadMyOffers)
        .then(loadMyProducts)
        .catch(function () {});
    }
  }

  function paintSession() {
    var signedIn = !!state.user;
    show($("#who"), signedIn);
    show($("#nav-home"), signedIn);
    show($("#nav-out"), signedIn);
    show($("#nav-auth"), !signedIn);
    if (signedIn) {
      text(
        $("#who"),
        (state.user.shop_name || state.user.name) +
          " · " +
          (state.user.role === "shop" ? "Shop" : "Student")
      );
    }
  }

  /* ------------------------------------------------------------ catalogue */

  function loadCategories() {
    return request("/api/categories", { auth: false })
      .then(function (data) {
        state.categories = data.categories || [];
        document.querySelectorAll("[data-categories]").forEach(function (sel) {
          var keepAny = sel.querySelector('option[value=""]');
          sel.textContent = "";
          if (keepAny) sel.appendChild(keepAny);
          state.categories.forEach(function (c) {
            sel.appendChild(new Option(c, c));
          });
        });
      })
      .catch(function () {
        /* The forms still submit; the server validates the category anyway. */
      });
  }

  /* ---------------------------------------------------------------- areas */

  /*
   * Area decides which shops hear about a request, and it is typed by hand.
   * Someone who invents a spelling nobody else uses gets a marketplace of
   * one: their request reaches no shop, and the screen that follows looks
   * exactly like one where shops simply have not replied yet. Suggesting the
   * areas already in use is what stops that happening in the first place.
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
          // Shown beside the suggestion by most browsers, so the choice
          // carries the reason to make it.
          opt.label = a.shops === 1 ? "1 shop" : a.shops + " shops";
          list.appendChild(opt);
        });
      })
      .catch(function () {
        /* Suggestions are a convenience. Typing an area still works. */
      });
  }

  function areaMatch(typed) {
    var key = (typed || "").trim().replace(/\s+/g, " ").toLowerCase();
    if (!key) return null;
    for (var i = 0; i < state.areas.length; i++) {
      if (state.areas[i].area.trim().replace(/\s+/g, " ").toLowerCase() === key) {
        return state.areas[i];
      }
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

  function loadProducts() {
    var form = $("#search");
    var params = new URLSearchParams();
    ["q", "category", "area"].forEach(function (k) {
      var v = form.elements[k].value.trim();
      if (v) params.set(k, v);
    });

    var list = $("#products");
    var empty = $("#products-empty");
    list.textContent = "";

    request("/api/products?" + params.toString(), { auth: false })
      .then(function (data) {
        var items = data.products || [];
        show(empty, items.length === 0);
        if (!items.length) {
          empty.textContent = "";
          empty.appendChild(el("strong", null, "Nothing listed yet."));
          empty.appendChild(
            document.createTextNode(
              "When shops add products they appear here, cheapest first."
            )
          );
          return;
        }
        items.forEach(function (p) {
          var li = el("li");
          li.appendChild(el("span", "list__title", p.name));
          li.appendChild(
            el(
              "span",
              "list__meta",
              [p.shop_name || p.shop_contact, p.area, p.specs]
                .filter(Boolean)
                .join(" · ")
            )
          );
          var side = el("span", "list__side");
          side.appendChild(el("span", "price", rupees(p.price)));
          li.appendChild(side);
          list.appendChild(li);
        });
      })
      .catch(function (err) {
        show(empty, true);
        empty.textContent = "";
        empty.appendChild(el("strong", null, "Could not load listings."));
        empty.appendChild(document.createTextNode(err.message));
      });
  }

  /* ----------------------------------------------------------------- auth */

  var registering = false;

  function paintAuthMode() {
    text($("#auth-title"), registering ? "Create an account" : "Sign in");
    text($("#auth-go"), registering ? "Create account" : "Sign in");
    text($("#auth-switch"), registering ? "I already have an account" : "Create an account");
    show($("#reg-only"), registering);
    $("#auth-form").elements.password.autocomplete = registering
      ? "new-password"
      : "current-password";
    paintRoleFields();
  }

  function paintRoleFields() {
    var form = $("#auth-form");
    var role = form.elements.role ? form.elements.role.value : "student";
    show($("#shop-name-field"), registering && role === "shop");
  }

  function signIn(email, password) {
    return request("/api/login", {
      method: "POST",
      auth: false,
      body: { email: email, password: password },
    }).then(function (data) {
      state.token = data.token;
      state.user = data.user;
      store(sessionStorage, TOKEN_KEY, data.token);
      paintSession();
      go("home");
    });
  }

  function restoreSession() {
    if (!state.token) return Promise.resolve();
    return request("/api/me")
      .then(function (user) {
        state.user = user;
        paintSession();
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

    request("/api/requests/mine")
      .then(function (data) {
        var items = data.requests || [];
        show(empty, items.length === 0);
        if (!items.length) {
          empty.textContent = "";
          empty.appendChild(el("strong", null, "No requests yet."));
          empty.appendChild(
            document.createTextNode("Post one above and nearby shops will be emailed.")
          );
          return;
        }
        items.forEach(function (r) {
          var li = el("li");
          var title = el("span", "list__title", r.title);
          li.appendChild(title);
          li.appendChild(
            el(
              "span",
              "list__meta",
              [
                r.category,
                r.budget ? "budget " + rupees(r.budget) : null,
                r.offer_count + (r.offer_count === 1 ? " offer" : " offers"),
              ]
                .filter(Boolean)
                .join(" · ")
            )
          );

          var side = el("span", "list__side");
          if (r.best_price !== null && r.best_price !== undefined) {
            side.appendChild(el("span", "price", rupees(r.best_price)));
            side.appendChild(el("span", "tag", "BEST SO FAR"));
          }
          var view = el("button", "btn btn--sm", r.offer_count ? "Compare" : "No offers yet");
          view.disabled = !r.offer_count;
          view.addEventListener("click", function () {
            openOffers(r.id);
          });
          side.appendChild(view);
          li.appendChild(side);
          list.appendChild(li);
        });
      })
      .catch(function (err) {
        show(empty, true);
        empty.textContent = err.message;
      });
  }

  /* ----------------------------------------------------------------- shop */

  function loadOpenRequests() {
    var list = $("#open-requests");
    var empty = $("#open-requests-empty");
    list.textContent = "";

    return request("/api/requests/open?my_area_only=" + (state.myAreaOnly ? "true" : "false"))
      .then(function (data) {
        var items = data.requests || [];
        show(empty, items.length === 0);
        if (!items.length) {
          empty.textContent = "";
          empty.appendChild(el("strong", null, "Nothing open right now."));
          empty.appendChild(
            document.createTextNode(
              state.myAreaOnly
                ? "No student in your area has posted yet. Turn off the area filter to see everything."
                : "When a student posts a request it shows up here."
            )
          );
          return;
        }
        items.forEach(function (r) {
          var li = el("li");
          li.appendChild(el("span", "list__title", r.title));
          li.appendChild(
            el(
              "span",
              "list__meta",
              [
                r.student_name,
                r.student_area,
                r.category,
                r.specs,
                r.offer_count + (r.offer_count === 1 ? " offer" : " offers"),
              ]
                .filter(Boolean)
                .join(" · ")
            )
          );

          var side = el("span", "list__side");
          if (r.budget) side.appendChild(el("span", "price", rupees(r.budget)));
          var btn = el("button", "btn btn--sm", r.i_offered ? "Offer again" : "Send offer");
          btn.addEventListener("click", function () {
            openOfferDialog(r);
          });
          side.appendChild(btn);
          li.appendChild(side);
          list.appendChild(li);
        });
      })
      .catch(function (err) {
        show(empty, true);
        empty.textContent = err.message;
      });
  }

  function loadMyOffers() {
    var list = $("#my-offers");
    var empty = $("#my-offers-empty");
    list.textContent = "";

    return request("/api/offers/mine")
      .then(function (data) {
        var items = data.offers || [];
        show(empty, items.length === 0);
        if (!items.length) {
          empty.textContent = "";
          empty.appendChild(el("strong", null, "No offers sent."));
          empty.appendChild(
            document.createTextNode("Reply to an open request above to send your first.")
          );
          return;
        }
        items.forEach(function (o) {
          var li = el("li");
          li.appendChild(el("span", "list__title", o.product_name));
          li.appendChild(
            el(
              "span",
              "list__meta",
              ["for “" + o.request_title + "”", o.note].filter(Boolean).join(" · ")
            )
          );

          var side = el("span", "list__side");
          side.appendChild(el("span", "price", rupees(o.price)));
          if (o.is_best) {
            side.appendChild(el("span", "tag", "CHEAPEST"));
          } else if (o.best_price !== null && o.best_price !== undefined) {
            side.appendChild(
              el("span", "delta", "beaten by " + rupees(Number(o.price) - Number(o.best_price)))
            );
          }
          li.appendChild(side);
          list.appendChild(li);
        });
      })
      .catch(function (err) {
        show(empty, true);
        empty.textContent = err.message;
      });
  }

  function loadMyProducts() {
    var list = $("#my-products");
    list.textContent = "";
    return request("/api/products/mine")
      .then(function (data) {
        (data.products || []).forEach(function (p) {
          var li = el("li");
          li.appendChild(el("span", "list__title", p.name));
          li.appendChild(
            el(
              "span",
              "list__meta",
              [p.category, p.specs, "stock " + p.stock].filter(Boolean).join(" · ")
            )
          );
          var side = el("span", "list__side");
          side.appendChild(el("span", "price", rupees(p.price)));
          li.appendChild(side);
          list.appendChild(li);
        });
      })
      .catch(function () {});
  }

  /* ----------------------------------------------- the comparison table */

  function openOffers(requestId) {
    state.lastRequestId = requestId;
    go("offers");

    var list = $("#offers");
    var empty = $("#offers-empty");
    var summary = $("#offers-summary");
    list.textContent = "";
    summary.textContent = "";

    request("/api/requests/" + requestId + "/offers")
      .then(function (data) {
        text($("#offers-title"), data.request.title);
        text(
          $("#offers-sub"),
          [data.request.category, data.request.specs].filter(Boolean).join(" · ")
        );

        [
          ["Offers", String(data.offer_count)],
          ["Best price", rupees(data.best_price)],
          ["Your budget", rupees(data.request.budget)],
          [
            "Under budget by",
            data.request.budget && data.best_price !== null
              ? rupees(Number(data.request.budget) - Number(data.best_price))
              : "—",
          ],
        ].forEach(function (pair) {
          var box = el("div");
          box.appendChild(el("dt", null, pair[0]));
          box.appendChild(el("dd", null, pair[1]));
          summary.appendChild(box);
        });

        var offers = data.offers || [];
        show(empty, offers.length === 0);
        if (!offers.length) {
          empty.textContent = "";
          empty.appendChild(el("strong", null, "No offers yet."));
          empty.appendChild(
            document.createTextNode("Shops in your area have been emailed about this request.")
          );
          return;
        }

        offers.forEach(function (o) {
          var li = el("li", o.is_best ? "offer--best" : null);
          li.appendChild(el("span", "list__title", o.shop_name));
          li.appendChild(
            el(
              "span",
              "list__meta",
              [o.product_name, o.area, o.specs, o.note].filter(Boolean).join(" · ")
            )
          );

          var side = el("span", "list__side");
          side.appendChild(el("span", "price price--lg", rupees(o.price)));
          if (o.is_best) side.appendChild(el("span", "tag", "CHEAPEST"));
          else side.appendChild(el("span", "delta", "+" + rupees(o.price_diff_vs_best)));
          li.appendChild(side);
          list.appendChild(li);
        });
      })
      .catch(function (err) {
        show(empty, true);
        empty.textContent = err.message;
      });
  }

  /* -------------------------------------------------------------- dialogs */

  var offerTarget = null;

  function openOfferDialog(req) {
    offerTarget = req;
    var form = $("#offer-form");
    form.reset();
    text($("#offer-msg"), "");
    text($("#offer-for"), req.title + (req.budget ? " · budget " + rupees(req.budget) : ""));
    form.elements.product_name.value = req.title;
    $("#offer-dialog").showModal();
  }

  /* ------------------------------------------------------------- wiring */

  document.addEventListener("click", function (e) {
    var nav = e.target.closest("[data-nav]");
    if (nav) {
      e.preventDefault();
      go(nav.getAttribute("data-nav"));
    }
  });

  $("#nav-out").addEventListener("click", function () {
    request("/api/logout", { method: "POST" }).catch(function () {});
    state.token = null;
    state.user = null;
    store(sessionStorage, TOKEN_KEY, null);
    paintSession();
    go("browse");
  });

  $("#nav-api").addEventListener("click", openApiDialog);
  $("#no-api-set").addEventListener("click", openApiDialog);

  function openApiDialog() {
    $("#api-form").elements.api.value = apiBase();
    $("#api-dialog").showModal();
  }

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

    var done = function (err) {
      msg.className = "msg msg--bad";
      text(msg, err.message);
    };

    if (!registering) {
      signIn(email, password).then(
        function () {
          text(msg, "");
        },
        done
      );
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
      },
    })
      .then(function () {
        return signIn(email, password);
      })
      .then(
        function () {
          text(msg, "");
        },
        done
      );
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
        loadMyRequests();
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
        loadOpenRequests()
          .then(loadMyOffers)
          .catch(function () {});
      },
      function (err) {
        window.alert(err.message);
      }
    );
  });

  $("#area-toggle").addEventListener("click", function () {
    state.myAreaOnly = !state.myAreaOnly;
    this.setAttribute("aria-pressed", String(state.myAreaOnly));
    loadOpenRequests();
  });

  /* ----------------------------------------------------------------- boot */

  if (!apiBase()) {
    show($("#no-api"), true);
    paintAuthMode();
    paintSession();
    return;
  }

  paintAuthMode();
  loadCategories()
    .then(loadAreas)
    .then(restoreSession)
    .then(function () {
      go(state.user ? "home" : "browse");
    });
})();
