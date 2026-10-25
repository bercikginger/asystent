package pl.asystent;

import com.hoho.android.usbserial.driver.UsbSerialPort;

import java.io.IOException;
import java.util.Arrays;

/**
 * Wgrywanie na Arduino Uno / Nano (ATmega328P) przez bootloader STK500v1 (Optiboot i stary ATmegaBOOT).
 * To samo, co robi avrdude z programatorem "arduino".
 */
final class AvrFlasher {
    private static final int STK_OK = 0x10, STK_INSYNC = 0x14, CRC_EOP = 0x20;
    private static final int PAGE = 128;

    interface Progress {
        void update(String stage, int pct);
    }

    private final SerialIO io;

    AvrFlasher(SerialIO io) {
        this.io = io;
    }

    void flash(int baud, byte[] image, Progress p) throws IOException {
        UsbSerialPort port = io.port;
        port.setParameters(baud, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE);

        p.update("reset płytki…", 0);
        boolean synced = false;
        for (int attempt = 0; attempt < 3 && !synced; attempt++) {
            reset(port);
            synced = sync();
        }
        if (!synced) {
            throw new IOException("Bootloader nie odpowiada. Sprawdź kabel i wybraną płytkę "
                    + "(dla klonów Nano spróbuj „stary bootloader”).");
        }
        command(new byte[]{0x50, CRC_EOP}, 0, 1000); // wejście w tryb programowania

        try {
            byte[] sig = command(new byte[]{0x75, CRC_EOP}, 3, 1000);
            p.update(String.format("sygnatura %02X %02X %02X", sig[0], sig[1], sig[2]), 2);
        } catch (IOException ignored) {
            // nie każdy bootloader to obsługuje
        }

        int size = ((image.length + PAGE - 1) / PAGE) * PAGE;
        byte[] img = Arrays.copyOf(image, size);
        Arrays.fill(img, image.length, size, (byte) 0xFF);

        for (int addr = 0; addr < size; addr += PAGE) {
            loadAddress(addr);
            byte[] cmd = new byte[5 + PAGE];
            cmd[0] = 0x64;
            cmd[1] = (byte) (PAGE >> 8);
            cmd[2] = (byte) (PAGE & 0xff);
            cmd[3] = 'F';
            System.arraycopy(img, addr, cmd, 4, PAGE);
            cmd[4 + PAGE] = CRC_EOP;
            command(cmd, 0, 2000);
            p.update("wgrywam…", 2 + (addr + PAGE) * 78 / size);
        }

        for (int addr = 0; addr < size; addr += PAGE) {
            loadAddress(addr);
            byte[] got = command(new byte[]{0x74, (byte) (PAGE >> 8), (byte) (PAGE & 0xff), 'F', CRC_EOP}, PAGE, 2000);
            for (int i = 0; i < PAGE; i++) {
                if (got[i] != img[addr + i]) {
                    throw new IOException(String.format("Weryfikacja nie przeszła pod adresem 0x%04X.", addr + i));
                }
            }
            p.update("weryfikuję…", 80 + (addr + PAGE) * 19 / size);
        }

        command(new byte[]{0x51, CRC_EOP}, 0, 1000); // wyjście – program startuje
        p.update("gotowe", 100);
    }

    private static void reset(UsbSerialPort port) throws IOException {
        port.setDTR(false);
        port.setRTS(false);
        SerialIO.sleep(250);
        port.setDTR(true);
        port.setRTS(true);
        SerialIO.sleep(50);
    }

    private boolean sync() throws IOException {
        for (int i = 0; i < 10; i++) {
            io.drain();
            io.write(new byte[]{0x30, CRC_EOP});
            int a = io.readByte(300);
            if (a == STK_INSYNC && io.readByte(200) == STK_OK) {
                io.drain();
                return true;
            }
        }
        return false;
    }

    private void loadAddress(int byteAddr) throws IOException {
        int word = byteAddr / 2;
        command(new byte[]{0x55, (byte) (word & 0xff), (byte) ((word >> 8) & 0xff), CRC_EOP}, 0, 1000);
    }

    /** Wysyła komendę i zwraca dane między INSYNC a OK. */
    private byte[] command(byte[] cmd, int dataLen, int timeoutMs) throws IOException {
        io.write(cmd);
        int a = io.readByte(timeoutMs);
        if (a != STK_INSYNC) throw new IOException("Płytka zgubiła synchronizację (0x" + Integer.toHexString(a) + ").");
        byte[] data = dataLen > 0 ? io.readExact(dataLen, timeoutMs) : new byte[0];
        if (io.readByte(timeoutMs) != STK_OK) throw new IOException("Płytka odrzuciła komendę 0x" + Integer.toHexString(cmd[0] & 0xff) + ".");
        return data;
    }
}
