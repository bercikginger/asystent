package pl.asystent;

import com.hoho.android.usbserial.driver.UsbSerialPort;

import java.io.IOException;

/** Buforowany odczyt/zapis portu USB-serial z limitami czasu. */
final class SerialIO {
    final UsbSerialPort port;
    private final byte[] buf = new byte[16384];
    private int pos = 0, len = 0;

    SerialIO(UsbSerialPort port) {
        this.port = port;
    }

    void write(byte[] data) throws IOException {
        port.write(data, 5000);
    }

    /** Jeden bajt albo -1 po przekroczeniu czasu. */
    int readByte(int timeoutMs) throws IOException {
        if (pos < len) return buf[pos++] & 0xff;
        long end = System.currentTimeMillis() + timeoutMs;
        while (true) {
            long left = end - System.currentTimeMillis();
            if (left <= 0) return -1;
            int n = port.read(buf, (int) Math.max(1, Math.min(200, left)));
            if (n > 0) {
                pos = 0;
                len = n;
                return buf[pos++] & 0xff;
            }
        }
    }

    byte[] readExact(int n, int timeoutMs) throws IOException {
        byte[] out = new byte[n];
        long end = System.currentTimeMillis() + timeoutMs;
        for (int i = 0; i < n; i++) {
            int b = readByte((int) Math.max(1, end - System.currentTimeMillis()));
            if (b < 0) throw new IOException("Brak odpowiedzi płytki (timeout).");
            out[i] = (byte) b;
        }
        return out;
    }

    /** Wyrzuca wszystko, co czeka w buforach. */
    void drain() {
        pos = len = 0;
        try {
            byte[] tmp = new byte[16384];
            long end = System.currentTimeMillis() + 300;
            while (System.currentTimeMillis() < end && port.read(tmp, 20) > 0) { /* pomiń */ }
        } catch (IOException ignored) {
        }
    }

    static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
        }
    }
}
