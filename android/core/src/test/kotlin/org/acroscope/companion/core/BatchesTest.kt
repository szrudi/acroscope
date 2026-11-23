package org.acroscope.companion.core

import java.io.File
import java.nio.file.Files
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class BatchesTest {
    private val store = Store(Files.createTempDirectory("batches").toFile())
    private val batches = Batches(store)
    private val id = "20261212-090507-ab12cd"

    private fun given(stage: Stage = Stage.PENDING) {
        val dir = store.dir(id).apply { mkdirs() }
        for (n in listOf("VID00001.mov", "VID00002.mov", "x.bbl")) File(dir, n).writeBytes(ByteArray(10))
        val items = listOf(Item("VID00001.mov", 10, Kind.CLIP, ""), Item("VID00002.mov", 10, Kind.CLIP, ""), Item("x.bbl", 10, Kind.LOG, ""))
        store.update { it.copy(batches = listOf(Batch(id, "park", "2026-12-12", "2026-12-12T09:05:07+01:00", items, stage)), seen = listOf(Seen("VID00001.mov", 10, 1000))) }
    }

    @Test
    fun `the event and the date of a pending batch can be put right`() {
        given()
        batches.edit(id, "  whoop race ", "2026-12-11")
        assertEquals("whoop race" to "2026-12-11", store.now.batch(id)!!.let { it.event to it.date })
        assertEquals("2026-12-11-whoop-race", store.now.batch(id)!!.session)
        assertEquals("2026-12-12T09:05:07+01:00", store.now.batch(id)!!.createdAt)            // when it was collected stays what it was
        for (bad in listOf("12-11-2026", "2026-13-01", "2026-02-30", "yesterday", "", "2026-12-11 ")) {
            val e = assertFailsWith<IllegalArgumentException>(bad) { batches.edit(id, "park", bad) }
            assertTrue("date" in e.message!!, e.message)
        }
        assertEquals("whoop race" to "2026-12-11", store.now.batch(id)!!.let { it.event to it.date })
        batches.edit(id, "", "2026-12-11")                                                   // no name: ingest files it as unsorted
        assertEquals("2026-12-11-unsorted", store.now.batch(id)!!.session)
    }

    @Test
    fun `a file taken out of a pending batch is gone from the phone and stays collected`() {
        given()
        batches.remove(id, "VID00001.mov")
        assertEquals(listOf("VID00002.mov", "x.bbl"), store.now.batch(id)!!.items.map { it.name })
        assertFalse(File(store.dir(id), "VID00001.mov").exists())
        assertEquals(listOf(Seen("VID00001.mov", 10, 1000)), store.now.seen)                 // the card still has it: not collected again
        batches.remove(id, "no-such.mov")                                                    // not there: nothing happens
        assertEquals(2, store.now.batch(id)!!.items.size)
        batches.remove(id, "VID00002.mov")
        batches.remove(id, "x.bbl")
        assertNull(store.now.batch(id))                                                      // the last file out: the batch is gone
        assertFalse(store.dir(id).exists())
    }

    @Test
    fun `a pending batch can be discarded whole`() {
        given()
        batches.discard(id)
        assertNull(store.now.batch(id))
        assertFalse(store.dir(id).exists())
        batches.discard(id)                                                                  // gone already: fine
    }

    @Test
    fun `what was handed to the server is not edited here any more`() {
        for (stage in listOf(Stage.COMMITTED, Stage.DONE)) {
            given(stage)
            for (change in listOf<() -> Unit>({ batches.edit(id, "home", "2026-12-11") }, { batches.remove(id, "VID00001.mov") }, { batches.discard(id) })) {
                val e = assertFailsWith<IllegalStateException> { change() }
                assertTrue("server" in e.message!!, e.message)
            }
            assertEquals("park" to 3, store.now.batch(id)!!.let { it.event to it.items.size })
            assertTrue(File(store.dir(id), "VID00001.mov").exists())
        }
    }

    @Test
    fun `finished batches are forgotten past the last few`() {
        val done = (10..39).map { Batch("202612$it-090507-ab12cd", "park", "2026-12-$it", "", stage = Stage.DONE) }
        val open = listOf(Batch("20261201-090507-pend01", "park", "2026-12-01", "", listOf(Item("x.bbl", 1, Kind.LOG, ""))),
            Batch("20261202-090507-comm01", "park", "2026-12-02", "", stage = Stage.COMMITTED))
        store.update { it.copy(batches = open + done) }
        batches.prune(keep = 20)
        assertEquals(open.map { it.id } + done.takeLast(20).map { it.id }, store.now.batches.map { it.id })   // the newest twenty finished, and everything not finished
        batches.prune()
        assertEquals(22, store.now.batches.size)                                             // twenty by default
    }

    @Test
    fun `the card's memory can be cleared`() {
        given()
        batches.forgetCollected()
        assertEquals(emptyList(), store.now.seen)
        assertEquals(3, store.now.batch(id)!!.items.size)
    }
}
