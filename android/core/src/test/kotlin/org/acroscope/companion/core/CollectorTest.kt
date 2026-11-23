package org.acroscope.companion.core

import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.nio.ByteBuffer
import java.nio.file.Files
import java.time.ZonedDateTime
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNotEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class CollectorTest {
    private val root: File = Files.createTempDirectory("collect").toFile()
    private val store = Store(root)
    private var now = ZonedDateTime.parse("2026-12-12T09:05:07+01:00")
    private val collector = Collector(store, { now }, Random(7))

    /** A movie as the DVR writes one: finished (with its index) or cut off by a power loss (without). */
    private fun movie(payload: Int, finished: Boolean = true): ByteArray {
        fun atom(type: String, n: Int) = ByteBuffer.allocate(8 + n).putInt(8 + n).put(type.toByteArray()).array().also { for (i in 8 until it.size) it[i] = (i % 251).toByte() }
        return atom("ftyp", 12) + atom("mdat", payload) + if (finished) atom("moov", 40) else ByteArray(0)
    }

    private class Clip(override val name: String, val data: ByteArray, override val modified: Long = 1000, val breakAt: Int? = null) : CardFile {
        override val size: Long get() = data.size.toLong()
        var opened = 0
        override fun open(): InputStream {
            opened++
            val inner = ByteArrayInputStream(data)
            return object : InputStream() {
                var at = 0
                override fun read(): Int = throw UnsupportedOperationException("a byte at a time from a card would take all day")
                override fun read(b: ByteArray, off: Int, len: Int): Int {
                    if (breakAt != null && at >= breakAt) throw IOException("card removed")
                    val n = inner.read(b, off, if (breakAt != null) minOf(len, breakAt - at) else len)
                    if (n > 0) at += n
                    return n
                }
            }
        }
    }

    private fun card(vararg files: CardFile) = object : Card {
        override val label = "COBRA"
        override fun files(): List<CardFile> = files.toList()
    }

    private fun event(name: String) = store.update { it.copy(settings = it.settings.copy(event = name)) }

    @Test
    fun `the clips of a card, in the order the dvr recorded them`() {
        event("whoop race")
        val c = card(Clip("VID00002.mov", movie(300)), Clip("notes.txt", ByteArray(5)), Clip("VID00001.mov", movie(100)), Clip("vid00003.MP4", movie(50)), Clip(".VID00009.mov", ByteArray(3)))
        val plan = collector.plan(c)
        assertEquals(listOf("VID00001.mov", "VID00002.mov", "vid00003.MP4"), plan.clips.map { it.name })
        assertEquals(listOf(".VID00009.mov", "notes.txt"), plan.others)       // what is left on the card is said, not passed over
        assertEquals(plan.clips.sumOf { it.size }, plan.bytes)
        assertEquals(0, plan.collected)

        val seenProgress = mutableListOf<Progress>()
        val out = collector.collect(c, free = Long.MAX_VALUE, progress = { seenProgress += it })
        assertNull(out.error)
        assertEquals(listOf("VID00001.mov", "VID00002.mov", "vid00003.MP4"), out.items.map { it.name })
        val batch = store.now.batches.single()
        assertEquals(Batch(batch.id, "whoop race", "2026-12-12", "2026-12-12T09:05:07+01:00", out.items, Stage.PENDING), batch)
        assertTrue(Names.isBatchId(batch.id) && batch.id.startsWith("20261212-090507-"), batch.id)
        assertEquals(listOf("VID00001.mov", "VID00002.mov", "vid00003.MP4"), store.dir(batch.id).list()!!.sorted())   // no .part left
        assertEquals(movie(300).toList(), File(store.dir(batch.id), "VID00002.mov").readBytes().toList())
        assertEquals(out.items.map { Item(it.name, File(store.dir(batch.id), it.name).length(), Kind.CLIP, "2026-12-12T09:05:07+01:00") }, out.items)
        assertEquals(listOf(Seen("VID00001.mov", movie(100).size.toLong(), 1000), Seen("VID00002.mov", movie(300).size.toLong(), 1000), Seen("vid00003.MP4", movie(50).size.toLong(), 1000)), store.now.seen)
        // progress: every clip named with its place in the collect, the bytes never past the total, ending on it
        assertEquals(setOf("VID00001.mov", "VID00002.mov", "vid00003.MP4"), seenProgress.map { it.name }.toSet())
        assertTrue(seenProgress.all { it.done <= it.total && it.total == plan.bytes && it.count == 3 }, seenProgress.toString())
        assertEquals(Progress("vid00003.MP4", 3, 3, plan.bytes, plan.bytes), seenProgress.last())
        assertEquals(seenProgress.map { it.done }.sorted(), seenProgress.map { it.done })
    }

    @Test
    fun `what was collected is not collected again`() {
        event("park")
        val a = Clip("VID00001.mov", movie(100))
        val b = Clip("VID00002.mov", movie(200))
        collector.collect(card(a), free = Long.MAX_VALUE)
        now = now.plusMinutes(30)
        val plan = collector.plan(card(a, b))
        assertEquals(listOf("VID00002.mov") to 1, plan.clips.map { it.name } to plan.collected)
        val out = collector.collect(card(a, b), free = Long.MAX_VALUE)
        assertEquals(listOf("VID00002.mov"), out.items.map { it.name })
        assertEquals(1, a.opened)                                               // not read a second time
        val batch = store.now.batches.single()                                  // the same event the same day: one pending upload
        assertEquals(listOf("VID00001.mov", "VID00002.mov"), batch.items.map { it.name })
        assertEquals(listOf("2026-12-12T09:05:07+01:00", "2026-12-12T09:35:07+01:00"), batch.items.map { it.collectedAt })
        assertEquals("2026-12-12T09:05:07+01:00", batch.createdAt)
        assertEquals(emptyList(), collector.collect(card(a, b), free = Long.MAX_VALUE).items)
        // a clip by the same name that is another recording (size or time differ) is new
        assertEquals(1, collector.plan(card(Clip("VID00001.mov", movie(100), modified = 2000))).clips.size)
        assertEquals(1, collector.plan(card(Clip("VID00001.mov", movie(101)))).clips.size)
    }

    @Test
    fun `another event or another day starts another batch`() {
        event("park")
        collector.collect(card(Clip("VID00001.mov", movie(100))), free = Long.MAX_VALUE)
        event("home")
        collector.collect(card(Clip("VID00002.mov", movie(100))), free = Long.MAX_VALUE)
        event("park")
        collector.collect(card(Clip("VID00003.mov", movie(100))), free = Long.MAX_VALUE)        // back at the park: its batch goes on
        now = now.plusDays(1)
        collector.collect(card(Clip("VID00004.mov", movie(100))), free = Long.MAX_VALUE)        // the next day there is another session
        assertEquals(listOf("park" to listOf("VID00001.mov", "VID00003.mov"), "home" to listOf("VID00002.mov"), "park" to listOf("VID00004.mov")),
            store.now.batches.map { it.event to it.items.map { i -> i.name } })
        assertEquals(listOf("2026-12-12", "2026-12-12", "2026-12-13"), store.now.batches.map { it.date })
        assertEquals(listOf("park", "home"), store.now.settings.recentEvents)                // what was flown, the latest first, each once
        assertEquals(3, store.now.batches.map { it.id }.toSet().size)
        // a batch that went up takes no more: the next collect of the same event and day starts one
        store.updateBatch(store.now.batches.last().id) { it.copy(stage = Stage.COMMITTED) }
        collector.collect(card(Clip("VID00005.mov", movie(100))), free = Long.MAX_VALUE)
        assertEquals(listOf("VID00005.mov"), store.now.batches.last().items.map { it.name })
        assertEquals(4, store.now.batches.size)
    }

    @Test
    fun `a card formatted in between starts a batch of its own, so the clips keep their order`() {
        event("park")
        collector.collect(card(Clip("VID00001.mov", movie(100)), Clip("VID00002.mov", movie(200))), free = Long.MAX_VALUE)
        now = now.plusHours(3)
        // the dvr counts from 1 again: same names, other recordings
        val out = collector.collect(card(Clip("VID00001.mov", movie(111), modified = 5000), Clip("VID00002.mov", movie(222), modified = 6000)), free = Long.MAX_VALUE)
        assertEquals(listOf("VID00001.mov", "VID00002.mov"), out.items.map { it.name })
        val (first, second) = store.now.batches
        assertEquals(listOf(movie(100).size, movie(200).size), first.items.map { it.size.toInt() })
        assertEquals(listOf(movie(111).size, movie(222).size), second.items.map { it.size.toInt() })
        assertTrue(first.id < second.id)                                        // ingested after the first: the day's numbers follow the flying
        assertEquals(movie(111).toList(), File(store.dir(second.id), "VID00001.mov").readBytes().toList())
        assertEquals(movie(100).toList(), File(store.dir(first.id), "VID00001.mov").readBytes().toList())
        // and what comes after goes on in the newer batch, though the older one has no clip of that name either
        collector.collect(card(Clip("VID00003.mov", movie(333), modified = 7000)), free = Long.MAX_VALUE)
        assertEquals(listOf(2, 3), store.now.batches.map { it.items.size })
    }

    @Test
    fun `a clip without its index is collected and flagged`() {
        event("park")
        val out = collector.collect(card(Clip("VID00001.mov", movie(100)), Clip("VID00002.mov", movie(100, finished = false)), Clip("VID00003.avi", "RIFF....".toByteArray())), free = Long.MAX_VALUE)
        assertEquals(listOf(false, true, false), out.items.map { it.unfinalised })   // an .avi has no such index: not judged
    }

    @Test
    fun `a card pulled in the middle leaves no half file and no word of it`() {
        event("park")
        val c = card(Clip("VID00001.mov", movie(100)), Clip("VID00002.mov", movie(5000), breakAt = 700), Clip("VID00003.mov", movie(100)))
        val out = collector.collect(c, free = Long.MAX_VALUE)
        assertEquals(listOf("VID00001.mov"), out.items.map { it.name })          // what was whole is kept
        assertTrue("VID00002.mov" in out.error!! && "card removed" in out.error, out.error)
        val batch = store.now.batches.single()
        assertEquals(listOf("VID00001.mov"), store.dir(batch.id).list()!!.toList())
        assertEquals(listOf("VID00001.mov"), store.now.seen.map { it.name })
        // plugged in again: the broken one and the one after it are still to collect
        assertEquals(listOf("VID00002.mov", "VID00003.mov"), collector.plan(c).clips.map { it.name })
    }

    @Test
    fun `a clip that came out shorter than the card said is not taken for whole`() {
        event("park")
        val short = object : CardFile {
            override val name = "VID00001.mov"
            override val size = 5000L                                           // the card's directory says so
            override val modified = 1000L
            override fun open(): InputStream = ByteArrayInputStream(movie(100))
        }
        val out = collector.collect(card(short), free = Long.MAX_VALUE)
        assertEquals(emptyList(), out.items)
        assertTrue("VID00001.mov" in out.error!!, out.error)
        assertEquals(emptyList(), store.now.seen)
        assertEquals(emptyList(), store.now.batches)
        assertTrue(File(root, "batches").list().isNullOrEmpty())                 // not even the folder the batch would have had
    }

    @Test
    fun `a collect that does not fit is refused before a byte is copied`() {
        event("park")
        val a = Clip("VID00001.mov", movie(1000))
        val need = a.size + Collector.MARGIN
        val out = collector.collect(card(a), free = need - 1)
        assertEquals(emptyList(), out.items)
        assertTrue("room" in out.error!!, out.error)
        assertEquals(0, a.opened)
        assertEquals(emptyList(), store.now.batches)
        assertNull(collector.collect(card(a), free = need).error)               // to the byte
    }

    @Test
    fun `stopping ends the collect after the clip in hand`() {
        event("park")
        var asked = 0
        val out = collector.collect(card(Clip("VID00001.mov", movie(100)), Clip("VID00002.mov", movie(100))), free = Long.MAX_VALUE, stop = { asked++ > 0 })
        assertEquals(listOf("VID00001.mov"), out.items.map { it.name })
        assertEquals("stopped", out.error)
    }

    @Test
    fun `only so many event names are remembered, and none for a collect without one`() {
        for (i in 1..14) {
            event("event $i")
            collector.collect(card(Clip("VID000%02d.mov".format(i), movie(10))), free = Long.MAX_VALUE)
        }
        event("")
        collector.collect(card(Clip("VID00099.mov", movie(10))), free = Long.MAX_VALUE)
        assertEquals((14 downTo 3).map { "event $it" }, store.now.settings.recentEvents)
    }

    @Test
    fun `nothing new makes no batch`() {
        event("park")
        assertEquals(Outcome(emptyList(), null), collector.collect(card(Clip("notes.txt", ByteArray(4))), free = Long.MAX_VALUE))
        assertEquals(emptyList(), store.now.batches)
    }

    // ---- a log the Betaflight app saved

    private val header = "H Product:Blackbox flight data recorder by Nicholas Sherlock\nH Data version:2\nH Firmware revision:Betaflight 4.5.1 (77d01ba3b) STM32G47X\n" +
        "H Board information:BEFH BETAFPVG473_V2\nH Craft name:OttO\nH I interval:8\n"
    private fun log(extra: Int = 300, head: String = header) = head.toByteArray() + ByteArray(extra) { 0x41 }

    @Test
    fun `a saved log goes into the batch under the name the aligner reads`() {
        event("whoop race")
        val name = "BTFL_BLACKBOX_LOG_OTTO_20261212_084500_BETAFPVG473_V2.bbl"
        val item = collector.addLog(name, modified = 0) { ByteArrayInputStream(log()) }
        assertEquals(Item(name, log().size.toLong(), Kind.LOG, "2026-12-12T09:05:07+01:00"), item)
        val batch = store.now.batches.single()
        assertEquals(listOf(item), batch.items)
        assertEquals(log().toList(), File(store.dir(batch.id), name).readBytes().toList())
        // the same dump picked again is the same log
        assertEquals(item, collector.addLog(name, modified = 0) { ByteArrayInputStream(log()) })
        assertEquals(1, store.now.batches.single().items.size)
        // clips collected later the same day join it: one batch, so automatch plans them together
        collector.collect(card(Clip("VID00001.mov", movie(100))), free = Long.MAX_VALUE)
        assertEquals(listOf(name, "VID00001.mov"), store.now.batches.single().items.map { it.name })
    }

    @Test
    fun `a log under another name is named from its own header and the time it was saved`() {
        event("park")
        val saved = ZonedDateTime.parse("2026-12-12T08:45:00+01:00").toInstant().toEpochMilli()
        assertEquals("BTFL_BLACKBOX_LOG_OTTO_20261212_084500_BETAFPVG473_V2.bbl", collector.addLog("btfl_001.bbl", saved) { ByteArrayInputStream(log()) }.name)
        // no time known: now; no craft name on the quad: left out; no board line: UNKNOWN
        val bare = "H Product:Blackbox flight data recorder by Nicholas Sherlock\nH Craft name:\n"
        assertEquals("BTFL_BLACKBOX_LOG_20261212_090507_UNKNOWN.bbl", collector.addLog("LOG00001.BBL", null) { ByteArrayInputStream(log(10, bare)) }.name)
        now = now.plusSeconds(5)
        assertEquals("BTFL_BLACKBOX_LOG_OTTO_20261212_090512_BETAFPVG473_V2.bbl", collector.addLog("btfl_002.bbl", 0) { ByteArrayInputStream(log(20)) }.name)   // a picker that says 0 knows no time
    }

    @Test
    fun `what is not a blackbox log is refused and nothing of it kept`() {
        event("park")
        for ((name, bytes) in listOf("x.bbl" to "not a log at all".toByteArray(), "x.bbl" to ByteArray(0), "notes.txt" to log(), "x.bbl" to movie(100))) {
            val e = assertFailsWith<IllegalArgumentException>(name) { collector.addLog(name, null) { ByteArrayInputStream(bytes) } }
            assertTrue("log" in e.message!!, e.message)
        }
        assertEquals(emptyList(), store.now.batches)
        assertTrue(File(root, "batches").list().isNullOrEmpty())
    }

    @Test
    fun `another dump under a name already there is kept beside it`() {
        event("park")
        val name = "BTFL_BLACKBOX_LOG_OTTO_20261212_084500_BETAFPVG473_V2.bbl"
        val a = collector.addLog(name, 0) { ByteArrayInputStream(log(300)) }
        val b = collector.addLog(name, 0) { ByteArrayInputStream(log(400)) }
        assertNotEquals(a.name, b.name)
        assertTrue(Names.isLog(b.name) && Names.accepted(b.name), b.name)
        assertEquals(2, store.now.batches.single().items.size)
        assertFalse(File(store.dir(store.now.batches.single().id), "$name.part").exists())
    }
}
