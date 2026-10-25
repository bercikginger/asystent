package pl.asystent;

import android.Manifest;
import android.app.Activity;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Color;
import android.hardware.usb.UsbManager;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.AlarmClock;
import android.view.KeyEvent;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.webkit.WebViewAssetLoader;

import com.hoho.android.usbserial.driver.UsbSerialDriver;

import org.json.JSONObject;

import java.text.Normalizer;
import java.util.List;
import java.util.Locale;

/**
 * Interfejs jest wbudowany w apkę (assets) i serwowany z https://appassets.androidplatform.net.
 * Natywnie: mikrofon, przycisk słuchawek, mikrofon Bluetooth, akcje telefonu, wgrywanie przez USB OTG.
 */
public class MainActivity extends Activity {

    private static final int REQ_MIC = 1;
    private static final String START_URL = "https://appassets.androidplatform.net/assets/index.html";
    private static final String ACTION_USB_PERMISSION = "pl.asystent.USB_PERMISSION";
    static final String VERSION = "1.5";

    private WebView web;
    private PermissionRequest pendingPermission;
    private MediaSession session;
    private boolean headsetEnabled = true;
    private boolean pageReady = false;
    private boolean pendingAssist = false;
    private String pendingFlash = null;
    private volatile boolean flashing = false;

    private final BroadcastReceiver usbReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context ctx, Intent intent) {
            if (!ACTION_USB_PERMISSION.equals(intent.getAction()) || pendingFlash == null) return;
            String job = pendingFlash;
            pendingFlash = null;
            if (intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)) startFlash(job);
            else flashEvent("błąd", 0, true, false, "Brak zgody na dostęp do USB.");
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(Color.BLACK);
        getWindow().setNavigationBarColor(Color.BLACK);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON); // tryb rozmowy

        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setUserAgentString(s.getUserAgentString() + " AsystentApp/" + VERSION);
        web.addJavascriptInterface(new Bridge(), "AsystentNative");

        final WebViewAssetLoader loader = new WebViewAssetLoader.Builder()
                .addPathHandler("/assets/", new WebViewAssetLoader.AssetsPathHandler(this))
                .build();

        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return loader.shouldInterceptRequest(request.getUrl());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                return !"appassets.androidplatform.net".equals(u.getHost());
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                pageReady = true;
                if (pendingAssist) {
                    pendingAssist = false;
                    js("window.onAssist && window.onAssist()");
                }
            }
        });

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

        IntentFilter f = new IntentFilter(ACTION_USB_PERMISSION);
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(usbReceiver, f, Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(usbReceiver, f);

        setupMediaSession();
        handleIntent(getIntent());

        if (savedInstanceState != null) web.restoreState(savedInstanceState);
        else web.loadUrl(START_URL);
    }

    // --- wywołanie jako asystent / przycisk głosowy ---

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleIntent(intent);
    }

    private void handleIntent(Intent intent) {
        if (intent == null) return;
        String a = intent.getAction();
        if (Intent.ACTION_ASSIST.equals(a) || Intent.ACTION_VOICE_COMMAND.equals(a)) {
            if (pageReady) js("window.onAssist && window.onAssist()");
            else pendingAssist = true;
        }
    }

    // --- przycisk na słuchawkach / kierownicy ---

    private void setupMediaSession() {
        session = new MediaSession(this, "Asystent");
        session.setCallback(new MediaSession.Callback() {
            @Override
            public boolean onMediaButtonEvent(Intent intent) {
                KeyEvent ke = intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT);
                if (headsetEnabled && ke != null && isTalkKey(ke.getKeyCode())) {
                    if (ke.getAction() == KeyEvent.ACTION_DOWN && ke.getRepeatCount() == 0) headsetPressed();
                    return true;
                }
                return super.onMediaButtonEvent(intent);
            }
        });
        session.setPlaybackState(new PlaybackState.Builder()
                .setActions(PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE)
                .setState(PlaybackState.STATE_PLAYING, 0, 1f)
                .build());
    }

    private static boolean isTalkKey(int code) {
        return code == KeyEvent.KEYCODE_HEADSETHOOK || code == KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
                || code == KeyEvent.KEYCODE_MEDIA_PLAY || code == KeyEvent.KEYCODE_MEDIA_PAUSE;
    }

    private void headsetPressed() {
        js("window.onHeadsetButton && window.onHeadsetButton()");
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (headsetEnabled && isTalkKey(keyCode)) {
            if (event.getRepeatCount() == 0) headsetPressed();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (session != null) session.setActive(headsetEnabled);
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (session != null) session.setActive(false); // poza apką przycisk wraca do odtwarzacza muzyki
    }

    // --- most JS <-> Android ---

    private class Bridge {
        @JavascriptInterface
        public String info() {
            try {
                return new JSONObject().put("version", VERSION).put("sdk", Build.VERSION.SDK_INT)
                        .put("usbHost", getPackageManager().hasSystemFeature(PackageManager.FEATURE_USB_HOST)).toString();
            } catch (Exception e) {
                return "{}";
            }
        }

        @JavascriptInterface
        public String action(String json) {
            try {
                return runAction(new JSONObject(json));
            } catch (Exception e) {
                return "błąd: " + e.getMessage();
            }
        }

        @JavascriptInterface
        public String setBtMic(boolean on) {
            return btMic(on);
        }

        @JavascriptInterface
        public void setHeadsetButton(boolean on) {
            headsetEnabled = on;
            runOnUiThread(() -> session.setActive(on));
        }

        @JavascriptInterface
        public void openUrl(String url) {
            if (url == null || !(url.startsWith("https://") || url.startsWith("http://"))) return;
            runOnUiThread(() -> {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception ignored) {
                }
            });
        }

        @JavascriptInterface
        public String usbDevices() {
            return OtgFlasher.describe((UsbManager) getSystemService(USB_SERVICE)).toString();
        }

        @JavascriptInterface
        public void flash(String json) {
            runOnUiThread(() -> requestFlash(json));
        }
    }

    private void js(String code) {
        runOnUiThread(() -> web.evaluateJavascript(code, null));
    }

    // --- akcje telefonu (minutnik, budzik, aplikacje, muzyka) ---

    private String runAction(JSONObject a) throws Exception {
        String type = a.optString("action");
        switch (type) {
            case "timer": {
                int sec = Math.max(1, a.optInt("seconds", 60));
                Intent i = new Intent(AlarmClock.ACTION_SET_TIMER)
                        .putExtra(AlarmClock.EXTRA_LENGTH, sec)
                        .putExtra(AlarmClock.EXTRA_MESSAGE, a.optString("label", "Asystent"))
                        .putExtra(AlarmClock.EXTRA_SKIP_UI, true);
                startActivity(i);
                return "minutnik: " + sec + " s";
            }
            case "alarm": {
                Intent i = new Intent(AlarmClock.ACTION_SET_ALARM)
                        .putExtra(AlarmClock.EXTRA_HOUR, a.optInt("hour", 7))
                        .putExtra(AlarmClock.EXTRA_MINUTES, a.optInt("minute", 0))
                        .putExtra(AlarmClock.EXTRA_MESSAGE, a.optString("label", "Asystent"))
                        .putExtra(AlarmClock.EXTRA_SKIP_UI, true);
                startActivity(i);
                return String.format(Locale.ROOT, "budzik: %02d:%02d", a.optInt("hour", 7), a.optInt("minute", 0));
            }
            case "open_app": {
                String want = norm(a.optString("app"));
                Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
                List<ResolveInfo> apps = getPackageManager().queryIntentActivities(launcher, 0);
                ResolveInfo best = null;
                for (ResolveInfo r : apps) {
                    String label = norm(String.valueOf(r.loadLabel(getPackageManager())));
                    if (label.equals(want)) {
                        best = r;
                        break;
                    }
                    if (best == null && !want.isEmpty() && (label.contains(want) || want.contains(label))) best = r;
                }
                if (best == null) return "nie znalazłem aplikacji: " + a.optString("app");
                Intent i = getPackageManager().getLaunchIntentForPackage(best.activityInfo.packageName);
                if (i != null) startActivity(i);
                return "otwieram: " + best.loadLabel(getPackageManager());
            }
            case "media": {
                String cmd = a.optString("command", "play_pause");
                int code = "next".equals(cmd) ? KeyEvent.KEYCODE_MEDIA_NEXT
                        : "previous".equals(cmd) ? KeyEvent.KEYCODE_MEDIA_PREVIOUS : KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE;
                // na chwilę oddaj przyciski odtwarzaczowi muzyki
                runOnUiThread(() -> session.setActive(false));
                AudioManager am = (AudioManager) getSystemService(AUDIO_SERVICE);
                am.dispatchMediaKeyEvent(new KeyEvent(KeyEvent.ACTION_DOWN, code));
                am.dispatchMediaKeyEvent(new KeyEvent(KeyEvent.ACTION_UP, code));
                web.postDelayed(() -> session.setActive(headsetEnabled && hasWindowFocus()), 1500);
                return "muzyka: " + cmd;
            }
            default:
                return "nieznana akcja: " + type;
        }
    }

    private static String norm(String s) {
        String n = Normalizer.normalize(s == null ? "" : s, Normalizer.Form.NFD).replaceAll("\\p{M}", "");
        return n.toLowerCase(Locale.ROOT).replace("ł", "l").trim();
    }

    // --- mikrofon Bluetooth (słuchawki / zestaw w aucie) ---

    private String btMic(boolean on) {
        AudioManager am = (AudioManager) getSystemService(AUDIO_SERVICE);
        try {
            if (on) {
                am.setMode(AudioManager.MODE_IN_COMMUNICATION);
                if (Build.VERSION.SDK_INT >= 31) {
                    for (AudioDeviceInfo d : am.getAvailableCommunicationDevices()) {
                        int t = d.getType();
                        if (t == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || t == AudioDeviceInfo.TYPE_BLE_HEADSET) {
                            return am.setCommunicationDevice(d) ? "ok: " + d.getProductName() : "nie udało się";
                        }
                    }
                    am.setMode(AudioManager.MODE_NORMAL);
                    return "brak urządzenia Bluetooth";
                }
                am.startBluetoothSco();
                am.setBluetoothScoOn(true);
                return "ok";
            }
            if (Build.VERSION.SDK_INT >= 31) am.clearCommunicationDevice();
            else {
                am.setBluetoothScoOn(false);
                am.stopBluetoothSco();
            }
            am.setMode(AudioManager.MODE_NORMAL);
            return "wyłączony";
        } catch (Exception e) {
            return "błąd: " + e.getMessage();
        }
    }

    // --- wgrywanie przez USB OTG ---

    private void requestFlash(String json) {
        if (flashing) {
            flashEvent("trwa już wgrywanie", 0, false, false, "");
            return;
        }
        UsbManager usb = (UsbManager) getSystemService(USB_SERVICE);
        List<UsbSerialDriver> drivers = OtgFlasher.drivers(usb);
        if (drivers.isEmpty()) {
            flashEvent("błąd", 0, true, false, "Nie widzę płytki. Podłącz ją kablem OTG (USB-C) i spróbuj ponownie.");
            return;
        }
        UsbSerialDriver d = drivers.get(0);
        if (usb.hasPermission(d.getDevice())) {
            startFlash(json);
        } else {
            pendingFlash = json;
            Intent i = new Intent(ACTION_USB_PERMISSION).setPackage(getPackageName());
            int flags = Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0;
            usb.requestPermission(d.getDevice(), PendingIntent.getBroadcast(this, 0, i, flags));
        }
    }

    private void startFlash(String json) {
        final JSONObject job;
        try {
            job = new JSONObject(json);
        } catch (Exception e) {
            flashEvent("błąd", 0, true, false, "Złe dane wgrywania.");
            return;
        }
        UsbManager usb = (UsbManager) getSystemService(USB_SERVICE);
        List<UsbSerialDriver> drivers = OtgFlasher.drivers(usb);
        if (drivers.isEmpty()) {
            flashEvent("błąd", 0, true, false, "Płytka została odłączona.");
            return;
        }
        final UsbSerialDriver d = drivers.get(0);
        flashing = true;
        new Thread(() -> OtgFlasher.run(usb, d, job.optString("server"), job.optString("id"), job.optString("fqbn"),
                new OtgFlasher.Listener() {
                    @Override
                    public void progress(String stage, int pct) {
                        flashEvent(stage, pct, false, false, "");
                    }

                    @Override
                    public void finished(boolean ok, String message) {
                        flashing = false;
                        flashEvent(ok ? "gotowe" : "błąd", ok ? 100 : 0, true, ok, message);
                    }
                }), "otg-flash").start();
    }

    private void flashEvent(String stage, int pct, boolean done, boolean ok, String msg) {
        try {
            JSONObject o = new JSONObject().put("stage", stage).put("pct", pct).put("done", done).put("ok", ok).put("msg", msg);
            js("window.onFlash && window.onFlash(" + o + ")");
        } catch (Exception ignored) {
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
        try {
            unregisterReceiver(usbReceiver);
        } catch (Exception ignored) {
        }
        btMic(false);
        if (session != null) session.release();
        web.destroy();
        super.onDestroy();
    }
}
