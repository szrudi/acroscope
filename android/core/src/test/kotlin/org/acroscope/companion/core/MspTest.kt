package org.acroscope.companion.core

import java.io.IOException
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull

/** MSP as the laptop's downloader speaks it: its own test vectors, and frames it produced. */
class MspTest {
    private fun hex(s: String) = s.chunked(2).map { it.toInt(16).toByte() }.toByteArray()

    /** A line that plays back what a controller would send. */
    private class Line(private var out: ByteArray) : SerialPort {
        val written = mutableListOf<ByteArray>()
        override fun write(bytes: ByteArray) { written += bytes }
        override fun read(buf: ByteArray, timeoutMs: Int): Int {
            val n = minOf(buf.size, out.size, 5)
            out.copyInto(buf, 0, 0, n)
            out = out.copyOfRange(n, out.size)
            return n
        }
        override fun close() {}
    }

    @Test
    fun `the checksum is crc-8 dvb-s2`() {
        assertEquals(0xBC, Msp.crc8("123456789".toByteArray()))
    }

    @Test
    fun `frames are byte for byte the downloader's`() {
        assertContentEquals(hex("24583c000a000000dd"), Msp.frame(Msp.NAME))
        assertContentEquals(hex("24583c004700070000100000001000c7"), Msp.frame(Msp.DATAFLASH_READ, Msp.le32(4096) + byteArrayOf(0, 0x10, 0)))
        assertContentEquals(hex("24583e000a0004004f74744f2a"), Msp.frame(Msp.NAME, "OttO".toByteArray(), '>'))
        assertContentEquals(hex("245821004800000090"), Msp.frame(Msp.DATAFLASH_ERASE, direction = '!'))
    }

    @Test
    fun `a reply is found after noise and a stale reply`() {
        val stale = Msp.frame(Msp.BOARD_INFO, "x".toByteArray(), '>')
        val line = Line("junk\$X>".toByteArray() + stale.copyOfRange(3, stale.size) + Msp.frame(Msp.NAME, "OttO".toByteArray(), '>'))
        assertEquals("OttO", String(MspLink(line).request(Msp.NAME)))
        assertContentEquals(Msp.frame(Msp.NAME), line.written.single())
    }

    @Test
    fun `a stray start of a frame right before a reply does not take the reply along`() {
        for (noise in listOf("\$", "\$X", "\$\$X", "\$X\$", "x\$X?", "\$Xjunk\$", "\$?>", "\$Y!", "\$\$>")) {
            val line = Line(noise.toByteArray() + Msp.frame(Msp.NAME, "OttO".toByteArray(), '>'))
            assertEquals("OttO", String(MspLink(line).request(Msp.NAME)), noise)
        }
    }

    @Test
    fun `a broken or refused reply, and silence, are told apart`() {
        val good = Msp.frame(Msp.NAME, "OttO".toByteArray(), '>')
        val broken = good.copyOf().also { it[9] = 'x'.code.toByte() }
        assertEquals("MSP checksum mismatch", assertFailsWith<IOException> { MspLink(Line(broken)).request(Msp.NAME) }.message)
        assertEquals(Msp.DATAFLASH_ERASE, assertFailsWith<MspRejected> { MspLink(Line(Msp.frame(Msp.DATAFLASH_ERASE, direction = '!'))).request(Msp.DATAFLASH_ERASE) }.command)
        assertFailsWith<MspTimeout> { MspLink(Line(ByteArray(0))).request(Msp.NAME) }
        assertFailsWith<MspTimeout> { MspLink(Line(good.copyOf(good.size - 2))).request(Msp.NAME) }        // cut off in the middle
        assertFailsWith<MspTimeout> { MspLink(Line(Msp.frame(Msp.STATUS, ByteArray(3), '>'))).request(Msp.NAME) }   // only an answer to something else
    }

    @Test
    fun `the board's name, or its target when it has none`() {
        val info = "S474".toByteArray() + ByteArray(4) + byteArrayOf(9) + "STM32G47X".toByteArray() + byteArrayOf(14) + "BETAFPVG473_V2".toByteArray() + byteArrayOf(4) + "BEFH".toByteArray()
        assertEquals("BETAFPVG473_V2", Msp.boardName(info))
        assertEquals("STM32G47X", Msp.boardName("S474".toByteArray() + ByteArray(4) + byteArrayOf(9) + "STM32G47X".toByteArray() + byteArrayOf(0)))
        assertEquals("STM32G47X", Msp.boardName("S474".toByteArray() + ByteArray(4) + byteArrayOf(9) + "STM32G47X".toByteArray()))
    }

    @Test
    fun `the core temperature, when the firmware tells it`() {
        val status = ByteArray(15) + byteArrayOf(1, 0xAA.toByte()) + byteArrayOf(26) + ByteArray(4) + byteArrayOf(0)       // one extra flag byte
        assertEquals(47, Msp.coreTemp(status + byteArrayOf(47, 0)))
        assertEquals(-5, Msp.coreTemp(status + byteArrayOf(0xFB.toByte(), 0xFF.toByte())))
        assertEquals(300, Msp.coreTemp(status + byteArrayOf(0x2C, 0x01)))
        assertNull(Msp.coreTemp(status))                                        // a firmware from before the field
        assertNull(Msp.coreTemp(status + ByteArray(2)))                          // 0: this MCU has no such sensor
        assertNull(Msp.coreTemp(status + byteArrayOf(47)))
        assertNull(Msp.coreTemp(ByteArray(0)))
        assertNull(Msp.coreTemp(ByteArray(15)))
        val two = ByteArray(15) + byteArrayOf(2, 1, 2) + byteArrayOf(26) + ByteArray(4) + byteArrayOf(0)                    // two extra flag bytes move it
        assertEquals(47, Msp.coreTemp(two + byteArrayOf(47, 0)))
    }
}
