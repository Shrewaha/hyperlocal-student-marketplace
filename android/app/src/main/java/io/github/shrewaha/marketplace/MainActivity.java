package io.github.shrewaha.marketplace;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.Manifest;
import android.content.pm.PackageManager;
import android.webkit.GeolocationPermissions;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
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

    private static final int GEOLOCATION_REQUEST = 1;
    private static final int CAMERA_REQUEST = 2;

    private WebView web;
    private String pendingGeolocationOrigin;
    private GeolocationPermissions.Callback pendingGeolocationCallback;
    private PermissionRequest pendingCameraRequest;

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

        /*
         * A page asking for a location inside a WebView is refused by default
         * and told nothing, so the button would appear to do nothing at all.
         * The request has to be passed on twice: once to Android for the app's
         * own permission, and once back to the page.
         */
        web.setWebChromeClient(new WebChromeClient() {
            /*
             * The barcode scanner asks the page for a camera. A WebView
             * refuses that by default and, as with geolocation, tells the
             * page nothing -- so the scanner would work perfectly in a
             * browser and appear completely dead in the APK.
             */
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                boolean wantsCamera = false;
                for (String resource : request.getResources()) {
                    if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(resource)) {
                        wantsCamera = true;
                        break;
                    }
                }
                if (!wantsCamera) {
                    request.deny();
                    return;
                }

                if (checkSelfPermission(Manifest.permission.CAMERA)
                        == PackageManager.PERMISSION_GRANTED) {
                    // Only the bundled pages run here, so there is no
                    // third-party origin to weigh.
                    request.grant(new String[]{PermissionRequest.RESOURCE_VIDEO_CAPTURE});
                    return;
                }

                pendingCameraRequest = request;
                requestPermissions(new String[]{Manifest.permission.CAMERA},
                        CAMERA_REQUEST);
            }

            @Override
            public void onGeolocationPermissionsShowPrompt(
                    String origin, GeolocationPermissions.Callback callback) {
                boolean granted = checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                        == PackageManager.PERMISSION_GRANTED
                        || checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION)
                        == PackageManager.PERMISSION_GRANTED;

                if (granted) {
                    // Only our own bundled pages ever run here, so there is no
                    // third-party origin to weigh.
                    callback.invoke(origin, true, false);
                    return;
                }

                pendingGeolocationOrigin = origin;
                pendingGeolocationCallback = callback;
                requestPermissions(new String[]{
                        Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.ACCESS_COARSE_LOCATION,
                }, GEOLOCATION_REQUEST);
            }
        });

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

    /**
     * Android has answered the permission prompt; tell the page, which has
     * been waiting on a callback since it asked.
     */
    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
                                           int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);

        boolean granted = false;
        for (int result : results) {
            if (result == PackageManager.PERMISSION_GRANTED) {
                granted = true;
                break;
            }
        }

        if (requestCode == CAMERA_REQUEST && pendingCameraRequest != null) {
            if (granted) {
                pendingCameraRequest.grant(
                        new String[]{PermissionRequest.RESOURCE_VIDEO_CAPTURE});
            } else {
                // Denied has to be passed on too. Left unanswered the page
                // waits forever on a promise that never settles.
                pendingCameraRequest.deny();
            }
            pendingCameraRequest = null;
            return;
        }

        if (requestCode == GEOLOCATION_REQUEST && pendingGeolocationCallback != null) {
            pendingGeolocationCallback.invoke(pendingGeolocationOrigin, granted, false);
            pendingGeolocationCallback = null;
            pendingGeolocationOrigin = null;
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
