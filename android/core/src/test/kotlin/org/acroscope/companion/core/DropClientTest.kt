package org.acroscope.companion.core

import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import java.io.File
import java.io.IOException
import java.net.InetSocketAddress
import java.nio.file.Files
import kotlin.test.AfterTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

/** The client's handling of what a real server only does on a bad day: a stand-in server scripted per request. */
class DropClientTest {
    private val requests = mutableListOf<String>()
    private val connections = mutableSetOf<Int>()                           // the client's port per request: one per connection
    private var respond: (HttpExchange, String) -> Unit = { x, _ -> x.sendResponseHeaders(500, -1) }
    private val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0).apply {
        createContext("/") { x ->
            val line = "${x.requestMethod} ${x.requestURI.rawPath} at=${x.requestHeaders.getFirst("Upload-Offset") ?: "-"}"
            synchronized(requests) { requests += line; connections += x.remoteAddress.port }
            try { respond(x, line) } finally { x.close() }
        }
        start()
    }
    private val pauses = mutableListOf<Long>()
    private val client = DropClient(Pairing("http://127.0.0.1:${server.address.port}", "t".repeat(43)), piece = 100, pause = { pauses += it })
    private val file = File.createTempFile("clip", ".mov", Files.createTempDirectory("dc").toFile()).apply { writeBytes(ByteArray(250) { it.toByte() }) }
    private val b = "20261212-090507-ab12cd"

    @AfterTest
    fun stop() = server.stop(0)

    private fun HttpExchange.answer(status: Int, headers: Map<String, String> = emptyMap(), body: String = "") {
        requestBody.readAllBytes()
        headers.forEach { (k, v) -> responseHeaders.add(k, v) }
        val bytes = body.toByteArray()
        sendResponseHeaders(status, if (bytes.isEmpty()) -1 else bytes.size.toLong())
        if (bytes.isNotEmpty()) responseBody.write(bytes)
    }

    /** A server that takes pieces, with `patch` deciding each PATCH: the status and where the file then ends. */
    private fun serve(patch: (at: Long, n: Int) -> Pair<Int, Long>) {
        var here = 0L
        var n = 0
        respond = { x, _ ->
            when (x.requestMethod) {
                "POST" -> x.answer(201, mapOf("Location" to "/drop/batches/$b/files/VID00001.mov"))
                "HEAD" -> x.answer(200, mapOf("Upload-Offset" to here.toString()))
                "PATCH" -> {
                    val (status, end) = patch(x.requestHeaders.getFirst("Upload-Offset").toLong(), n++)
                    here = end
                    x.answer(status, if (status == 204) mapOf("Upload-Offset" to end.toString()) else emptyMap(), if (status == 204) "" else """{"error": "refused $status"}""")
                }
                else -> x.answer(405)
            }
        }
    }

    @Test
    fun `every request says who it is from and which protocol it speaks`() {
        var seen: Map<String, String> = emptyMap()
        respond = { x, _ -> seen = x.requestHeaders.mapValues { it.value.first() }.mapKeys { it.key.lowercase() }; x.answer(200, body = """{"events": ["park"]}""") }
        assertEquals(listOf("park"), client.events())
        assertEquals("Bearer " + "t".repeat(43), seen["authorization"])
        assertEquals("1.0.0", seen["tus-resumable"])
    }

    @Test
    fun `one connection serves call after call, whichever client object makes it`() {
        respond = { x, _ -> x.answer(200, body = """{"events": []}""") }
        val pairing = Pairing("http://127.0.0.1:${server.address.port}", "t".repeat(43))
        repeat(3) { DropClient(pairing).events() }                              // as the app does: a client per call
        client.events()
        assertEquals(4, requests.size)
        assertEquals(1, connections.size, connections.toString())
    }

    @Test
    fun `a file that is busy is waited for, and not forever`() {
        serve { at, n -> if (n < 2) 423 to at else 204 to at + 100L.coerceAtMost(250 - at) }
        client.upload(b, "VID00001.mov", file)
        assertEquals(listOf(2000L, 2000L), pauses)
        assertEquals(listOf("at=0", "at=0", "at=0", "at=100", "at=200"), requests.filter { it.startsWith("PATCH") }.map { it.substringAfter(' ').substringAfter(' ') })
        pauses.clear()
        serve { at, _ -> 423 to at }                                            // held by something that never lets go
        val e = assertFailsWith<DropException> { client.upload(b, "VID00001.mov", file) }
        assertEquals(423, e.status)
        assertEquals(45, pauses.size)                                           // 90 s: past the server's own minute of patience, then given up
    }

    @Test
    fun `when the server has other bytes than thought, the upload goes on from where it is`() {
        var first = true
        serve { at, _ -> if (first) { first = false; 409 to 150L } else 204 to at + 100L.coerceAtMost(250 - at) }   // an earlier piece landed after all
        val progress = mutableListOf<Long>()
        client.upload(b, "VID00001.mov", file, progress = { progress += it })
        assertEquals(listOf("at=0", "at=150"), requests.filter { it.startsWith("PATCH") }.map { it.substringAfterLast(' ') })
        assertEquals(250L, progress.last())
    }

    @Test
    fun `a refusal in the middle ends the upload with the server's words`() {
        serve { at, n -> if (n == 1) 507 to at else 204 to at + 100 }
        val e = assertFailsWith<DropException> { client.upload(b, "VID00001.mov", file) }
        assertEquals(507 to "refused 507", e.status to e.message)
        assertEquals(2, requests.count { it.startsWith("PATCH") })               // and nothing more was sent
    }

    @Test
    fun `a sidecar the server does not take is not passed over`() {
        respond = { x, _ ->
            when (x.requestMethod) {
                "DELETE" -> x.answer(404)
                "POST" -> x.answer(201, mapOf("Location" to "/drop/batches/$b/files/import.json"))
                else -> x.answer(507, body = """{"error": "no room on the server's disk"}""")
            }
        }
        val e = assertFailsWith<DropException> { client.put(b, "import.json", "{}".toByteArray()) }
        assertEquals(507 to "no room on the server's disk", e.status to e.message)
        respond = { x, _ -> x.answer(if (x.requestMethod == "DELETE") 423 else 204) }      // nor one that cannot be replaced right now
        assertEquals(423, assertFailsWith<DropException> { client.put(b, "import.json", "{}".toByteArray()) }.status)
    }

    @Test
    fun `an answer that is not the server's own is still an error with its status`() {
        respond = { x, _ -> x.answer(502, body = "<html>Bad Gateway</html>") }   // a proxy's page
        val e = assertFailsWith<DropException> { client.device() }
        assertEquals(502, e.status)
        assertTrue("502" in e.message!!, e.message)
    }

    @Test
    fun `an answer that is not the drop's is no answer, not a refusal and not a crash`() {
        val pages = listOf(
            "<html>log in to the Wi-Fi</html>",                                   // a portal or a login page, with a 200
            """{"name": "field phone"}""",                                       // a JSON object without what was asked for
            """{"id": 7, "name": [], "quota": "lots", "staged": 0, "max_file": 1}""",
            """[{"id": "$b"}]""",                                                // a list of batches without their status
            """{"events": "park"}""",
            "",
        )
        for (page in pages) {
            respond = { x, _ -> x.answer(200, body = page) }
            for (call in listOf<() -> Unit>({ client.device() }, { client.events() }, { client.batches() }, { client.commit(b, mapOf("x.bbl" to 1L)) }, { client.retry(b) })) {
                val e = assertFailsWith<IOException>(page) { call() }
                assertTrue(e !is DropException, "$page: $e")                    // tried again later, like no answer at all
            }
        }
    }

    @Test
    fun `an upload whose answers lack the offset is no answer`() {
        respond = { x, _ -> if (x.requestMethod == "POST") x.answer(201, mapOf("Location" to "/drop/batches/$b/files/VID00001.mov")) else x.answer(200) }
        assertTrue(assertFailsWith<IOException> { client.upload(b, "VID00001.mov", file) } !is DropException)     // HEAD without Upload-Offset
        serve { _, _ -> 204 to 0L }                                             // a piece taken, and the file said to end where it did
        assertTrue(assertFailsWith<IOException> { client.upload(b, "VID00001.mov", file) } !is DropException)
        assertEquals(1, requests.count { it.startsWith("PATCH") })              // not sent again and again
        requests.clear()
        respond = { x, _ ->
            when (x.requestMethod) {
                "POST" -> x.answer(201, mapOf("Location" to "/drop/batches/$b/files/VID00001.mov"))
                "HEAD" -> x.answer(200, mapOf("Upload-Offset" to "0"))
                else -> x.answer(204, mapOf("Upload-Offset" to "many"))
            }
        }
        assertTrue(assertFailsWith<IOException> { client.upload(b, "VID00001.mov", file) } !is DropException)
    }

    @Test
    fun `a server that is not there is an io error, not a refusal`() {
        server.stop(0)
        val e = assertFailsWith<IOException> { client.device() }
        assertTrue(e !is DropException)
    }

    @Test
    fun `a batch's status is read with what it became`() {
        respond = { x, _ ->
            x.answer(200, body = """[{"id": "$b", "status": "done", "session": "2026-12-12-whoop-race", "error": null, "progress": null,
                "result": {"clips": ["2026-12-12_001", "2026-12-12_002"], "logs": ["x.bbl"]}, "clips": 2, "logs": 1},
                {"id": "20261212-100000-zz99zz", "status": "failed", "session": null, "error": "ValueError: VID00003.mov: moov atom not found", "result": null, "clips": 1, "logs": 0}]""")
        }
        assertEquals(mapOf(
            b to ServerStatus("done", "2026-12-12-whoop-race", null, listOf("2026-12-12_001", "2026-12-12_002"), listOf("x.bbl")),
            "20261212-100000-zz99zz" to ServerStatus("failed", null, "ValueError: VID00003.mov: moov atom not found"),
        ), client.batches())
    }
}
