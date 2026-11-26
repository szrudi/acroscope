package org.acroscope.companion.core

import java.io.Closeable
import java.io.IOException

/** A serial line to a flight controller: bytes out, and bytes in with a patience. */
interface SerialPort : Closeable {
    fun write(bytes: ByteArray)
    /** Up to `buf.size` bytes into `buf`, waiting at most `timeoutMs`; 0 when nothing came. */
    fun read(buf: ByteArray, timeoutMs: Int): Int
}

/** The flight controller said no to a command (`$X!`). */
class MspRejected(val command: Int) : IOException("the flight controller rejected MSP command $command")

/** Nothing came from the flight controller in time. */
class MspTimeout : IOException("the flight controller stopped answering")

/** The flight controller took nothing for a while but is still there: busy, as while its flash chip erases. */
class SerialBusy : IOException("the flight controller takes nothing for now")

/** The line to the flight controller is gone: unplugged, or it dropped off USB to restart. */
class SerialGone(message: String) : IOException(message)

/**
 * MSP v2 as Betaflight speaks it over USB: `$X<`, a flag byte, the command and the payload's size (u16, little
 * endian), the payload, and a CRC-8/DVB-S2 over everything after the direction byte. Replies are `$X>` (fine) or
 * `$X!` (rejected). A port of the laptop's downloader (`bf-blackbox.py`), vector for vector.
 */
object Msp {
    const val BOARD_INFO = 4
    const val NAME = 10
    const val DATAFLASH_SUMMARY = 70
    const val DATAFLASH_READ = 71
    const val DATAFLASH_ERASE = 72
    const val STATUS = 101

    /** CRC-8/DVB-S2 (polynomial 0xD5); its check value for "123456789" is 0xBC. */
    fun crc8(data: ByteArray, from: Int = 0, to: Int = data.size): Int {
        var crc = 0
        for (i in from until to) {
            crc = crc xor (data[i].toInt() and 0xff)
            repeat(8) { crc = if (crc and 0x80 != 0) ((crc shl 1) xor 0xD5) and 0xff else (crc shl 1) and 0xff }
        }
        return crc
    }

    fun frame(command: Int, payload: ByteArray = ByteArray(0), direction: Char = '<'): ByteArray {
        val body = byteArrayOf(0, command.toByte(), (command shr 8).toByte(), payload.size.toByte(), (payload.size shr 8).toByte()) + payload
        return "\$X$direction".toByteArray(Charsets.US_ASCII) + body + crc8(body).toByte()
    }

    fun u16(b: ByteArray, at: Int): Int = (b[at].toInt() and 0xff) or ((b[at + 1].toInt() and 0xff) shl 8)

    fun u32(b: ByteArray, at: Int): Long = (0..3).fold(0L) { v, i -> v or ((b[at + i].toLong() and 0xff) shl (8 * i)) }

    fun le32(v: Long): ByteArray = ByteArray(4) { (v shr (8 * it)).toByte() }

    /** The MCU's core temperature from an MSP_STATUS reply: its last field since MSP API 1.46 (Betaflight 4.5), after
     *  the extra flight mode flag bytes (their count at 15), the arming flags and the config state. Null when the
     *  firmware is older, and for 0: an MCU without the sensor says that. */
    fun coreTemp(status: ByteArray): Int? {
        var p = 15
        if (status.size <= p) return null
        p += 1 + (status[p].toInt() and 0xff) + 1 + 4 + 1
        if (status.size < p + 2) return null
        val t = u16(status, p).toShort().toInt()
        return if (t == 0) null else t
    }

    /** The board's name from MSP_BOARD_INFO: after a 4-byte identifier, the hardware revision (u16) and two bytes,
     *  the length-prefixed target name and board name; the target when the board has no name. */
    fun boardName(info: ByteArray): String {
        var p = 8
        val target = String(info, p + 1, info[p].toInt() and 0xff, Charsets.US_ASCII)
        p += 1 + (info[p].toInt() and 0xff)
        val board = if (p < info.size) String(info, p + 1, info[p].toInt() and 0xff, Charsets.US_ASCII) else ""
        return board.ifEmpty { target }
    }
}

/** Requests and their replies on a serial line: noise and stale replies are passed over until the command matches. */
class MspLink(private val port: SerialPort) {
    private var buf = ByteArray(0)

    /** Wait until `n` bytes are in hand. */
    private fun need(n: Int, timeoutMs: Int) {
        val chunk = ByteArray(8192)
        while (buf.size < n) {
            val got = port.read(chunk, timeoutMs)
            if (got <= 0) throw MspTimeout()
            buf += chunk.copyOf(got)
        }
    }

    private fun drop(n: Int) {
        buf = buf.copyOfRange(n, buf.size)
    }

    fun request(command: Int, payload: ByteArray = ByteArray(0), timeoutMs: Int = 2000): ByteArray {
        port.write(Msp.frame(command, payload))
        while (true) {
            need(3, timeoutMs)
            // not the start of a reply: one byte on, so a `$` that is noise does not take the real one behind it along
            if (buf[0] != '$'.code.toByte() || buf[1] != 'X'.code.toByte() || (buf[2] != '>'.code.toByte() && buf[2] != '!'.code.toByte())) {
                drop(1)
                continue
            }
            need(8, timeoutMs)
            val size = Msp.u16(buf, 6)
            need(9 + size, timeoutMs)
            val rejected = buf[2] == '!'.code.toByte()
            val answered = Msp.u16(buf, 4)
            val data = buf.copyOfRange(8, 8 + size)
            val sum = buf[8 + size].toInt() and 0xff
            val good = sum == Msp.crc8(buf, 3, 8 + size)
            drop(9 + size)
            if (!good) throw IOException("MSP checksum mismatch")
            if (answered != command) continue                                   // a reply to something asked earlier
            if (rejected) throw MspRejected(command)
            return data
        }
    }
}
