package io.github.shrewaha.marketplace;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.webkit.WebSettingsCompat;
import androidx.webkit.WebViewAssetLoader;
import androidx.webkit.WebViewFeature;

/**
 * The whole app: a WebView showing the same client that runs at
 * shrewaha.github.io, loaded from inside the APK rather than over the network.
 *
 * The pages are not opened as file:// URLs. A file:// page has an opaque
 * origin, which browsers report to a server as "Origin: null" and which makes
 * localStorage and sessionStorage unreliable -- so sign-in and the saved
 * server address would both misbehave. WebViewAssetLoader instead serves the
 * bundled files from https://appassets.androidplatform.net/, a real origin
 * that never leaves the device. The client then behaves exactly as it does in
 * a desktop browser, with no Android-specific code in app.js.
 */
public class MainActivity extends Activity {

    private static final String START_URL =
            "https://appassets.androidplatform.net/assets/web/index.html";

    private WebView web;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        final WebViewAssetLoader loader = new WebViewAssetLoader.Builder()
                .addPathHandler("/assets/", new WebViewAssetLoader.AssetsPathHandler(this))
                .build();

        web = new WebView(this);
        setContentView(web);

        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        // The client stores the session token and the server address here.
        settings.setDomStorageEnabled(true);
        // The page is already laid out for a phone, so let it size itself
        // rather than rendering a desktop width and zooming out.
        settings.setUseWideViewPort(false);
        settings.setLoadWithOverviewMode(false);

        // Without this the WebView reports prefers-color-scheme: light whatever
        // the phone is set to, and styles.css would never use its dark palette.
        // Guarded because the feature depends on the installed WebView version,
        // not on the Android version.
        if (WebViewFeature.isFeatureSupported(WebViewFeature.ALGORITHMIC_DARKENING)) {
            WebSettingsCompat.setAlgorithmicDarkeningAllowed(settings, true);
        }

        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view,
                                                              WebResourceRequest request) {
                return loader.shouldInterceptRequest(request.getUrl());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri url = request.getUrl();
                // Anything inside the bundled app stays here. The footer links
                // to GitHub, and those belong in a real browser rather than in
                // a WebView with no address bar or tabs.
                if ("appassets.androidplatform.net".equals(url.getHost())) {
                    return false;
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, url));
                } catch (Exception ignored) {
                    // No browser installed, or nothing handles the scheme.
                    // Staying put is better than crashing.
                }
                return true;
            }
        });

        if (savedInstanceState == null) {
            web.loadUrl(START_URL);
        } else {
            web.restoreState(savedInstanceState);
        }
    }

    /** Keeps the current screen across a rotation instead of reloading. */
    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    /**
     * Back should step through the app's own screens first, and only leave
     * once there is nothing left to go back to.
     */
    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
