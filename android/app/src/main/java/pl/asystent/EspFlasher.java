package pl.asystent;

import com.hoho.android.usbserial.driver.UsbSerialPort;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;
import java.util.List;

/**
 * Wgrywanie na ESP32 / ESP32-S3 / ESP32-C3 / ESP8266 przez bootloader w ROM (protokół esptool, SLIP).
 */
final class EspFlasher {
    private static final int FLASH_BEGIN = 0x02, FLASH_DATA = 0x03, SYNC = 0x08, READ_REG = 0x0A,
            SPI_SET_PARAMS = 0x0B, SPI_ATTACH = 0x0D, CHANGE_BAUD = 0x0F;
    private static final int BLOCK = 0x400; // rozmiar bloku dla loadera w ROM
    private static final int FAST_BAUD = 460800;

    interface Progress {
        void update(String stage, int pct);
    }

    static final class Part {
        final int offset;
        final byte[] data;

        Part(int offset, byte[] data) {
            this.offset = offset;
            this.data = data;
        }
    }

    private final SerialIO io;
    private final String chip;
    private final boolean usbJtag;   // natywne USB w ESP32-S3/C3 (VID 0x303A)
    private final int statusLen;

    EspFlasher(SerialIO io, String chip, boolean usbJtag) {
        this.io = io;
        this.chip = chip;
        this.usbJtag = usbJtag;
        this.statusLen = "esp8266".equals(chip) ? 2 : 4;
    }

    void flash(List<Part> parts, Progress p) throws IOException {
        p.update("wejście w bootloader…", 0);
        connect(115200);

        if (!"esp8266".equals(chip) && !usbJtag) {
            try {
                command(CHANGE_BAUD, le(FAST_BAUD, 0), 0, 3000);
                io.port.setParameters(FAST_BAUD, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE);
                SerialIO.sleep(50);
                io.drain();
                command(READ_REG, le(0x40001000), 0, 1000); // test łącza po zmianie prędkości
                p.update("szybkie łącze 460800", 2);
            } catch (IOException e) {
                p.update("wolne łącze 115200", 2);
                connect(115200);
            }
        }

        long total = 0, done = 0;
        for (Part part : parts) total += part.data.length;

        for (Part part : parts) {
            int size = part.data.length;
            int blocks = (size + BLOCK - 1) / BLOCK;
            int erase = "esp8266".equals(chip) ? esp8266EraseSize(part.offset, size) : size;
            byte[] begin = "esp32".equals(chip) || "esp8266".equals(chip)
                    ? le(erase, blocks, BLOCK, part.offset)
                    : le(erase, blocks, BLOCK, part.offset, 0);
            p.update(String.format("kasuję 0x%X…", part.offset), (int) (done * 100 / total));
            command(FLASH_BEGIN, begin, 0, 5000 + (int) (size * 40000L / (1024 * 1024)));

            for (int seq = 0; seq < blocks; seq++) {
                byte[] block = new byte[BLOCK];
                Arrays.fill(block, (byte) 0xFF);
                int from = seq * BLOCK, n = Math.min(BLOCK, size - from);
                System.arraycopy(part.data, from, block, 0, n);
                ByteBuffer pkt = ByteBuffer.allocate(16 + BLOCK).order(ByteOrder.LITTLE_ENDIAN);
                pkt.putInt(BLOCK).putInt(seq).putInt(0).putInt(0).put(block);
                command(FLASH_DATA, pkt.array(), checksum(block), 5000);
                done += n;
                p.update("wgrywam…", (int) (done * 99 / total));
            }
        }

        p.update("restart płytki…", 99);
        hardReset();
        p.update("gotowe", 100);
    }

    // --- połączenie ---

    private void connect(int baud) throws IOException {
        io.port.setParameters(baud, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE);
        for (int attempt = 0; attempt < 4; attempt++) {
            if (usbJtag) resetUsbJtag();
            else resetClassic(attempt % 2 == 0 ? 50 : 500);
            if (sync()) {
                if (!"esp8266".equals(chip)) {
                    command(SPI_ATTACH, new byte[8], 0, 3000);
                    command(SPI_SET_PARAMS, le(0, 4 * 1024 * 1024, 64 * 1024, 4096, 256, 0xFFFF), 0, 3000);
                }
                return;
            }
        }
        throw new IOException("ESP nie wchodzi w bootloader. Przytrzymaj przycisk BOOT na płytce i spróbuj ponownie.");
    }

    private void resetClassic(int delayMs) throws IOException {
        // jak esptool: EN (RTS) w dół, IO0 (DTR) w dół, puszczenie EN
        io.port.setDTR(false);
        io.port.setRTS(true);
        SerialIO.sleep(100);
        io.port.setDTR(true);
        io.port.setRTS(false);
        SerialIO.sleep(delayMs);
        io.port.setDTR(false);
    }

    private void resetUsbJtag() throws IOException {
        io.port.setRTS(false);
        io.port.setDTR(false);
        SerialIO.sleep(100);
        io.port.setDTR(true);
        io.port.setRTS(false);
        SerialIO.sleep(100);
        io.port.setRTS(true);
        io.port.setDTR(false);
        io.port.setRTS(true);
        SerialIO.sleep(100);
        io.port.setRTS(false);
        io.port.setDTR(false);
    }

    private void hardReset() {
        try {
            io.port.setDTR(false);
            io.port.setRTS(true);
            SerialIO.sleep(usbJtag ? 200 : 100);
            io.port.setRTS(false);
        } catch (IOException ignored) {
        }
    }

    private boolean sync() throws IOException {
        byte[] data = new byte[36];
        data[0] = 0x07;
        data[1] = 0x07;
        data[2] = 0x12;
        data[3] = 0x20;
        Arrays.fill(data, 4, 36, (byte) 0x55);
        for (int i = 0; i < 7; i++) {
            io.drain();
            try {
                command(SYNC, data, 0, 200);
                // ROM odsyła kilka odpowiedzi na SYNC – wyrzuć resztę
                long end = System.currentTimeMillis() + 150;
                while (System.currentTimeMillis() < end) {
                    if (readPacket(50) == null) break;
                }
                io.drain();
                return true;
            } catch (IOException ignored) {
            }
        }
        return false;
    }

    // --- protokół ---

    private byte[] command(int op, byte[] data, int chk, int timeoutMs) throws IOException {
        ByteBuffer pkt = ByteBuffer.allocate(8 + data.length).order(ByteOrder.LITTLE_ENDIAN);
        pkt.put((byte) 0x00).put((byte) op).putShort((short) data.length).putInt(chk).put(data);
        io.write(slip(pkt.array()));

        long end = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < end) {
            byte[] r = readPacket((int) Math.max(1, end - System.currentTimeMillis()));
            if (r == null) break;
            if (r.length < 8 || r[0] != 0x01 || (r[1] & 0xff) != op) continue;
            byte[] body = Arrays.copyOfRange(r, 8, r.length);
            if (body.length >= statusLen && body[body.length - statusLen] != 0) {
                throw new IOException(String.format("ESP zgłosił błąd 0x%02X przy komendzie 0x%02X.",
                        body[body.length - statusLen + 1], op));
            }
            return body;
        }
        throw new IOException(String.format("Brak odpowiedzi ESP na komendę 0x%02X.", op));
    }

    /** Odczyt jednej ramki SLIP albo null po czasie. */
    private byte[] readPacket(int timeoutMs) throws IOException {
        long end = System.currentTimeMillis() + timeoutMs;
        int b;
        do {
            b = io.readByte((int) Math.max(1, end - System.currentTimeMillis()));
            if (b < 0) return null;
        } while (b != 0xC0);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        while (true) {
            b = io.readByte((int) Math.max(1, end - System.currentTimeMillis()) + 100);
            if (b < 0) return null;
            if (b == 0xC0) {
                if (out.size() == 0) continue; // pusta ramka – początek następnej
                return out.toByteArray();
            }
            if (b == 0xDB) {
                int e = io.readByte(200);
                b = e == 0xDC ? 0xC0 : e == 0xDD ? 0xDB : e;
            }
            out.write(b);
        }
    }

    private static byte[] slip(byte[] data) {
        ByteArrayOutputStream out = new ByteArrayOutputStream(data.length + 16);
        out.write(0xC0);
        for (byte x : data) {
            int b = x & 0xff;
            if (b == 0xC0) {
                out.write(0xDB);
                out.write(0xDC);
            } else if (b == 0xDB) {
                out.write(0xDB);
                out.write(0xDD);
            } else {
                out.write(b);
            }
        }
        out.write(0xC0);
        return out.toByteArray();
    }

    private static int checksum(byte[] data) {
        int c = 0xEF;
        for (byte b : data) c ^= (b & 0xff);
        return c;
    }

    private static byte[] le(int... v) {
        ByteBuffer b = ByteBuffer.allocate(v.length * 4).order(ByteOrder.LITTLE_ENDIAN);
        for (int x : v) b.putInt(x);
        return b.array();
    }

    /** Obejście błędu kasowania w ROM ESP8266 (jak w esptool). */
    private static int esp8266EraseSize(int offset, int size) {
        int sectorsPerBlock = 16, sector = 4096;
        int numSectors = (size + sector - 1) / sector;
        int startSector = offset / sector;
        int head = sectorsPerBlock - (startSector % sectorsPerBlock);
        if (numSectors < head) head = numSectors;
        if (numSectors < 2 * head) return (numSectors + 1) / 2 * sector;
        return (numSectors - head) * sector;
    }
}
