package org.acroscope.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File
import java.nio.file.Files
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class StoreTest {
    private val root: File = Files.createTempDirectory("store").toFile()
    private val batch = Batch("20261212-090507-ab12cd", "whoop race", "2026-12-12", "2026-12-12T09:05:07+01:00")

    @Test
    fun `what is changed is there after a restart`() {
        val store = Store(root)
        assertEquals(State(), store.now)
        assertFalse(File(root, "state.json").exists())                          // nothing written for nothing
        store.update { it.copy(settings = it.settings.copy(event = "park"), batches = listOf(batch)) }
        store.updateBatch(batch.id) { it.copy(items = listOf(Item("VID00001.mov", 5, Kind.CLIP, "2026-12-12T09:06:00+01:00", unfinalised = true))) }
        val again = Store(root)
        assertEquals(store.now, again.now)
        assertEquals("park", again.now.settings.event)
        assertEquals(listOf(Item("VID00001.mov", 5, Kind.CLIP, "2026-12-12T09:06:00+01:00", true)), again.now.batch(batch.id)!!.items)
        assertNull(again.now.batch("20261212-090507-zzzzzz"))
        assertEquals(listOf("state.json"), root.list()!!.toList())              // and no half-written file beside it
    }

    @Test
    fun `the state as a flow says each change once`() {
        val store = Store(root)
        val before = store.state.value
        store.update { it }                                                     // no change: nothing written
        assertFalse(File(root, "state.json").exists())
        store.update { it.copy(seen = listOf(Seen("VID00001.mov", 5, 1000))) }
        assertEquals(listOf(Seen("VID00001.mov", 5, 1000)), store.state.value.seen)
        assertTrue(before !== store.state.value)
    }

    @Test
    fun `a state file from a later version is read for what is known`() {
        root.mkdirs()
        File(root, "state.json").writeText("""{"settings": {"event": "home", "somethingNew": 1}, "batches": [], "futureList": [1, 2]}""")
        assertEquals("home", Store(root).now.settings.event)
    }

    @Test
    fun `a batch has its folder, and only a batch id names one`() {
        val store = Store(root)
        assertEquals(File(root, "batches/20261212-090507-ab12cd"), store.dir(batch.id))
        for (bad in listOf("..", "../x", "b1", "")) assertFailsWith<IllegalArgumentException>(bad) { store.dir(bad) }
        val roomy = File(root, "not/made/yet")
        val split = Store(root, roomy)                                          // the state here, the footage on the roomy storage
        assertEquals(File(roomy, "batches/20261212-090507-ab12cd"), split.dir(batch.id))
        split.update { it.copy(batches = listOf(batch)) }
        assertTrue(File(root, "state.json").exists() && !roomy.exists())
        assertTrue(split.free > 0 && split.free == root.usableSpace)             // asked of the nearest folder that is there
    }

    @Test
    fun `pending batches come oldest first`() {
        val store = Store(root)
        val later = batch.copy(id = "20261212-150000-zz99zz")
        val done = batch.copy(id = "20261211-100000-aa11aa", stage = Stage.DONE)
        store.update { it.copy(batches = listOf(later, done, batch)) }
        assertEquals(listOf(batch.id, later.id), store.now.pending.map { it.id })
    }

    @Test
    fun `the sidecar is what ingest reads`() {
        val o = Json.parseToJsonElement(batch.sidecar("field phone")).jsonObject
        assertEquals(setOf("date", "session", "imported_at", "host"), o.keys)
        assertEquals(listOf("2026-12-12", "whoop race", "2026-12-12T09:05:07+01:00", "field phone"),
            listOf("date", "session", "imported_at", "host").map { o[it]!!.jsonPrimitive.content })
        assertEquals("2026-12-12-whoop-race", batch.session)
        val bare = Json.parseToJsonElement(batch.copy(event = " ").sidecar(null)).jsonObject
        assertEquals(setOf("date", "imported_at"), bare.keys)                   // no name given: ingest files it as unsorted
        assertEquals("2026-12-12-unsorted", batch.copy(event = " ").session)
        assertEquals(15, batch.copy(items = listOf(Item("a.bbl", 5, Kind.LOG, ""), Item("VID1.mov", 10, Kind.CLIP, ""))).bytes)
    }

    @Test
    fun `the pairing is there when both halves are`() {
        assertNull(Settings().pairing)
        assertNull(Settings(url = "https://x.example.org").pairing)
        assertEquals(Pairing("https://x.example.org", "t".repeat(43)), Settings(url = "https://x.example.org", token = "t".repeat(43)).pairing)
    }
}
