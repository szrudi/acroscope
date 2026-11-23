package org.acroscope.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.junit.AfterClass
import org.junit.Assume
import org.junit.BeforeClass
import java.io.File
import java.net.ServerSocket
import java.nio.file.Files
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * The client against the server it is for: a real `acroscope serve` child on a scratch data dir, a device paired
 * through its API, and what the client sent looked at on the server's disk. Needs the repo's Python
 * (`ACROSCOPE_PYTHON`, else `.venv/bin/python` at the repo's root); skipped where there is none.
 */
class DropContractTest {
    companion object {
        private var server: Process? = null
        private lateinit var root: File
        private lateinit var url: String
        private lateinit var device: Pair<String, String>          // id, token
        private val http = OkHttpClient()
        private var n = 0
        const val QUOTA = 2_000_000L

        @BeforeClass
        @JvmStatic
        fun start() {
            val repo = File("../..").canonicalFile
            val named = System.getenv("ACROSCOPE_PYTHON")
            val python = when {
                named == null -> File(repo, ".venv/bin/python")
                '/' in named -> File(named)
                else -> System.getenv("PATH").orEmpty().split(File.pathSeparator).map { File(it, named) }.firstOrNull { it.canExecute() } ?: File(named)
            }
            check(named == null || python.canExecute()) { "ACROSCOPE_PYTHON=$named is not a Python that can be run" }   // asked for: not skipped quietly
            Assume.assumeTrue("no Python to run the server with (set ACROSCOPE_PYTHON)", python.canExecute())
            root = Files.createTempDirectory("drop").toFile()
            File(root, "data/videos/2026-10-11-park").mkdirs()
            File(root, "data/videos/2026-10-11-park/2026-10-11_001.mp4").writeBytes(ByteArray(0))
            val port = ServerSocket(0).use { it.localPort }
            url = "http://127.0.0.1:$port"
            val pb = ProcessBuilder(python.path, "-m", "acroscope.cli", "serve", "--host", "127.0.0.1", "--port", port.toString(), "--no-warm")
                .redirectErrorStream(true).redirectOutput(File(root, "server.log"))
            pb.environment().apply {
                put("PYTHONPATH", repo.path); put("ACROSCOPE_URL", ""); put("ACROSCOPE_DB", File(root, "t.db").path)
                put("ACROSCOPE_DATA", File(root, "data").path); put("ACROSCOPE_CACHE", File(root, "cache").path)
                put("ACROSCOPE_DROP_QUOTA", QUOTA.toString()); put("ACROSCOPE_DROP_MIN_FREE", "0")
            }
            server = pb.start()
            val up = (1..100).any {
                try { http.newCall(Request.Builder().url("$url/api/store").build()).execute().use { it.code == 200 } } catch (e: Exception) { Thread.sleep(100); false }
            }
            check(up) { "the server did not come up: " + File(root, "server.log").readText().takeLast(2000) }
            http.newCall(Request.Builder().url("$url/api/scan").post(ByteArray(0).toRequestBody(null)).build()).execute().close()   // the session folder into the database
            device = pair("contract phone")
        }

        @AfterClass
        @JvmStatic
        fun stop() {
            server?.destroy()
            server?.waitFor()
        }

        fun pair(name: String): Pair<String, String> =
            http.newCall(Request.Builder().url("$url/api/devices").post("""{"name": "$name"}""".toRequestBody(null)).build()).execute().use {
                val o = Json.parseToJsonElement(it.body.string()).jsonObject
                o["id"]!!.jsonPrimitive.content to o["token"]!!.jsonPrimitive.content
            }

        fun batch(): String = "20261212-0905%02d-ct%04d".format(n / 100, n++)
    }

    private val client get() = DropClient(Pairing(url, device.second), piece = 1000)
    private fun staged(batch: String, name: String) = File(root, "data/drop/${device.first}/$batch/files/$name")
    private fun bytes(n: Int, seed: Int = 1) = ByteArray(n) { ((it * 31 + seed) % 251).toByte() }
    private fun file(n: Int, seed: Int = 1) = File.createTempFile("clip", ".mov", root).apply { writeBytes(bytes(n, seed)) }

    @Test
    fun `the server says who this device is`() {
        assertEquals(Device(device.first, "contract phone", QUOTA, client.device().staged, 4L shl 30), client.device())
    }

    @Test
    fun `a file goes up in pieces and is the same on the server`() {
        val b = batch()
        val f = file(10_500)
        val progress = mutableListOf<Long>()
        client.upload(b, "VID00001.mov", f, progress = { progress += it })
        assertEquals(f.readBytes().toList(), staged(b, "VID00001.mov").readBytes().toList())
        assertEquals(0L, progress.first())
        assertEquals(10_500L, progress.last())
        assertEquals(progress.sorted(), progress)
        assertTrue((1000L..10_000L step 1000).all { it in progress }, progress.toString())     // a piece at a time
        client.upload(b, "VID00001.mov", f, progress = { progress += it })                     // again: nothing left to send
        assertEquals(10_500L, staged(b, "VID00001.mov").length())
    }

    @Test
    fun `an upload that stopped goes on from where the server has it`() {
        val b = batch()
        val f = file(8_000)
        var pieces = 0
        assertFailsWith<DropStopped> { client.upload(b, "VID00001.mov", f, stop = { pieces++ >= 3 }) }
        assertEquals(3000L, staged(b, "VID00001.mov").length())                               // three pieces went up before the stop
        val progress = mutableListOf<Long>()
        client.upload(b, "VID00001.mov", f, progress = { progress += it })
        assertEquals(3000L, progress.first())                                                 // asked the server, did not start over
        assertEquals(f.readBytes().toList(), staged(b, "VID00001.mov").readBytes().toList())
    }

    @Test
    fun `another file under a name that is staged takes its place`() {
        val b = batch()
        client.upload(b, "VID00001.mov", file(3_000, seed = 1))
        val other = file(3_500, seed = 2)
        client.upload(b, "VID00001.mov", other)
        assertEquals(other.readBytes().toList(), staged(b, "VID00001.mov").readBytes().toList())
    }

    @Test
    fun `the sidecar is sent whole each time`() {
        val b = batch()
        client.put(b, "import.json", """{"date": "2026-12-12"}""".toByteArray())
        client.put(b, "import.json", """{"date": "2026-12-13"}""".toByteArray())               // the same length, another day
        assertEquals("""{"date": "2026-12-13"}""", staged(b, "import.json").readText())
    }

    @Test
    fun `a committed batch is in the inbox whole, and the device hears how it is doing`() {
        val b = batch()
        val clip = file(4_200)
        val log = File.createTempFile("log", ".bbl", root).apply { writeText("H Product:Blackbox flight data recorder by Nicholas Sherlock\n" + "x".repeat(700)) }
        val sidecar = Batch(b, "whoop race", "2026-12-12", "2026-12-12T09:05:07+01:00").sidecar("contract phone").toByteArray()
        client.upload(b, "VID00001.mov", clip)
        client.upload(b, "BTFL_BLACKBOX_LOG_OTTO_20261212_084500_BETAFPVG473_V2.bbl", log)
        client.put(b, "import.json", sidecar)
        val manifest = mapOf("VID00001.mov" to clip.length(), "BTFL_BLACKBOX_LOG_OTTO_20261212_084500_BETAFPVG473_V2.bbl" to log.length(), "import.json" to sidecar.size.toLong())
        assertFalse(File(root, "data/inbox/$b").exists())
        val status = client.commit(b, manifest)
        assertEquals(ServerStatus("pending"), status)
        val inbox = File(root, "data/inbox/$b")
        assertEquals((manifest.keys + ".done").sorted(), inbox.list()!!.sorted())
        assertEquals(clip.readBytes().toList(), File(inbox, "VID00001.mov").readBytes().toList())
        assertEquals("whoop race", Json.parseToJsonElement(File(inbox, "import.json").readText()).jsonObject["session"]!!.jsonPrimitive.content)
        val done = Json.parseToJsonElement(File(inbox, ".done").readText()).jsonObject["files"]!!.jsonObject
        assertEquals(manifest, done.mapValues { it.value.jsonPrimitive.content.toLong() })
        assertEquals(status, client.commit(b, manifest))                                       // a lost answer asked for again
        assertEquals(ServerStatus("pending"), client.batches()[b])
        val conflict = assertFailsWith<DropException> { client.retry(b) }                      // nothing failed: nothing to try again
        assertEquals(409, conflict.status)
        val spent = assertFailsWith<DropException> { client.upload(b, "VID00002.mov", clip) }  // and the id is used
        assertEquals(409, spent.status)
        assertTrue("already handed in" in spent.message!!, spent.message)
    }

    @Test
    fun `recent events come from the server's sessions`() {
        assertEquals(listOf("park"), client.events())
    }

    @Test
    fun `what the server refuses is said with its status and its words`() {
        val gone = pair("revoked phone")
        http.newCall(Request.Builder().url("$url/api/devices/${gone.first}").delete().build()).execute().close()
        for (token in listOf(gone.second, "x".repeat(43))) {
            val e = assertFailsWith<DropException> { DropClient(Pairing(url, token)).device() }
            assertEquals(401, e.status)
            assertTrue("token" in e.message!!, e.message)
        }
        val big = assertFailsWith<DropException> { client.upload(batch(), "VID00001.mov", file((QUOTA + 1).toInt())) }
        assertEquals(507, big.status)
        assertTrue("full" in big.message!!, big.message)
        assertEquals(400, assertFailsWith<DropException> { client.upload(batch(), "notes.txt", file(10)) }.status)
        assertEquals(400, assertFailsWith<DropException> { client.upload("b1", "VID00001.mov", file(10)) }.status)
        assertEquals(404, assertFailsWith<DropException> { client.commit(batch(), mapOf("VID00001.mov" to 10L)) }.status)
        assertEquals(404, assertFailsWith<DropException> { client.retry(batch()) }.status)
    }

    @Test
    fun `a name with a space or a hash goes up under that name`() {
        val b = batch()
        val f = file(1_200)
        client.upload(b, "dump #2 (old).bbl", f)
        assertEquals(1_200L, staged(b, "dump #2 (old).bbl").length())
        client.put(b, "dump #2 (old).bbl", "H Product:Blackbox".toByteArray())
        assertEquals("H Product:Blackbox", staged(b, "dump #2 (old).bbl").readText())
    }
}
