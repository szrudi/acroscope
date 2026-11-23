package org.acroscope.companion.core

import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.time.ZonedDateTime
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class BlackboxTest {
    private val dir: File = Files.createTempDirectory("bbl").toFile()
    private val log = "H Product:Blackbox flight data recorder by Nicholas Sherlock\nH Data version:2\n".toByteArray() + ByteArray(10_000) { (it % 250).toByte() }
    private val fc = FakeFc(log)
    private var time = 1_700_000_000_000L
    private val paused = mutableListOf<Long>()
    private val box = Blackbox(fc, { ZonedDateTime.parse("2026-12-12T08:45:00+01:00") }, pause = { paused += it; time += it }, now = { time.also { time += 600 } })   // every look at the clock is 0.6 s on

    @Test
    fun `the used part of the flash is saved under the name the aligner reads`() {
        val progress = mutableListOf<FlashProgress>()
        fc.temps = mutableListOf(44, 41, 43)
        val saved = box.download(dir, { progress += it })!!
        assertEquals("BTFL_BLACKBOX_LOG_OTTO_20261212_084500_BETAFPVG473_V2.bbl", saved.file.name)
        assertEquals(log.toList(), saved.file.readBytes().toList())
        assertEquals(listOf(saved.file.name), dir.list()!!.toList())             // no .part beside it
        // read a piece at a time, each where the last one ended, the last one only as long as what is left
        assertEquals(listOf("read@0+4096", "read@4096+4096", "read@8192+${log.size - 8192}"), fc.asked.filter { it.startsWith("read") })
        assertEquals(listOf(4096L, 8192L, log.size.toLong()), progress.map { it.done })
        assertTrue(progress.all { it.total == log.size.toLong() })
        // the temperature is asked about once a second, not with every piece, and the warmest is kept
        assertEquals(2, fc.asked.count { it == Msp.STATUS.toString() })
        assertEquals(listOf(44, 44, 41), progress.map { it.temp })
        assertEquals(44, saved.peakTemp)                                          // the warmest it was, not the last reading
        assertFalse(Msp.DATAFLASH_ERASE.toString() in fc.asked)                   // saving erases nothing
    }

    @Test
    fun `a quad without a name, and a firmware that tells no temperature`() {
        fc.craft = ""
        fc.spoil = { command, reply -> if (command == Msp.STATUS) Msp.frame(Msp.STATUS, ByteArray(11), '>') else reply }
        val saved = box.download(dir)!!
        assertEquals("BTFL_BLACKBOX_LOG_20261212_084500_BETAFPVG473_V2.bbl", saved.file.name)
        assertNull(saved.peakTemp)
    }

    @Test
    fun `an empty flash is nothing to save, and a board without one says so`() {
        fc.flash = ByteArray(0)
        assertNull(box.download(dir))
        assertTrue(dir.list().isNullOrEmpty())
        fc.present = false
        val e = assertFailsWith<IOException> { box.download(dir) }
        assertTrue("no flash" in e.message!!, e.message)
        assertEquals(Flash(present = false, ready = true, used = 0), box.flash())
    }

    @Test
    fun `a read that is not what was asked for ends the download and leaves no file`() {
        val spoils = listOf<(ByteArray) -> ByteArray>(
            { r -> Msp.frame(Msp.DATAFLASH_READ, Msp.le32(4096) + r.copyOfRange(12, r.size - 1), '>') },                    // from another address
            { r -> Msp.frame(Msp.DATAFLASH_READ, r.copyOfRange(8, 14) + byteArrayOf(1) + r.copyOfRange(15, r.size - 1), '>') },   // compressed
            { r -> Msp.frame(Msp.DATAFLASH_READ, r.copyOfRange(8, 12) + byteArrayOf(0, 0, 0), '>') },                         // nothing
            { r -> Msp.frame(Msp.DATAFLASH_READ, r.copyOfRange(8, r.size - 200), '>') },                                      // shorter than it says
            { _ -> Msp.frame(Msp.DATAFLASH_READ, ByteArray(3), '>') },
        )
        for ((i, spoil) in spoils.withIndex()) {
            var first = true
            fc.spoil = { command, reply -> if (command == Msp.DATAFLASH_READ && first) { first = false; spoil(reply) } else reply }
            val e = assertFailsWith<IOException>("spoil $i") { box.download(dir) }
            assertTrue("bad flash read at 0" in e.message!!, e.message)
            assertTrue(dir.list().isNullOrEmpty(), "spoil $i left ${dir.list()?.toList()}")
            assertFailsWith<IllegalStateException> { box.erase() }               // and the flash is not to be erased after that
        }
        assertFalse(Msp.DATAFLASH_ERASE.toString() in fc.asked)
    }

    @Test
    fun `what does not start like a log is not saved, and the flash stays`() {
        fc.flash = ByteArray(5000) { 7 }
        val e = assertFailsWith<IOException> { box.download(dir) }
        assertTrue("not a whole log" in e.message!! && "not erased" in e.message!!, e.message)
        assertTrue(dir.list().isNullOrEmpty())
        assertFailsWith<IllegalStateException> { box.erase() }
        assertEquals(5000, fc.flash.size)
    }

    @Test
    fun `stopping and a line gone quiet leave no file either`() {
        var pieces = 0
        assertEquals("stopped", assertFailsWith<IOException> { box.download(dir, stop = { pieces++ >= 2 }) }.message)
        assertTrue(dir.list().isNullOrEmpty())
        fc.spoil = { command, reply -> if (command == Msp.DATAFLASH_READ && Msp.u32(reply, 8) == 4096L) null else reply }   // unplugged after the first piece
        assertFailsWith<MspTimeout> { box.download(dir) }
        assertTrue(dir.list().isNullOrEmpty())
        assertFailsWith<IllegalStateException> { box.erase() }
    }

    @Test
    fun `the flash is erased only after a verified download, and once`() {
        val e = assertFailsWith<IllegalStateException> { box.erase() }
        assertTrue("saved and verified" in e.message!!, e.message)
        assertFalse(Msp.DATAFLASH_ERASE.toString() in fc.asked)
        box.download(dir)
        fc.busyPolls = 3
        box.erase()
        assertEquals(0, fc.flash.size)
        assertEquals(1, fc.asked.count { it == Msp.DATAFLASH_ERASE.toString() })
        assertEquals(List(4) { 1000L }, paused)                                   // asked again each second until it said ready and empty
        assertFailsWith<IllegalStateException> { box.erase() }                   // nothing saved since: nothing to erase
        assertNull(box.download(dir))                                            // and the flash is empty now
    }

    @Test
    fun `a save that failed takes back what an earlier one allowed`() {
        box.download(dir)!!.file.delete()
        fc.spoil = { command, reply -> if (command == Msp.DATAFLASH_READ) Msp.frame(command, ByteArray(3), '>') else reply }
        assertFailsWith<IOException> { box.download(dir) }
        assertFailsWith<IllegalStateException> { box.erase() }                   // the last word on this flash was "not saved"
        assertEquals(log.size, fc.flash.size)
    }

    @Test
    fun `the erase is over when the flash says both ready and empty`() {
        box.download(dir)
        fc.halfDone = mutableListOf(true to 5000L, false to 0L)                  // answering again but not empty yet; empty but still busy
        box.erase()
        assertEquals(3, paused.size)
        assertEquals(0, fc.flash.size)
    }

    @Test
    fun `a controller that goes quiet while the chip erases is waited for`() {
        box.download(dir)
        fc.eraseAnswers = false                                                  // no answer to the erase itself
        fc.silentPolls = 2                                                       // nor to the first two questions after it
        fc.busyPolls = 1
        box.erase()
        assertEquals(0, fc.flash.size)
        assertEquals(4, paused.size)
    }

    @Test
    fun `an erase that never ends is given up on after five minutes`() {
        box.download(dir)
        fc.eraseNeverEnds = true
        val start = time
        val e = assertFailsWith<IOException> { box.erase() }
        assertTrue("five minutes" in e.message!!, e.message)
        assertTrue(time - start in 300_000..310_000, (time - start).toString())
        assertEquals(log.size, fc.flash.size)
    }

    @Test
    fun `noise on the line does not spoil a download`() {
        fc.noise = "\r\n# garbage \$X".toByteArray()
        assertEquals(log.toList(), box.download(dir)!!.file.readBytes().toList())
    }
}
