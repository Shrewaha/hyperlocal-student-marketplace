/*
 * HyperLocal Student Marketplace -- JavaFX desktop client.
 *
 * Single file on purpose: drop it in IntelliJ, add the JavaFX SDK and
 * org.json, and run. It talks to the FastAPI backend over plain JSON.
 *
 * Run:
 *   VM options:
 *     --module-path "C:\javafx-sdk-21\lib" --add-modules javafx.controls
 *
 * Every network call goes through async(), which runs on a background
 * thread and hands the result back on the JavaFX thread. Calling the API
 * directly from a button handler would freeze the whole window until the
 * server replied.
 */

import javafx.application.Application;
import javafx.application.Platform;
import javafx.collections.FXCollections;
import javafx.collections.ObservableList;
import javafx.geometry.Insets;
import javafx.geometry.Pos;
import javafx.scene.Parent;
import javafx.scene.Scene;
import javafx.scene.control.*;
import javafx.scene.layout.*;
import javafx.scene.text.Font;
import javafx.scene.text.FontWeight;
import javafx.stage.Stage;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.Callable;
import java.util.function.Consumer;

public class MarketplaceApp extends Application {

    /* ------------------------------------------------------------------
     * Configuration and session state
     * ------------------------------------------------------------------ */

    /*
     * Where the backend lives.
     *
     * A downloaded build cannot be recompiled by the person running it, so the
     * address has to be settable from outside. In order of precedence:
     *
     *   java -Dmarketplace.api=https://api.example.com -jar MarketplaceApp.jar
     *   MARKETPLACE_API=https://api.example.com  (environment variable)
     *   http://localhost:8000                    (unchanged default)
     *
     * Running from the IDE with nothing set behaves exactly as it always did.
     */
    static final String BASE_URL = resolveBaseUrl();

    private static String resolveBaseUrl() {
        String url = System.getProperty("marketplace.api");
        if (url == null || url.isBlank()) {
            url = System.getenv("MARKETPLACE_API");
        }
        if (url == null || url.isBlank()) {
            return "http://localhost:8000";
        }
        url = url.trim();
        // A trailing slash would turn every path into a double-slashed URL.
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        return url;
    }

    static final String[] CATEGORIES = {
            "Electronics", "Stationery", "Lab Components", "Books",
            "Computer Parts", "Tools", "Other"
    };

    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();

    private String token = null;
    private String role = null;
    private String userName = null;
    private String shopName = null;

    private Stage stage;

    /* Palette, kept in one place so the screens stay consistent. */
    static final String INK = "#111827";
    static final String MUTED = "#6B7280";
    static final String LINE = "#E5E7EB";
    static final String CANVAS = "#F9FAFB";
    static final String BEST_BG = "#D1FAE5";
    static final String BEST_INK = "#065F46";
    static final String DANGER = "#B91C1C";

    public static void main(String[] args) {
        launch(args);
    }

    @Override
    public void start(Stage primaryStage) {
        this.stage = primaryStage;
        stage.setTitle("HyperLocal Student Marketplace");
        showLoginScreen();
        stage.show();
    }

    /* ------------------------------------------------------------------
     * HTTP plumbing
     * ------------------------------------------------------------------ */

    /** Thrown for any non-2xx reply, carrying the server's own message. */
    static class ApiException extends RuntimeException {
        final int status;

        ApiException(int status, String message) {
            super(message);
            this.status = status;
        }
    }

    private HttpRequest.Builder base(String path) {
        HttpRequest.Builder b = HttpRequest.newBuilder()
                .uri(URI.create(BASE_URL + path))
                .timeout(Duration.ofSeconds(20))
                .header("Content-Type", "application/json");
        if (token != null) {
            b.header("Authorization", "Bearer " + token);
        }
        return b;
    }

    private JSONObject send(HttpRequest request) {
        try {
            HttpResponse<String> res =
                    HTTP.send(request, HttpResponse.BodyHandlers.ofString());
            String body = res.body() == null ? "" : res.body().trim();

            if (res.statusCode() >= 200 && res.statusCode() < 300) {
                if (body.isEmpty()) return new JSONObject();
                return new JSONObject(body);
            }

            // FastAPI reports problems as {"detail": "..."} -- surface that
            // rather than a bare status code.
            String message = "Request failed (HTTP " + res.statusCode() + ")";
            try {
                JSONObject err = new JSONObject(body);
                Object detail = err.opt("detail");
                if (detail instanceof String) {
                    message = (String) detail;
                } else if (detail instanceof JSONArray
                        && ((JSONArray) detail).length() > 0) {
                    // Pydantic validation errors arrive as a list.
                    JSONObject first = ((JSONArray) detail).getJSONObject(0);
                    message = first.optString("msg", message);
                }
            } catch (Exception ignored) {
                // Body was not JSON; keep the generic message.
            }
            throw new ApiException(res.statusCode(), message);

        } catch (ApiException e) {
            throw e;
        } catch (java.net.ConnectException e) {
            throw new ApiException(0,
                    "Cannot reach the server at " + BASE_URL
                            + ".\nIs uvicorn running?");
        } catch (Exception e) {
            throw new ApiException(0, e.getMessage() == null
                    ? e.toString() : e.getMessage());
        }
    }

    JSONObject getJson(String path) {
        return send(base(path).GET().build());
    }

    JSONObject postJson(String path, JSONObject body) {
        return send(base(path).POST(HttpRequest.BodyPublishers
                .ofString(body.toString(), StandardCharsets.UTF_8)).build());
    }

    JSONObject patchJson(String path, JSONObject body) {
        return send(base(path).method("PATCH", HttpRequest.BodyPublishers
                .ofString(body.toString(), StandardCharsets.UTF_8)).build());
    }

    static String enc(String s) {
        return URLEncoder.encode(s == null ? "" : s, StandardCharsets.UTF_8);
    }

    /**
     * Run a call off the UI thread, then deliver the result back on it.
     * onError may be null, in which case failures show an alert.
     */
    <T> void async(Callable<T> work, Consumer<T> onSuccess,
                   Consumer<String> onError) {
        Thread worker = new Thread(() -> {
            try {
                T result = work.call();
                Platform.runLater(() -> onSuccess.accept(result));
            } catch (Exception e) {
                String msg = e.getMessage() == null ? e.toString() : e.getMessage();
                Platform.runLater(() -> {
                    if (onError != null) onError.accept(msg);
                    else error(msg);
                });
            }
        });
        worker.setDaemon(true);   // never keeps the app alive after close
        worker.start();
    }

    <T> void async(Callable<T> work, Consumer<T> onSuccess) {
        async(work, onSuccess, null);
    }

    /* ------------------------------------------------------------------
     * Small UI helpers
     * ------------------------------------------------------------------ */

    static Label h1(String text) {
        Label l = new Label(text);
        l.setFont(Font.font("System", FontWeight.BOLD, 22));
        l.setStyle("-fx-text-fill: " + INK + ";");
        return l;
    }

    static Label h2(String text) {
        Label l = new Label(text);
        l.setFont(Font.font("System", FontWeight.BOLD, 15));
        l.setStyle("-fx-text-fill: " + INK + ";");
        return l;
    }

    static Label muted(String text) {
        Label l = new Label(text);
        l.setStyle("-fx-text-fill: " + MUTED + "; -fx-font-size: 12px;");
        l.setWrapText(true);
        return l;
    }

    static TextField field(String prompt) {
        TextField t = new TextField();
        t.setPromptText(prompt);
        return t;
    }

    static Button primary(String text) {
        Button b = new Button(text);
        b.setStyle("-fx-background-color: " + INK + "; -fx-text-fill: white; "
                + "-fx-font-weight: bold; -fx-padding: 8 18 8 18; "
                + "-fx-background-radius: 6;");
        return b;
    }

    static Button ghost(String text) {
        Button b = new Button(text);
        b.setStyle("-fx-background-color: transparent; -fx-text-fill: " + INK
                + "; -fx-border-color: " + LINE + "; -fx-border-radius: 6; "
                + "-fx-background-radius: 6; -fx-padding: 6 14 6 14;");
        return b;
    }

    static VBox card() {
        VBox v = new VBox(6);
        v.setPadding(new Insets(14));
        v.setStyle("-fx-background-color: white; -fx-border-color: " + LINE
                + "; -fx-border-radius: 10; -fx-background-radius: 10;");
        return v;
    }

    static ComboBox<String> categoryBox() {
        ComboBox<String> c = new ComboBox<>(
                FXCollections.observableArrayList(CATEGORIES));
        c.setPromptText("Category");
        c.setMaxWidth(Double.MAX_VALUE);
        return c;
    }

    static String money(double value) {
        // "Rs." rather than the rupee sign: the backend does the same, and it
        // keeps Windows consoles and older fonts out of trouble.
        return String.format("Rs. %.2f", value);
    }

    static void error(String message) {
        Alert a = new Alert(Alert.AlertType.ERROR, message, ButtonType.OK);
        a.setHeaderText("Something went wrong");
        a.getDialogPane().setMinWidth(420);
        a.showAndWait();
    }

    static void info(String message) {
        Alert a = new Alert(Alert.AlertType.INFORMATION, message, ButtonType.OK);
        a.setHeaderText(null);
        a.showAndWait();
    }

    /** Parses a money field, or returns null and flags the field in red. */
    static Double parseMoney(TextField f, boolean required) {
        String raw = f.getText() == null ? "" : f.getText().trim();
        if (raw.isEmpty()) {
            if (required) {
                f.setStyle("-fx-border-color: " + DANGER + ";");
                return null;
            }
            f.setStyle("");
            return Double.NaN;          // caller treats NaN as "not supplied"
        }
        try {
            double v = Double.parseDouble(raw.replace(",", ""));
            if (v < 0) throw new NumberFormatException();
            f.setStyle("");
            return v;
        } catch (NumberFormatException e) {
            f.setStyle("-fx-border-color: " + DANGER + ";");
            return null;
        }
    }

    private void setScene(Parent root, double w, double h) {
        Scene scene = new Scene(root, w, h);
        stage.setScene(scene);
    }

    /* ------------------------------------------------------------------
     * Login
     * ------------------------------------------------------------------ */

    void showLoginScreen() {
        VBox box = new VBox(12);
        box.setPadding(new Insets(40));
        box.setAlignment(Pos.CENTER);
        box.setStyle("-fx-background-color: " + CANVAS + ";");

        VBox panel = card();
        panel.setMaxWidth(380);

        TextField email = field("Email");
        PasswordField password = new PasswordField();
        password.setPromptText("Password");

        Button login = primary("Log in");
        login.setMaxWidth(Double.MAX_VALUE);
        Button register = ghost("Create an account");
        register.setMaxWidth(Double.MAX_VALUE);
        Label status = muted("");

        Runnable attempt = () -> {
            String e = email.getText().trim();
            String p = password.getText();
            if (e.isEmpty() || p.isEmpty()) {
                status.setText("Enter your email and password.");
                return;
            }
            login.setDisable(true);
            status.setText("Signing in...");

            JSONObject body = new JSONObject().put("email", e).put("password", p);
            async(() -> postJson("/api/login", body),
                    res -> {
                        token = res.getString("token");
                        JSONObject u = res.getJSONObject("user");
                        role = u.getString("role");
                        userName = u.optString("name", "");
                        shopName = u.optString("shop_name", null);
                        if ("student".equals(role)) showStudentDashboard();
                        else showShopDashboard();
                    },
                    msg -> {
                        login.setDisable(false);
                        status.setText(msg);
                    });
        };

        login.setOnAction(e -> attempt.run());
        password.setOnAction(e -> attempt.run());   // Enter submits
        register.setOnAction(e -> showRegisterScreen());

        panel.getChildren().addAll(
                h1("Welcome back"),
                muted("Students post what they need. Local shops compete on price."),
                new Separator(),
                email, password, login, register, status);

        box.getChildren().add(panel);
        setScene(box, 520, 520);
    }

    /* ------------------------------------------------------------------
     * Register
     * ------------------------------------------------------------------ */

    void showRegisterScreen() {
        VBox box = new VBox(12);
        box.setPadding(new Insets(30));
        box.setAlignment(Pos.CENTER);
        box.setStyle("-fx-background-color: " + CANVAS + ";");

        VBox panel = card();
        panel.setMaxWidth(420);

        TextField name = field("Full name");
        TextField email = field("Email");
        PasswordField password = new PasswordField();
        password.setPromptText("Password (at least 6 characters)");

        ComboBox<String> roleBox = new ComboBox<>(
                FXCollections.observableArrayList("student", "shop"));
        roleBox.setValue("student");
        roleBox.setMaxWidth(Double.MAX_VALUE);

        TextField area = field("Area or locality (e.g. Civil Lines)");
        TextField shop = field("Shop name");
        Label areaHint = muted(
                "Area is how students and shops find each other. "
                        + "Use the same spelling as the shops near you.");

        // The shop-name field only makes sense for a shop account.
        shop.setVisible(false);
        shop.setManaged(false);
        roleBox.setOnAction(e -> {
            boolean isShop = "shop".equals(roleBox.getValue());
            shop.setVisible(isShop);
            shop.setManaged(isShop);
        });

        Button submit = primary("Create account");
        submit.setMaxWidth(Double.MAX_VALUE);
        Button back = ghost("Back to login");
        back.setMaxWidth(Double.MAX_VALUE);
        Label status = muted("");

        submit.setOnAction(e -> {
            if (name.getText().trim().isEmpty()
                    || email.getText().trim().isEmpty()
                    || password.getText().isEmpty()) {
                status.setText("Name, email and password are required.");
                return;
            }
            if (password.getText().length() < 6) {
                status.setText("Password must be at least 6 characters.");
                return;
            }
            if ("shop".equals(roleBox.getValue())
                    && shop.getText().trim().isEmpty()) {
                status.setText("Shop accounts need a shop name.");
                return;
            }

            JSONObject body = new JSONObject()
                    .put("name", name.getText().trim())
                    .put("email", email.getText().trim())
                    .put("password", password.getText())
                    .put("role", roleBox.getValue())
                    .put("area", area.getText().trim());
            if ("shop".equals(roleBox.getValue())) {
                body.put("shop_name", shop.getText().trim());
            }

            submit.setDisable(true);
            status.setText("Creating your account...");
            async(() -> postJson("/api/register", body),
                    res -> {
                        info("Account created. You can log in now.");
                        showLoginScreen();
                    },
                    msg -> {
                        submit.setDisable(false);
                        status.setText(msg);
                    });
        });

        back.setOnAction(e -> showLoginScreen());

        panel.getChildren().addAll(h1("Create an account"), new Separator(),
                name, email, password, roleBox, area, shop, areaHint,
                submit, back, status);

        box.getChildren().add(panel);
        setScene(box, 560, 640);
    }

    /* ------------------------------------------------------------------
     * Nav bar (shared)
     * ------------------------------------------------------------------ */

    HBox buildNavBar() {
        HBox bar = new HBox(12);
        bar.setPadding(new Insets(12, 18, 12, 18));
        bar.setAlignment(Pos.CENTER_LEFT);
        bar.setStyle("-fx-background-color: " + INK + ";");

        Label title = new Label("HyperLocal Marketplace");
        title.setStyle("-fx-text-fill: white; -fx-font-weight: bold; "
                + "-fx-font-size: 14px;");

        String who = ("shop".equals(role) && shopName != null && !shopName.isEmpty())
                ? shopName : userName;
        Label user = new Label(who + "  (" + role + ")");
        user.setStyle("-fx-text-fill: #D1D5DB; -fx-font-size: 12px;");

        Region spacer = new Region();
        HBox.setHgrow(spacer, Priority.ALWAYS);

        CheckBox emailToggle = new CheckBox("Email me updates");
        emailToggle.setStyle("-fx-text-fill: #D1D5DB; -fx-font-size: 12px;");
        emailToggle.setDisable(true);     // until we know the real value

        async(() -> getJson("/api/notifications/settings"),
                res -> {
                    emailToggle.setSelected(res.optBoolean("notify_email", true));
                    emailToggle.setDisable(false);
                },
                msg -> emailToggle.setDisable(true));   // leave it alone on failure

        emailToggle.setOnAction(e -> {
            boolean wanted = emailToggle.isSelected();
            emailToggle.setDisable(true);
            JSONObject body = new JSONObject().put("notify_email", wanted);
            async(() -> patchJson("/api/notifications/settings", body),
                    res -> emailToggle.setDisable(false),
                    msg -> {
                        emailToggle.setSelected(!wanted);   // roll the UI back
                        emailToggle.setDisable(false);
                        error("Could not change the setting.\n" + msg);
                    });
        });

        Button logout = new Button("Log out");
        logout.setStyle("-fx-background-color: transparent; -fx-text-fill: white; "
                + "-fx-border-color: #4B5563; -fx-border-radius: 6; "
                + "-fx-background-radius: 6;");
        logout.setOnAction(e -> {
            String dying = token;
            token = null;
            role = null;
            userName = null;
            shopName = null;
            // Fire and forget: the local session is already gone either way.
            new Thread(() -> {
                try {
                    HTTP.send(HttpRequest.newBuilder()
                            .uri(URI.create(BASE_URL + "/api/logout"))
                            .header("Authorization", "Bearer " + dying)
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.noBody()).build(),
                            HttpResponse.BodyHandlers.ofString());
                } catch (Exception ignored) {
                }
            }).start();
            showLoginScreen();
        });

        bar.getChildren().addAll(title, user, spacer, emailToggle, logout);
        return bar;
    }

    /* ------------------------------------------------------------------
     * Student dashboard
     * ------------------------------------------------------------------ */

    void showStudentDashboard() {
        BorderPane root = new BorderPane();
        root.setTop(buildNavBar());

        TabPane tabs = new TabPane();
        tabs.setTabClosingPolicy(TabPane.TabClosingPolicy.UNAVAILABLE);

        Tab post = new Tab("Post a Request");
        Tab mine = new Tab("My Requests");
        Tab browse = new Tab("Browse Shops");

        VBox myRequestsBox = new VBox(10);
        myRequestsBox.setPadding(new Insets(16));
        ScrollPane myScroll = new ScrollPane(myRequestsBox);
        myScroll.setFitToWidth(true);
        mine.setContent(myScroll);

        post.setContent(buildPostRequestForm(() -> {
            loadMyRequests(myRequestsBox);
            tabs.getSelectionModel().select(mine);
        }));
        browse.setContent(buildBrowseProducts());

        // Refresh the list whenever the tab is opened, so a new offer shows up.
        mine.setOnSelectionChanged(e -> {
            if (mine.isSelected()) loadMyRequests(myRequestsBox);
        });

        tabs.getTabs().addAll(post, mine, browse);
        root.setCenter(tabs);

        loadMyRequests(myRequestsBox);
        setScene(root, 1000, 680);
    }

    VBox buildPostRequestForm(Runnable onPosted) {
        VBox form = new VBox(10);
        form.setPadding(new Insets(20));
        form.setMaxWidth(560);

        TextField title = field("What do you need? (e.g. ESP8266 NodeMCU)");
        ComboBox<String> category = categoryBox();
        TextArea description = new TextArea();
        description.setPromptText("Any details -- when you need it, condition, quantity");
        description.setPrefRowCount(3);
        TextField specs = field("Specs (e.g. NodeMCU v3, CH340)");
        TextField budget = field("Budget in Rs. (optional)");

        Button submit = primary("Post request");
        Label status = muted("");

        submit.setOnAction(e -> {
            if (title.getText().trim().isEmpty()) {
                status.setText("Give your request a title.");
                return;
            }
            if (category.getValue() == null) {
                status.setText("Pick a category.");
                return;
            }
            Double b = parseMoney(budget, false);
            if (b == null) {
                status.setText("Budget must be a number, or left blank.");
                return;
            }

            JSONObject body = new JSONObject()
                    .put("title", title.getText().trim())
                    .put("category", category.getValue())
                    .put("description", description.getText().trim())
                    .put("specs", specs.getText().trim());
            if (!Double.isNaN(b)) body.put("budget", b);

            submit.setDisable(true);
            status.setText("Posting...");
            async(() -> postJson("/api/requests", body),
                    res -> {
                        submit.setDisable(false);
                        title.clear();
                        description.clear();
                        specs.clear();
                        budget.clear();
                        category.setValue(null);
                        status.setText("");
                        info("Request posted. Shops in your area have been "
                                + "emailed, and offers will appear under "
                                + "My Requests.");
                        onPosted.run();
                    },
                    msg -> {
                        submit.setDisable(false);
                        status.setText(msg);
                    });
        });

        form.getChildren().addAll(h1("Post a request"),
                muted("Shops in your area get an email and reply with offers."),
                new Separator(), title, category, description, specs, budget,
                submit, status);
        return form;
    }

    void loadMyRequests(VBox container) {
        container.getChildren().setAll(muted("Loading..."));
        async(() -> getJson("/api/requests/mine"), res -> {
            JSONArray arr = res.optJSONArray("requests");
            container.getChildren().clear();
            if (arr == null || arr.length() == 0) {
                container.getChildren().add(
                        muted("No requests yet. Post one from the first tab."));
                return;
            }
            for (int i = 0; i < arr.length(); i++) {
                JSONObject r = arr.getJSONObject(i);
                container.getChildren().add(buildRequestCard(r));
            }
        }, msg -> container.getChildren().setAll(muted(msg)));
    }

    VBox buildRequestCard(JSONObject r) {
        VBox c = card();
        int id = r.getInt("id");
        int offerCount = r.optInt("offer_count", 0);

        Label title = h2(r.optString("title"));
        Label meta = muted(r.optString("category")
                + (r.isNull("budget") ? ""
                : "   ·   budget " + money(r.getDouble("budget")))
                + "   ·   " + offerCount + (offerCount == 1 ? " offer" : " offers"));

        HBox row = new HBox(10);
        row.setAlignment(Pos.CENTER_LEFT);

        Label best = new Label();
        if (!r.isNull("best_price")) {
            best.setText("Best so far: " + money(r.getDouble("best_price")));
            best.setStyle("-fx-text-fill: " + BEST_INK + "; -fx-font-weight: bold; "
                    + "-fx-background-color: " + BEST_BG + "; -fx-padding: 4 8 4 8; "
                    + "-fx-background-radius: 6;");
        } else {
            best.setText("Waiting for offers");
            best.setStyle("-fx-text-fill: " + MUTED + ";");
        }

        Region spacer = new Region();
        HBox.setHgrow(spacer, Priority.ALWAYS);

        Button compare = primary("View Offers & Compare");
        compare.setDisable(offerCount == 0);
        compare.setOnAction(e -> showCompareScreen(id, r.optString("title")));

        row.getChildren().addAll(best, spacer, compare);
        c.getChildren().addAll(title, meta, row);
        return c;
    }

    /* ------------------------------------------------------------------
     * Comparison screen
     * ------------------------------------------------------------------ */

    /** One row of the comparison table. */
    public static class OfferRow {
        private final String shop, area, product, specs, price, result;
        private final boolean best;

        OfferRow(JSONObject o, double bestPrice) {
            this.shop = o.optString("shop_name", "-");
            this.area = o.optString("area", "-");
            this.product = o.optString("product_name", "-");
            this.specs = o.isNull("specs") ? "-" : o.optString("specs", "-");
            double p = o.getDouble("price");
            this.price = money(p);
            this.best = o.optBoolean("is_best", false);
            this.result = best ? "BEST DEAL" : "+" + money(p - bestPrice);
        }

        public String getShop() { return shop; }
        public String getArea() { return area; }
        public String getProduct() { return product; }
        public String getSpecs() { return specs; }
        public String getPrice() { return price; }
        public String getResult() { return result; }
        public boolean isBest() { return best; }
    }

    void showCompareScreen(int requestId, String requestTitle) {
        BorderPane root = new BorderPane();
        root.setTop(buildNavBar());

        VBox body = new VBox(12);
        body.setPadding(new Insets(18));

        Button back = ghost("< Back to my requests");
        back.setOnAction(e -> showStudentDashboard());

        Label heading = h1("Offers for: " + requestTitle);
        Label summary = muted("Loading offers...");

        TableView<OfferRow> table = new TableView<>();
        table.setPlaceholder(new Label("No offers on this request yet."));
        table.setColumnResizePolicy(TableView.CONSTRAINED_RESIZE_POLICY);
        VBox.setVgrow(table, Priority.ALWAYS);

        table.getColumns().addAll(
                col("Shop", "shop", 160),
                col("Area", "area", 110),
                col("Product", "product", 180),
                col("Specs", "specs", 180),
                col("Price (Rs.)", "price", 110),
                col("Result", "result", 120));

        // The cheapest row is the whole point of this screen, so it gets the
        // green treatment -- and the style is cleared on recycled rows.
        table.setRowFactory(tv -> new TableRow<OfferRow>() {
            @Override
            protected void updateItem(OfferRow item, boolean empty) {
                super.updateItem(item, empty);
                if (empty || item == null || !item.isBest()) {
                    setStyle("");
                } else {
                    setStyle("-fx-background-color: " + BEST_BG + "; "
                            + "-fx-font-weight: bold; -fx-text-fill: " + BEST_INK + ";");
                }
            }
        });

        async(() -> getJson("/api/requests/" + requestId + "/offers"), res -> {
            JSONArray arr = res.optJSONArray("offers");
            ObservableList<OfferRow> rows = FXCollections.observableArrayList();
            double bestPrice = res.isNull("best_price") ? 0 : res.getDouble("best_price");
            if (arr != null) {
                for (int i = 0; i < arr.length(); i++) {
                    rows.add(new OfferRow(arr.getJSONObject(i), bestPrice));
                }
            }
            table.setItems(rows);

            JSONObject req = res.optJSONObject("request");
            StringBuilder s = new StringBuilder();
            s.append(rows.size()).append(rows.size() == 1 ? " offer" : " offers");
            if (!rows.isEmpty()) s.append("   ·   best ").append(money(bestPrice));
            if (req != null && !req.isNull("budget")) {
                double budget = req.getDouble("budget");
                s.append("   ·   budget ").append(money(budget));
                if (!rows.isEmpty()) {
                    s.append(bestPrice <= budget
                            ? "   ·   within budget" : "   ·   over budget");
                }
            }
            summary.setText(s.toString());
        }, msg -> summary.setText(msg));

        body.getChildren().addAll(back, heading, summary, table);
        root.setCenter(body);
        setScene(root, 1000, 680);
    }

    static <S> TableColumn<S, String> col(String title, String property, double width) {
        TableColumn<S, String> c = new TableColumn<>(title);
        c.setPrefWidth(width);
        c.setCellValueFactory(new javafx.scene.control.cell
                .PropertyValueFactory<>(property));
        return c;
    }

    /* ------------------------------------------------------------------
     * Browse products (student)
     * ------------------------------------------------------------------ */

    /** One row of the public product browse. */
    public static class ProductRow {
        private final String name, category, shop, area, specs, price, stock;

        ProductRow(JSONObject p) {
            this.name = p.optString("name", "-");
            this.category = p.optString("category", "-");
            this.shop = p.isNull("shop_name")
                    ? p.optString("shop_contact", "-") : p.optString("shop_name");
            this.area = p.isNull("area") ? "-" : p.optString("area");
            this.specs = p.isNull("specs") ? "-" : p.optString("specs");
            this.price = money(p.getDouble("price"));
            this.stock = String.valueOf(p.optInt("stock", 0));
        }

        public String getName() { return name; }
        public String getCategory() { return category; }
        public String getShop() { return shop; }
        public String getArea() { return area; }
        public String getSpecs() { return specs; }
        public String getPrice() { return price; }
        public String getStock() { return stock; }
    }

    VBox buildBrowseProducts() {
        VBox box = new VBox(12);
        box.setPadding(new Insets(18));

        TextField search = field("Search products");
        ComboBox<String> category = categoryBox();
        category.getItems().add(0, "All categories");
        category.setValue("All categories");
        Button go = primary("Search");

        HBox controls = new HBox(10, search, category, go);
        HBox.setHgrow(search, Priority.ALWAYS);
        controls.setAlignment(Pos.CENTER_LEFT);

        TableView<ProductRow> table = new TableView<>();
        table.setPlaceholder(new Label("No products found."));
        table.setColumnResizePolicy(TableView.CONSTRAINED_RESIZE_POLICY);
        VBox.setVgrow(table, Priority.ALWAYS);
        table.getColumns().addAll(
                col("Product", "name", 180),
                col("Shop", "shop", 150),
                col("Area", "area", 110),
                col("Category", "category", 130),
                col("Specs", "specs", 160),
                col("Price (Rs.)", "price", 110),
                col("Stock", "stock", 70));

        Runnable load = () -> {
            String q = search.getText().trim();
            String cat = category.getValue();
            StringBuilder path = new StringBuilder("/api/products?limit=200");
            if (!q.isEmpty()) path.append("&q=").append(enc(q));
            if (cat != null && !"All categories".equals(cat)) {
                path.append("&category=").append(enc(cat));
            }
            async(() -> getJson(path.toString()), res -> {
                JSONArray arr = res.optJSONArray("products");
                ObservableList<ProductRow> rows = FXCollections.observableArrayList();
                if (arr != null) {
                    for (int i = 0; i < arr.length(); i++) {
                        rows.add(new ProductRow(arr.getJSONObject(i)));
                    }
                }
                table.setItems(rows);
            });
        };

        go.setOnAction(e -> load.run());
        search.setOnAction(e -> load.run());
        load.run();

        box.getChildren().addAll(h1("Browse local shops"),
                muted("Everything shops have listed, cheapest first."),
                controls, table);
        return box;
    }

    /* ------------------------------------------------------------------
     * Shop dashboard
     * ------------------------------------------------------------------ */

    void showShopDashboard() {
        BorderPane root = new BorderPane();
        root.setTop(buildNavBar());

        TabPane tabs = new TabPane();
        tabs.setTabClosingPolicy(TabPane.TabClosingPolicy.UNAVAILABLE);

        Tab addProduct = new Tab("Add Product");
        Tab requests = new Tab("Student Requests");
        Tab myOffers = new Tab("My Offers");

        VBox requestsBox = new VBox(10);
        requestsBox.setPadding(new Insets(16));
        ScrollPane reqScroll = new ScrollPane(requestsBox);
        reqScroll.setFitToWidth(true);

        CheckBox areaOnly = new CheckBox("Only requests in my area");
        areaOnly.setSelected(true);
        areaOnly.setPadding(new Insets(12, 16, 0, 16));
        areaOnly.setOnAction(e -> loadOpenRequests(requestsBox, areaOnly.isSelected()));

        VBox reqWrap = new VBox(areaOnly, reqScroll);
        VBox.setVgrow(reqScroll, Priority.ALWAYS);
        requests.setContent(reqWrap);

        TableView<OfferMineRow> offersTable = buildMyOffersTable();
        myOffers.setContent(offersTable);

        addProduct.setContent(buildAddProductForm());

        requests.setOnSelectionChanged(e -> {
            if (requests.isSelected()) {
                loadOpenRequests(requestsBox, areaOnly.isSelected());
            }
        });
        myOffers.setOnSelectionChanged(e -> {
            if (myOffers.isSelected()) loadMyOffers(offersTable);
        });

        tabs.getTabs().addAll(addProduct, requests, myOffers);
        tabs.getSelectionModel().select(requests);
        root.setCenter(tabs);

        loadOpenRequests(requestsBox, true);
        setScene(root, 1000, 680);
    }

    VBox buildAddProductForm() {
        VBox form = new VBox(10);
        form.setPadding(new Insets(20));
        form.setMaxWidth(560);

        TextField name = field("Product name");
        ComboBox<String> category = categoryBox();
        TextArea description = new TextArea();
        description.setPromptText("Description");
        description.setPrefRowCount(3);
        TextField specs = field("Specs");
        TextField price = field("Price in Rs.");
        TextField stock = field("Stock (default 1)");

        Button submit = primary("Add product");
        Label status = muted("");

        submit.setOnAction(e -> {
            if (name.getText().trim().isEmpty()) {
                status.setText("Product name is required.");
                return;
            }
            if (category.getValue() == null) {
                status.setText("Pick a category.");
                return;
            }
            Double p = parseMoney(price, true);
            if (p == null || Double.isNaN(p)) {
                status.setText("Enter a valid price.");
                return;
            }
            int st = 1;
            try {
                String raw = stock.getText().trim();
                if (!raw.isEmpty()) st = Math.max(0, Integer.parseInt(raw));
            } catch (NumberFormatException ex) {
                status.setText("Stock must be a whole number.");
                return;
            }

            JSONObject body = new JSONObject()
                    .put("name", name.getText().trim())
                    .put("category", category.getValue())
                    .put("description", description.getText().trim())
                    .put("specs", specs.getText().trim())
                    .put("price", p)
                    .put("stock", st);

            submit.setDisable(true);
            async(() -> postJson("/api/products", body), res -> {
                submit.setDisable(false);
                name.clear();
                description.clear();
                specs.clear();
                price.clear();
                stock.clear();
                category.setValue(null);
                status.setText("");
                info("Product added. Students can find it under Browse Shops.");
            }, msg -> {
                submit.setDisable(false);
                status.setText(msg);
            });
        });

        form.getChildren().addAll(h1("Add a product"),
                muted("Listed products show up in the student product search."),
                new Separator(), name, category, description, specs, price,
                stock, submit, status);
        return form;
    }

    void loadOpenRequests(VBox container, boolean areaOnly) {
        container.getChildren().setAll(muted("Loading..."));
        String path = "/api/requests/open?my_area_only=" + areaOnly;
        async(() -> getJson(path), res -> {
            JSONArray arr = res.optJSONArray("requests");
            container.getChildren().clear();
            if (arr == null || arr.length() == 0) {
                container.getChildren().add(muted(areaOnly
                        ? "No open requests in your area right now. Untick the "
                        + "box above to see every open request."
                        : "No open requests right now."));
                return;
            }
            for (int i = 0; i < arr.length(); i++) {
                container.getChildren().add(
                        buildOpenRequestCard(arr.getJSONObject(i), container, areaOnly));
            }
        }, msg -> container.getChildren().setAll(muted(msg)));
    }

    VBox buildOpenRequestCard(JSONObject r, VBox container, boolean areaOnly) {
        VBox c = card();
        int id = r.getInt("id");
        boolean alreadyOffered = !r.isNull("i_offered");

        Label title = h2(r.optString("title"));
        StringBuilder meta = new StringBuilder(r.optString("category"));
        if (!r.isNull("budget")) meta.append("   ·   budget ")
                .append(money(r.getDouble("budget")));
        meta.append("   ·   ").append(r.optInt("offer_count", 0)).append(" offers");
        if (!r.isNull("student_area")) {
            meta.append("   ·   ").append(r.getString("student_area"));
        }

        VBox details = new VBox(4);
        if (!r.isNull("specs") && !r.optString("specs").isEmpty()) {
            details.getChildren().add(muted("Specs: " + r.getString("specs")));
        }
        if (!r.isNull("description") && !r.optString("description").isEmpty()) {
            details.getChildren().add(muted(r.getString("description")));
        }

        VBox offerForm = buildOfferForm(id, () ->
                loadOpenRequests(container, areaOnly));
        offerForm.setVisible(false);
        offerForm.setManaged(false);

        Button toggle = alreadyOffered ? ghost("Send another offer")
                : primary("Send an offer");
        toggle.setOnAction(e -> {
            boolean show = !offerForm.isVisible();
            offerForm.setVisible(show);
            offerForm.setManaged(show);
            toggle.setText(show ? "Cancel" : (alreadyOffered
                    ? "Send another offer" : "Send an offer"));
        });

        HBox actions = new HBox(10, toggle);
        if (alreadyOffered) {
            Label done = new Label("You have offered on this");
            done.setStyle("-fx-text-fill: " + BEST_INK + "; -fx-background-color: "
                    + BEST_BG + "; -fx-padding: 4 8 4 8; -fx-background-radius: 6; "
                    + "-fx-font-size: 11px;");
            actions.getChildren().add(done);
        }
        actions.setAlignment(Pos.CENTER_LEFT);

        c.getChildren().addAll(title, muted(meta.toString()), details,
                actions, offerForm);
        return c;
    }

    VBox buildOfferForm(int requestId, Runnable onSubmitted) {
        VBox form = new VBox(8);
        form.setPadding(new Insets(12, 0, 0, 0));

        TextField product = field("Product you can supply");
        TextField specs = field("Specs you are offering");
        TextField price = field("Your price in Rs.");
        TextField note = field("Note (delivery, warranty, stock)");

        Button submit = primary("Submit offer");
        Label status = muted("");

        submit.setOnAction(e -> {
            if (product.getText().trim().isEmpty()) {
                status.setText("Name the product you are offering.");
                return;
            }
            Double p = parseMoney(price, true);
            if (p == null || Double.isNaN(p)) {
                status.setText("Enter a valid price.");
                return;
            }

            JSONObject body = new JSONObject()
                    .put("request_id", requestId)
                    .put("product_name", product.getText().trim())
                    .put("specs", specs.getText().trim())
                    .put("price", p)
                    .put("note", note.getText().trim());

            submit.setDisable(true);
            status.setText("Sending...");
            async(() -> postJson("/api/offers", body), res -> {
                submit.setDisable(false);
                status.setText("");
                info("Offer sent. The student has been emailed with your "
                        + "price compared against the others.");
                onSubmitted.run();
            }, msg -> {
                submit.setDisable(false);
                status.setText(msg);
            });
        });

        form.getChildren().addAll(new Separator(), product, specs, price, note,
                submit, status);
        return form;
    }

    /** One row of the shop's own offer list. */
    public static class OfferMineRow {
        private final String request, product, price, best, status;

        OfferMineRow(JSONObject o) {
            this.request = o.optString("request_title", "-");
            this.product = o.optString("product_name", "-");
            this.price = money(o.getDouble("price"));
            boolean isBest = o.optBoolean("is_best", false);
            this.best = isBest ? "Cheapest"
                    : (o.isNull("best_price") ? "-"
                    : "Beaten by " + money(o.getDouble("best_price")));
            this.status = o.optString("status", "-");
        }

        public String getRequest() { return request; }
        public String getProduct() { return product; }
        public String getPrice() { return price; }
        public String getBest() { return best; }
        public String getStatus() { return status; }
    }

    TableView<OfferMineRow> buildMyOffersTable() {
        TableView<OfferMineRow> table = new TableView<>();
        table.setPlaceholder(new Label("You have not sent any offers yet."));
        table.setColumnResizePolicy(TableView.CONSTRAINED_RESIZE_POLICY);
        table.getColumns().addAll(
                col("Request", "request", 220),
                col("Your product", "product", 200),
                col("Your price", "price", 120),
                col("Standing", "best", 180),
                col("Request status", "status", 120));
        loadMyOffers(table);
        return table;
    }

    void loadMyOffers(TableView<OfferMineRow> table) {
        async(() -> getJson("/api/offers/mine"), res -> {
            JSONArray arr = res.optJSONArray("offers");
            ObservableList<OfferMineRow> rows = FXCollections.observableArrayList();
            if (arr != null) {
                for (int i = 0; i < arr.length(); i++) {
                    rows.add(new OfferMineRow(arr.getJSONObject(i)));
                }
            }
            table.setItems(rows);
        });
    }
}
