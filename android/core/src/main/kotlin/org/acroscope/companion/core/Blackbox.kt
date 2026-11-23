package org.acroscope.companion.core

import java.io.File
import java.io.IOException
import java.time.ZonedDateTime

/** The quad's flash: whether it has one, whether it answers, how much of it is written. */
data class Flash(val present: Boolean, val ready: Boolean, val used: Long)

/** A log saved off a quad: the file (named as the aligner reads it), and how warm the MCU got on the way. */
data class SavedLog(val file: File, val peakTemp: Int?)

/** Where a download is: bytes saved of the bytes used, and the MCU's core temperature when the firmware tells. */
data class FlashProgress(val done: Long, val total: Long, val temp: Int?)

/**
 * The blackbox log read off a quad over USB, as the Configurator's "save flash to file" does it: only the used
 * part of the flash, into a file named `BTFL_BLACKBOX_LOG_<CRAFT>_<date>_<time>_<board>.bbl` (the aligner orders
 * logs by that time, the phone's at the download). The flash is erased only on request, and only after a download
 * that was verified: the file as long as the flash said, and starting like a log.
 */
class Blackbox(port: SerialPort, private val clock: () -> ZonedDateTime, private val pause: (Long) -> Unit = Thread::sleep, private val now: () -> Long = System::currentTimeMillis) {
    private val link = MspLink(port)
    private var verified = false

    companion object {
        const val PIECE = 4096
        private val MAGIC = "H Product:Blackbox".toByteArray()
    }

    fun flash(): Flash {
        val s = link.request(Msp.DATAFLASH_SUMMARY)
        if (s.size < 13) throw IOException("a flash summary of ${s.size} bytes")
        val flags = s[0].toInt()
        return Flash(present = flags and 2 != 0, ready = flags and 1 != 0, used = Msp.u32(s, 9))
    }

    /**
     * Save the used part of the flash into `dir`. Null when there is nothing on it. The file is written under a
     * `.part` name and renamed once it is as long as the flash said and starts like a log; anything else leaves no
     * file and the flash as it was. `progress` hears the bytes saved and the core temperature, read once a second:
     * the board sits on USB power with no air moving over it.
     */
    fun download(dir: File, progress: (FlashProgress) -> Unit = {}, stop: () -> Boolean = { false }): SavedLog? {
        verified = false
        val flash = flash()
        if (!flash.present) throw IOException("this flight controller has no flash for logs")
        if (flash.used == 0L) return null
        val craft = String(link.request(Msp.NAME), Charsets.UTF_8)
        val board = Msp.boardName(link.request(Msp.BOARD_INFO))
        val file = File(dir, Names.log(craft, board, clock().toLocalDateTime()))
        val part = File(dir, file.name + ".part")
        dir.mkdirs()
        var temp: Int? = null
        var peak: Int? = null
        var polled = 0L
        try {
            part.outputStream().use { out ->
                var at = 0L
                while (at < flash.used) {
                    if (stop()) throw IOException("stopped")
                    if (now() - polled >= 1000) {
                        polled = now()
                        temp = Msp.coreTemp(link.request(Msp.STATUS))
                        temp?.let { t -> peak = maxOf(peak ?: t, t) }
                    }
                    val want = minOf(PIECE.toLong(), flash.used - at).toInt()
                    val r = link.request(Msp.DATAFLASH_READ, Msp.le32(at) + byteArrayOf(want.toByte(), (want shr 8).toByte(), 0))   // 0: not compressed
                    val n = if (r.size >= 7) Msp.u16(r, 4) else 0
                    if (r.size < 7 || Msp.u32(r, 0) != at || r[6].toInt() != 0 || n == 0 || r.size < 7 + n) throw IOException("a bad flash read at $at")
                    out.write(r, 7, n)
                    at += n
                    progress(FlashProgress(at, flash.used, temp))
                }
                out.fd.sync()
            }
            val head = part.inputStream().use { it.readNBytes(MAGIC.size) }
            if (part.length() != flash.used || !head.contentEquals(MAGIC)) throw IOException("what came off the flash is not a whole log; the flash was not erased")
            if (!part.renameTo(file)) throw IOException("${file.name}: could not be put in place")
        } finally {
            part.delete()
        }
        verified = true
        return SavedLog(file, peak)
    }

    /**
     * Erase the flash: only after a download on this connection was verified. The flight controller blocks while
     * the chip erases and answers afterwards, or not at all; either way the summary is asked until it says ready
     * and empty, for up to five minutes.
     */
    fun erase() {
        check(verified) { "the flash is erased only after its log was saved and verified" }
        try {
            link.request(Msp.DATAFLASH_ERASE, timeoutMs = 120_000)
        } catch (e: MspTimeout) {
            // some chips keep the controller busy: the summary below decides
        }
        val deadline = now() + 300_000
        while (now() < deadline) {
            pause(1000)
            val flash = try { flash() } catch (e: MspTimeout) { continue }
            if (flash.ready && flash.used == 0L) {
                verified = false
                return
            }
        }
        throw IOException("the erase did not finish within five minutes")
    }
}
