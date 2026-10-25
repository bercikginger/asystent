package pl.asystent;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;

/**
 * Apka = okno na serwer asystenta na laptopie (adres z Tailscale, np. https://laptop.xxxx.ts.net).
 * Daje stronie dostęp do mikrofonu i autoodtwarzanie odpowiedzi.
 */
public class MainActivity extends Activity {

    private static final int REQ_MIC = 1;
    private static final String PREFS = "asystent";
    private static final String KEY_URL = "url";

    private WebView web;
    private PermissionRequest pendingPermission;
    private boolean dialogOpen = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(Color.parseColor("#0e1116"));
        getWindow().setNavigationBarColor(Color.parseColor("#0e1116"));

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0e1116"));
        FrameLayout root = new FrameLayout(this);
        root.addView(web);
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);

        web.addJavascriptInterface(new Bridge(), "Asystent");

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> {
                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                        request.grant(request.getResources());
                    } else {
                        pendingPermission = request;
                        requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
                    }
                });
            }
        });

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) {
                    showError(String.valueOf(err.getDescription()));
                }
            }
        });

        String url = getUrl();
        if (url.isEmpty()) {
            showUrlDialog(true);
        } else if (savedInstanceState == null) {
            web.loadUrl(url);
        } else {
            web.restoreState(savedInstanceState);
        }
    }

    // --- adres serwera ---

    private String getUrl() {
        return getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_URL, "");
    }

    private void saveUrl(String url) {
        SharedPreferences.Editor e = getSharedPreferences(PREFS, MODE_PRIVATE).edit();
        e.putString(KEY_URL, url);
        e.apply();
    }

    private static String normalize(String raw) {
        String u = raw.trim();
        if (u.isEmpty()) return u;
        if (!u.startsWith("http://") && !u.startsWith("https://")) u = "https://" + u;
        Uri parsed = Uri.parse(u);
        if (parsed.getHost() == null) return "";
        return u.endsWith("/") ? u : u + "/";
    }

    private void showUrlDialog(boolean firstRun) {
        if (dialogOpen) return;
        dialogOpen = true;
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("https://laptop.xxxx.ts.net");
        input.setText(getUrl());
        input.setSingleLine(true);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        FrameLayout box = new FrameLayout(this);
        box.setPadding(pad, pad / 2, pad, 0);
        box.addView(input);

        AlertDialog.Builder b = new AlertDialog.Builder(this)
                .setTitle("Adres serwera")
                .setMessage("Adres laptopa z Tailscale (tailscale serve).")
                .setView(box)
                .setCancelable(!firstRun)
                .setPositiveButton("Połącz", (d, w) -> {
                    dialogOpen = false;
                    String u = normalize(input.getText().toString());
                    if (u.isEmpty()) {
                        showUrlDialog(true);
                        return;
                    }
                    saveUrl(u);
                    web.loadUrl(u);
                });
        if (!firstRun) b.setNegativeButton("Anuluj", (d, w) -> dialogOpen = false);
        b.setOnCancelListener(d -> dialogOpen = false);
        b.show();
    }

    private void showError(String reason) {
        if (dialogOpen) return;
        dialogOpen = true;
        new AlertDialog.Builder(this)
                .setTitle("Brak połączenia z laptopem")
                .setMessage("Sprawdź, czy na laptopie działa start.bat i Tailscale.\n\n" + reason)
                .setCancelable(false)
                .setPositiveButton("Ponów", (d, w) -> {
                    dialogOpen = false;
                    web.loadUrl(getUrl());
                })
                .setNegativeButton("Zmień adres", (d, w) -> {
                    dialogOpen = false;
                    showUrlDialog(false);
                })
                .show();
    }

    /** Wywoływane ze strony: window.Asystent.settings() */
    private class Bridge {
        @JavascriptInterface
        public void settings() {
            runOnUiThread(() -> showUrlDialog(false));
        }
    }

    // --- uprawnienia, cykl życia ---

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode != REQ_MIC || pendingPermission == null) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            pendingPermission.grant(pendingPermission.getResources());
        } else {
            pendingPermission.deny();
        }
        pendingPermission = null;
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }
}
