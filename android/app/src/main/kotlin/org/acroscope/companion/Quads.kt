package org.acroscope.companion

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.os.SystemClock
import org.acroscope.companion.core.SerialPort
import org.acroscope.companion.core.SerialPorts
import java.io.IOException

/** The quad as the phone has it on USB: none, one the app may not talk to yet, or one it may. */
sealed interface QuadState {
    data object None : QuadState
    /** A flight controller is plugged in; `ask` puts Android's question to the pilot. */
    data class NeedsPermission(val name: String, val ask: () -> Unit) : QuadState
    /** `id` tells one plugged-in device from another (Android's name for it): a quad swapped while the app was not
     *  looking is another quad, though it calls itself the same. */
    data class Ready(val name: String, val id: String = name) : QuadState
}

interface Quads {
    fun state(): QuadState
    /** The serial line to the flight controller that is plugged in. */
    fun open(): SerialPort
}

/** Flight controllers through Android's USB host API: the first plugged-in device that has a serial port. */
class UsbQuads(private val context: Context) : Quads {
    companion object {
        const val PERMISSION = "org.acroscope.companion.USB_PERMISSION"
    }

    private val usb = context.getSystemService(UsbManager::class.java)

    private fun controller(): UsbDevice? = usb.deviceList.values.firstOrNull { UsbSerial.interfaces(it) != null }

    override fun state(): QuadState {
        val device = controller() ?: return QuadState.None
        val name = device.productName ?: "flight controller"
        if (usb.hasPermission(device)) return QuadState.Ready(name, device.deviceName)
        return QuadState.NeedsPermission(name) {
            val answer = PendingIntent.getBroadcast(context, 0, Intent(PERMISSION).setPackage(context.packageName), PendingIntent.FLAG_MUTABLE)
            usb.requestPermission(device, answer)
        }
    }

    override fun open(): SerialPort = UsbSerial.open(usb, controller() ?: throw IOException("no flight controller is plugged in"))
}

/**
 * A flight controller's USB serial port (CDC-ACM), spoken to directly: claim its interfaces, set the line, then
 * bulk transfers in and out. Only one app can hold it at a time.
 */
class UsbSerial private constructor(
    private val connection: UsbDeviceConnection,
    private val claimed: List<UsbInterface>,
    private val input: UsbEndpoint,
    private val output: UsbEndpoint,
) : SerialPort {
    companion object {
        /** A device's serial data interface, with the control interface that goes with it; null when it has none. */
        fun interfaces(device: UsbDevice): Pair<UsbInterface?, UsbInterface>? {
            val all = (0 until device.interfaceCount).map(device::getInterface)
            val faces = all.map { SerialPorts.Face(it.interfaceClass, it.interfaceSubclass) }
            val (control, data) = SerialPorts.find(device.vendorId, device.productId, faces) ?: return null
            return control?.let(all::get) to all[data]
        }

        fun open(usb: UsbManager, device: UsbDevice): UsbSerial {
            val (control, data) = interfaces(device) ?: throw IOException("this is no flight controller's serial port")
            val ends = (0 until data.endpointCount).map(data::getEndpoint).filter { it.type == UsbConstants.USB_ENDPOINT_XFER_BULK }
            val input = ends.firstOrNull { it.direction == UsbConstants.USB_DIR_IN }
            val output = ends.firstOrNull { it.direction == UsbConstants.USB_DIR_OUT }
            if (input == null || output == null) throw IOException("the flight controller's serial port has no way in or out")
            val connection = usb.openDevice(device) ?: throw IOException("the flight controller could not be opened")
            val claimed = listOfNotNull(control, data)
            for (face in claimed) {
                if (!connection.claimInterface(face, true)) {
                    connection.close()
                    throw IOException("another app holds the flight controller: close the Betaflight app and try again")
                }
            }
            // 115200 8N1 (a USB serial port has no speed, but the request is expected), then DTR and RTS up: "someone is listening"
            val line = byteArrayOf(0x00, 0xC2.toByte(), 0x01, 0x00, 0, 0, 8)
            connection.controlTransfer(0x21, 0x20, 0, control?.id ?: 0, line, line.size, 1000)
            connection.controlTransfer(0x21, 0x22, 0x03, control?.id ?: 0, null, 0, 1000)
            return UsbSerial(connection, claimed, input, output)
        }
    }

    override fun write(bytes: ByteArray) {
        var at = 0
        while (at < bytes.size) {
            val n = connection.bulkTransfer(output, bytes, at, bytes.size - at, 2000)
            if (n <= 0) throw IOException("the flight controller takes nothing: is it still plugged in?")
            at += n
        }
    }

    /** A transfer that fails at once is a cable pulled; one that fails after its time is only silence. */
    override fun read(buf: ByteArray, timeoutMs: Int): Int {
        val started = SystemClock.elapsedRealtime()
        val n = connection.bulkTransfer(input, buf, buf.size, timeoutMs)
        if (n >= 0) return n
        if (SystemClock.elapsedRealtime() - started < timeoutMs / 2) throw IOException("the flight controller is gone")
        return 0
    }

    override fun close() {
        claimed.forEach(connection::releaseInterface)
        connection.close()
    }
}
