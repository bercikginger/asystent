package pl.asystent;

import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbManager;
import android.util.Base64;

import com.hoho.android.usbserial.driver.CdcAcmSerialDriver;
import com.hoho.android.usbserial.driver.ProbeTable;
import com.hoho.android.usbserial.driver.UsbSerialDriver;
import com.hoho.android.usbserial.driver.UsbSerialPort;
import com.hoho.android.usbserial.driver.UsbSerialProber;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.List;

/** Pobiera skompilowany program z laptopa i wgrywa go przez USB OTG. */
final class OtgFlasher {

    interface Listener {
        void progress(String stage, int pct);
        void finished(boolean ok, String message);
    }

    private static final int VID_ESPRESSIF = 0x303A;

    static List<UsbSerialDriver> drivers(UsbManager usb) {
        List<UsbSerialDriver> list = new ArrayList<>(UsbSerialProber.getDefaultProber().findAllDrivers(usb));
        ProbeTable extra = new ProbeTable();
        extra.addProduct(VID_ESPRESSIF, 0x1001, CdcAcmSerialDriver.class); // ESP32-S3/C3 natywne USB
        extra.addProduct(VID_ESPRESSIF, 0x0002, CdcAcmSerialDriver.class);
        for (UsbSerialDriver d : new UsbSerialProber(extra).findAllDrivers(usb)) {
            boolean dup = false;
            for (UsbSerialDriver e : list) dup |= e.getDevice().getDeviceName().equals(d.getDevice().getDeviceName());
            if (!dup) list.add(d);
        }
        return list;
    }

    static JSONArray describe(UsbManager usb) {
        JSONArray arr = new JSONArray();
        for (UsbSerialDriver d : drivers(usb)) {
            UsbDevice dev = d.getDevice();
            try {
                arr.put(new JSONObject()
                        .put("name", dev.getProductName() != null ? dev.getProductName() : dev.getDeviceName())
                        .put("vid", dev.getVendorId()).put("pid", dev.getProductId())
                        .put("driver", d.getClass().getSimpleName().replace("SerialDriver", "")));
            } catch (Exception ignored) {
            }
        }
        return arr;
    }

    /** Uruchamiać w wątku w tle; urządzenie musi mieć już zgodę na dostęp. */
    static void run(UsbManager usb, UsbSerialDriver driver, String server, String sketchId, String fqbn, Listener l) {
        UsbSerialPort port = null;
        try {
            l.progress("kompiluję na laptopie…", 0);
            JSONObject fw = download(server + "api/arduino/firmware/" + sketchId + "?fqbn=" + URLEncoder.encode(fqbn, "UTF-8"));
            if (!fw.optBoolean("ok")) {
                l.finished(false, fw.optString("log", "Laptop nie przygotował programu."));
                return;
            }
            String chip = fw.getString("chip");
            int baud = fw.optInt("baud", 115200);
            JSONArray parts = fw.getJSONArray("parts");
            l.progress(String.format("pobrano %d KB", fw.optInt("size") / 1024), 3);

            UsbDeviceConnection conn = usb.openDevice(driver.getDevice());
            if (conn == null) {
                l.finished(false, "Nie mogę otworzyć urządzenia USB.");
                return;
            }
            port = driver.getPorts().get(0);
            port.open(conn);
            SerialIO io = new SerialIO(port);

            if ("avr".equals(chip)) {
                byte[] image = Base64.decode(parts.getJSONObject(0).getString("data"), Base64.DEFAULT);
                new AvrFlasher(io).flash(baud, image, (s, p) -> l.progress(s, 5 + p * 95 / 100));
            } else {
                List<EspFlasher.Part> list = new ArrayList<>();
                for (int i = 0; i < parts.length(); i++) {
                    JSONObject o = parts.getJSONObject(i);
                    list.add(new EspFlasher.Part(o.getInt("offset"), Base64.decode(o.getString("data"), Base64.DEFAULT)));
                }
                boolean jtag = driver.getDevice().getVendorId() == VID_ESPRESSIF;
                new EspFlasher(io, chip, jtag).flash(list, (s, p) -> l.progress(s, 5 + p * 95 / 100));
            }
            l.finished(true, "Program wgrany i uruchomiony.");
        } catch (Exception e) {
            l.finished(false, e.getMessage() != null ? e.getMessage() : e.toString());
        } finally {
            if (port != null) {
                try {
                    port.close();
                } catch (Exception ignored) {
                }
            }
        }
    }

    private static JSONObject download(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(15000);
        c.setReadTimeout(20 * 60 * 1000); // pierwsza kompilacja ESP32 potrafi trwać kilka minut
        int code = c.getResponseCode();
        InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[65536];
        int n;
        while (in != null && (n = in.read(buf)) > 0) out.write(buf, 0, n);
        c.disconnect();
        String body = out.toString("UTF-8");
        try {
            return new JSONObject(body);
        } catch (Exception e) {
            return new JSONObject().put("ok", false).put("log", "Laptop odpowiedział " + code + ".");
        }
    }
}
