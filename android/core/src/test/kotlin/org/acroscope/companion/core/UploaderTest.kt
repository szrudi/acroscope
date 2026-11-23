package org.acroscope.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File
import java.io.IOException
import java.nio.file.Files
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class UploaderTest {
    private val root: File = Files.createTempDirectory("up").toFile()
    private val store = Store(root)

    /** The server as the uploader sees it: what was sent, in order, and failures to order. */
    private class FakeDrop : Drop {
        val calls = mutableListOf<String>()
        val staged = mutableMapOf<String, MutableMap<String, ByteArray>>()
        val committed = mutableMapOf<String, Map<String, Long>>()
        val statuses = mutableMapOf<String, ServerStatus>()
        var failAt: ((String) -> IOException?) = { null }
        var recent = listOf("park", "home")

        private fun call(what: String) { calls += what; failAt(what)?.let { throw it } }
        override fun device(): Device { call("device"); return Device("d0123456789", "field phone", 1L shl 30, 0, 4L shl 30) }
        override fun events(): List<String> { call("events"); return recent }
        override fun batches(): Map<String, ServerStatus> { call("batches"); return statuses.toMap() }
        override fun upload(batch: String, name: String, file: File, progress: (Long) -> Unit, stop: () -> Boolean) {
            call("upload $batch $name")
            if (stop()) throw DropStopped()
            staged.getOrPut(batch) { mutableMapOf() }[name] = file.readBytes()
            progress(file.length())
        }
        override fun put(batch: String, name: String, bytes: ByteArray) { call("put $batch $name"); staged.getOrPut(batch) { mutableMapOf() }[name] = bytes }
        override fun commit(batch: String, files: Map<String, Long>): ServerStatus {
            call("commit $batch")
            committed[batch] = files
            return ServerStatus("pending").also { statuses[batch] = it }
        }
        override fun retry(batch: String): ServerStatus { call("retry $batch"); return ServerStatus("pending").also { statuses[batch] = it } }
    }

    private val drop = FakeDrop()
    private val uploader = Uploader(store) { drop }
    private val a = "20261212-090507-aaaaaa"
    private val b = "20261212-150000-bbbbbb"

    private fun paired() = store.update { it.copy(settings = it.settings.copy(url = "https://x.example.org", token = "t".repeat(43), device = "field phone")) }

    private fun batch(id: String, event: String, vararg files: Pair<String, Int>) {
        val dir = store.dir(id).apply { mkdirs() }
        val items = files.map { (name, n) -> File(dir, name).writeBytes(ByteArray(n) { 7 }); Item(name, n.toLong(), if (Names.isLog(name)) Kind.LOG else Kind.CLIP, "2026-12-12T09:06:00+01:00") }
        store.update { it.copy(batches = it.batches + Batch(id, event, "2026-12-12", "2026-12-12T09:05:07+01:00", items)) }
    }

    @Test
    fun `every pending batch goes up oldest first, files then sidecar then commit`() {
        paired()
        batch(b, "park", "VID00003.mov" to 30)
        batch(a, "whoop race", "VID00002.mov" to 20, "x.bbl" to 5, "VID00001.mov" to 10)
        val progress = mutableListOf<UploadProgress>()
        assertEquals(UploadResult.Done(2), uploader.run(progress = { progress += it }))
        assertEquals(listOf("upload $a x.bbl", "upload $a VID00001.mov", "upload $a VID00002.mov", "put $a import.json", "commit $a",
            "upload $b VID00003.mov", "put $b import.json", "commit $b", "batches"), drop.calls)
        val sidecar = drop.staged[a]!!["import.json"]!!
        assertEquals(mapOf("VID00001.mov" to 10L, "VID00002.mov" to 20L, "x.bbl" to 5L, "import.json" to sidecar.size.toLong()), drop.committed[a])
        val o = Json.parseToJsonElement(String(sidecar)).jsonObject
        assertEquals(listOf("2026-12-12", "whoop race", "field phone"), listOf("date", "session", "host").map { o[it]!!.jsonPrimitive.content })
        assertEquals(listOf(Stage.COMMITTED, Stage.COMMITTED), listOf(a, b).map { store.now.batch(it)!!.stage })
        assertEquals(ServerStatus("pending"), store.now.batch(a)!!.server)
        assertTrue(File(store.dir(a), "VID00001.mov").exists())                 // the phone's copy stays until the server reports the batch done
        // progress: the bytes of everything pending, never past the total, ending on it
        assertTrue(progress.all { it.total == 65L && it.done <= it.total }, progress.toString())
        assertEquals(65L, progress.last().done)
        assertEquals(progress.map { it.done }.sorted(), progress.map { it.done })
    }

    @Test
    fun `a failure stops there, and what is behind it waits, so batches arrive in order`() {
        paired()
        batch(a, "whoop race", "VID00001.mov" to 10, "VID00002.mov" to 20)
        batch(b, "park", "VID00003.mov" to 30)
        drop.failAt = { if (it == "upload $a VID00002.mov") IOException("connection reset") else null }
        assertEquals(UploadResult.Unreachable("connection reset"), uploader.run())
        assertEquals(listOf("upload $a VID00001.mov", "upload $a VID00002.mov"), drop.calls)
        assertEquals(listOf(Stage.PENDING, Stage.PENDING), listOf(a, b).map { store.now.batch(it)!!.stage })
        drop.failAt = { null }
        drop.calls.clear()
        assertEquals(UploadResult.Done(2), uploader.run())                       // the next run sends it all again: the client asks the server what it has
        assertEquals("commit $a", drop.calls[3])
    }

    @Test
    fun `what the server says no to is told apart`() {
        paired()
        batch(a, "park", "VID00001.mov" to 10)
        drop.failAt = { if (it.startsWith("upload")) DropException(507, "the drop is full") else null }
        assertEquals(UploadResult.Refused(507, "the drop is full"), uploader.run())
        drop.failAt = { if (it.startsWith("upload")) DropException(401, "this door takes a paired device's token") else null }
        assertEquals(UploadResult.Refused(401, "this door takes a paired device's token"), uploader.run())
        drop.failAt = { if (it.startsWith("upload")) DropStopped() else null }
        assertEquals(UploadResult.Stopped, uploader.run())
        assertEquals(Stage.PENDING, store.now.batch(a)!!.stage)
    }

    @Test
    fun `without a pairing nothing is tried`() {
        batch(a, "park", "VID00001.mov" to 10)
        assertEquals(UploadResult.NotPaired, uploader.run())
        assertEquals(emptyList(), drop.calls)
    }

    @Test
    fun `nothing pending only asks how the committed ones are doing`() {
        paired()
        assertEquals(UploadResult.Done(0), uploader.run())
        assertEquals(emptyList(), drop.calls)                                    // nothing committed either: nothing to ask
        batch(a, "park", "VID00001.mov" to 10)
        uploader.run()
        drop.calls.clear()
        assertEquals(UploadResult.Done(0), uploader.run())
        assertEquals(listOf("batches"), drop.calls)
    }

    @Test
    fun `a batch with nothing in it is dropped, not sent`() {
        paired()
        batch(a, "park")
        assertEquals(UploadResult.Done(0), uploader.run())
        assertNull(store.now.batch(a))
        assertEquals(emptyList(), drop.calls)
    }

    @Test
    fun `a file gone from the phone fails the batch before anything is sent`() {
        paired()
        batch(a, "park", "VID00001.mov" to 10, "VID00002.mov" to 20)
        File(store.dir(a), "VID00002.mov").delete()
        val r = uploader.run()
        assertTrue(r is UploadResult.Broken && "VID00002.mov" in r.message, r.toString())
        assertEquals(emptyList(), drop.calls)
        File(store.dir(a), "VID00002.mov").writeBytes(ByteArray(19))            // or not the size it was collected with
        assertTrue(uploader.run() is UploadResult.Broken)
        assertEquals(emptyList(), drop.calls)
    }

    @Test
    fun `the phone's copy goes when the server reports the batch done, not before`() {
        paired()
        batch(a, "whoop race", "VID00001.mov" to 10, "x.bbl" to 5)
        uploader.run()
        for (status in listOf("pending", "running")) {
            drop.statuses[a] = ServerStatus(status)
            uploader.refresh()
            assertEquals(Stage.COMMITTED to status, store.now.batch(a)!!.stage to store.now.batch(a)!!.server!!.status)
            assertTrue(File(store.dir(a), "VID00001.mov").exists(), status)
        }
        drop.statuses[a] = ServerStatus("failed", error = "ValueError: VID00001.mov: moov atom not found")
        uploader.refresh()
        assertEquals("ValueError: VID00001.mov: moov atom not found", store.now.batch(a)!!.server!!.error)
        assertTrue(File(store.dir(a), "VID00001.mov").exists())                  // a failed batch keeps its files: they may be needed again
        drop.statuses[a] = ServerStatus("done", "2026-12-12-whoop-race", null, listOf("2026-12-12_001"), listOf("x.bbl"))
        uploader.refresh()
        val done = store.now.batch(a)!!
        assertEquals(Stage.DONE to listOf("2026-12-12_001"), done.stage to done.server!!.clips)
        assertFalse(store.dir(a).exists())
        assertEquals(2, done.items.size)                                         // what it held is still told
        drop.calls.clear()
        uploader.refresh()
        assertEquals(emptyList(), drop.calls)                                    // nothing left to ask about
    }

    @Test
    fun `a committed batch the server does not know is left alone`() {
        paired()
        batch(a, "park", "VID00001.mov" to 10)
        uploader.run()
        drop.statuses.clear()                                                    // another server, or its database gone
        uploader.refresh()
        assertEquals(Stage.COMMITTED, store.now.batch(a)!!.stage)
        assertTrue(File(store.dir(a), "VID00001.mov").exists())
    }

    @Test
    fun `a failed batch is tried again on the server`() {
        paired()
        batch(a, "park", "VID00001.mov" to 10)
        uploader.run()
        store.updateBatch(a) { it.copy(server = ServerStatus("failed", error = "boom")) }
        uploader.retry(a)
        assertEquals("retry $a", drop.calls.last())
        assertEquals(ServerStatus("pending"), store.now.batch(a)!!.server)
    }

    @Test
    fun `the names of recent events, the server's and the ones used here`() {
        paired()
        store.update { it.copy(settings = it.settings.copy(recentEvents = listOf("whoop race", "home"))) }
        assertEquals(listOf("whoop race", "home", "park"), uploader.events())    // used here first, each once
        drop.failAt = { IOException("no route") }
        assertEquals(listOf("whoop race", "home"), uploader.events())            // out in the field: what is remembered
        store.update { it.copy(settings = Settings(recentEvents = listOf("x"))) }
        assertEquals(listOf("x"), uploader.events())                             // not paired: nothing asked
    }

    @Test
    fun `pairing asks the server who this is and remembers it`() {
        val p = Pairing("https://x.example.org", "t".repeat(43))
        assertEquals("field phone", uploader.pair(p))
        assertEquals(Settings(url = p.url, token = p.token, device = "field phone"), store.now.settings)
        drop.failAt = { DropException(401, "this door takes a paired device's token") }
        val before = store.now
        val e = kotlin.runCatching { uploader.pair(Pairing("https://y.example.org", "u".repeat(43))) }.exceptionOrNull()
        assertTrue(e is DropException && e.status == 401)
        assertEquals(before, store.now)                                          // a code that does not work replaces nothing
    }
}
