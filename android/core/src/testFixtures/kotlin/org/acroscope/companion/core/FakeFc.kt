package org.acroscope.companion.core

/** A flight controller on the other end of the line: it answers the commands the downloader uses, out of a flash
 *  whose content the test sets, and misbehaves on request. */
class FakeFc(var flash: ByteArray = ByteArray(0)) : SerialPort {
    var present = true
    var ready = true
    var craft = "OttO"
    var boardInfo: ByteArray = "S474".toByteArray() + ByteArray(4) + byteArrayOf(9) + "STM32G47X".toByteArray() + byteArrayOf(14) + "BETAFPVG473_V2".toByteArray()
    var temps = mutableListOf(41)                        // one per status request, the last one repeated
    val asked = mutableListOf<String>()                  // every request, as "command" or "read@address+size"
    var noise = ByteArray(0)                             // sent before every reply
    var spoil: ((command: Int, reply: ByteArray) -> ByteArray?)? = null   // a reply changed, or dropped (null)
    var eraseAnswers = true                              // false: the erase command gets no reply
    var busyPolls = 0                                    // summaries after an erase that still say not ready
    var silentPolls = 0                                  // summaries after an erase that get no reply at all
    var eraseNeverEnds = false
    var halfDone = mutableListOf<Pair<Boolean, Long>>()   // summaries after an erase that say (ready, used) as given, before it is done
    var closed = false
    var most: Int? = null                                // a read answers at most this many bytes, whatever was asked
    var more = 0                                         // a read answers this many bytes past what was asked
    var says: Long? = null                               // the used size the summary reports, when not the flash's own
    private var incoming = ByteArray(0)
    private var outgoing = ByteArray(0)
    private var erasing = false

    override fun write(bytes: ByteArray) {
        incoming += bytes
        while (incoming.size >= 9) {
            check(String(incoming, 0, 3, Charsets.US_ASCII) == "\$X<") { "not a request" }
            val size = Msp.u16(incoming, 6)
            if (incoming.size < 9 + size) return
            check(Msp.crc8(incoming, 3, 8 + size) == (incoming[8 + size].toInt() and 0xff)) { "request with a bad checksum" }
            val command = Msp.u16(incoming, 4)
            val payload = incoming.copyOfRange(8, 8 + size)
            incoming = incoming.copyOfRange(9 + size, incoming.size)
            val reply = answer(command, payload)
            val sent = reply?.let { r -> spoil?.let { it(command, r) } ?: if (spoil == null) r else null }
            sent?.let { outgoing += noise + it }
        }
    }

    private fun answer(command: Int, payload: ByteArray): ByteArray? {
        asked += if (command == Msp.DATAFLASH_READ) "read@${Msp.u32(payload, 0)}+${Msp.u16(payload, 4)}" else command.toString()
        return when (command) {
            Msp.DATAFLASH_SUMMARY -> {
                if (erasing) {
                    if (silentPolls > 0) { silentPolls--; return null }
                    if (halfDone.isNotEmpty()) return halfDone.removeAt(0).let { summary(it.first, it.second) }
                    if (busyPolls > 0 || eraseNeverEnds) { busyPolls--; return summary(false, flash.size.toLong()) }
                    erasing = false
                    flash = ByteArray(0)
                }
                summary(ready, says ?: flash.size.toLong())
            }
            Msp.NAME -> Msp.frame(command, craft.toByteArray(), '>')
            Msp.BOARD_INFO -> Msp.frame(command, boardInfo, '>')
            Msp.STATUS -> {
                val t = if (temps.size > 1) temps.removeAt(0) else temps[0]
                Msp.frame(command, ByteArray(15) + byteArrayOf(1, 0x55, 26) + ByteArray(4) + byteArrayOf(0) + byteArrayOf(t.toByte(), (t shr 8).toByte()), '>')
            }
            Msp.DATAFLASH_READ -> {
                val at = Msp.u32(payload, 0).toInt()
                val n = minOf(Msp.u16(payload, 4) + more, most ?: Int.MAX_VALUE, flash.size - at)
                Msp.frame(command, Msp.le32(at.toLong()) + byteArrayOf(n.toByte(), (n shr 8).toByte(), 0) + flash.copyOfRange(at, at + n), '>')
            }
            Msp.DATAFLASH_ERASE -> {
                erasing = true
                if (eraseAnswers) Msp.frame(command, ByteArray(0), '>') else null
            }
            else -> Msp.frame(command, ByteArray(0), '!')
        }
    }

    private fun summary(ready: Boolean, used: Long): ByteArray {
        val flags = (if (ready) 1 else 0) or (if (present) 2 else 0)
        return Msp.frame(Msp.DATAFLASH_SUMMARY, byteArrayOf(flags.toByte()) + Msp.le32(64) + Msp.le32(16L shl 20) + Msp.le32(used), '>')
    }

    override fun read(buf: ByteArray, timeoutMs: Int): Int {
        val n = minOf(buf.size, outgoing.size, 300)      // in dribbles, as a serial line gives them
        outgoing.copyInto(buf, 0, 0, n)
        outgoing = outgoing.copyOfRange(n, outgoing.size)
        return n
    }

    override fun close() { closed = true }
}
