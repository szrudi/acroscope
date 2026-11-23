package org.acroscope.companion

import android.content.Intent
import android.net.Uri
import org.acroscope.companion.core.Card
import org.acroscope.companion.core.CardFile
import org.acroscope.companion.core.Device
import org.acroscope.companion.core.Drop
import org.acroscope.companion.core.DropException
import org.acroscope.companion.core.DropStopped
import org.acroscope.companion.core.Pairing
import org.acroscope.companion.core.ServerStatus
import org.acroscope.companion.core.Store
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.nio.ByteBuffer
import java.nio.file.Files
import java.time.ZonedDateTime

val TREE: Uri = Uri.parse("content://com.android.externalstorage.documents/tree/1234-ABCD%3A")
const val CODE = "acroscope://pair?url=https%3A%2F%2Fdrop.example.org&token=sII2Bs1adMHfku5e1T3FYIh9W1U7m8OASln7pIJCU08"

/** A movie as the DVR writes one, with its index or cut off before it. */
fun movie(payload: Int, finished: Boolean = true): ByteArray {
    fun atom(type: String, n: Int) = ByteBuffer.allocate(8 + n).putInt(8 + n).put(type.toByteArray()).array()
    return atom("ftyp", 12) + atom("mdat", payload) + if (finished) atom("moov", 40) else ByteArray(0)
}

class FakeFile(override val name: String, private val data: ByteArray, override val modified: Long = 1000) : CardFile {
    override val size: Long get() = data.size.toLong()
    override fun open(): InputStream = ByteArrayInputStream(data)
}

class FakeCards(var now: CardState = CardState.None, var files: List<CardFile> = emptyList()) : Cards {
    val granted = mutableListOf<Uri>()
    override fun state(): CardState = now
    override fun grant(tree: Uri) { granted += tree; now = CardState.Ready("COBRA", tree) }
    override fun open(tree: Uri): Card = object : Card {
        override val label = "COBRA"
        override fun files(): List<CardFile> = files
    }
}

/** The jobs run where they are asked for: on the caller's thread, to their end. */
class InlineRunner : Runner {
    lateinit var graph: Graph
    val ran = mutableListOf<String>()
    override fun collect(tree: Uri) { ran += "collect"; graph.session.collectNow(tree) { false } }
    override fun upload() { ran += "upload"; graph.session.uploadNow { false } }
}

class FakeDrop : Drop {
    val calls = mutableListOf<String>()
    val statuses = mutableMapOf<String, ServerStatus>()
    var fail: IOException? = null
    var recent = listOf("home")
    private fun call(what: String) { calls += what; fail?.let { throw it } }
    override fun device(): Device { call("device"); return Device("d0123456789", "field phone", 1L shl 30, 0, 4L shl 30) }
    override fun events(): List<String> { call("events"); return recent }
    override fun batches(): Map<String, ServerStatus> { call("batches"); return statuses.toMap() }
    override fun upload(batch: String, name: String, file: File, progress: (Long) -> Unit, stop: () -> Boolean) {
        call("upload $batch $name")
        if (stop()) throw DropStopped()
        progress(file.length())
    }
    override fun put(batch: String, name: String, bytes: ByteArray) = call("put $batch $name")
    override fun commit(batch: String, files: Map<String, Long>): ServerStatus { call("commit $batch"); return ServerStatus("pending").also { statuses[batch] = it } }
    override fun retry(batch: String): ServerStatus { call("retry $batch"); return ServerStatus("pending").also { statuses[batch] = it } }
}

/** The app with a scratch store, a card and a server that are stand-ins, and jobs that run inline. */
class World(val drop: FakeDrop = FakeDrop(), val cards: FakeCards = FakeCards(), var clock: ZonedDateTime = ZonedDateTime.now()) {
    val root: File = Files.createTempDirectory("world").toFile()
    val runner = InlineRunner()
    val pairings = mutableListOf<Pairing>()
    val graph = Graph(Store(File(root, "state"), File(root, "files")), cards, runner, drop = { pairings += it; drop }, clock = { clock }).also { runner.graph = it }
    val store get() = graph.store
    val session get() = graph.session

    fun paired() = store.update { it.copy(settings = it.settings.copy(url = "https://drop.example.org", token = "t".repeat(43), device = "field phone")) }
    fun card(vararg files: CardFile) { cards.files = files.toList(); cards.now = CardState.Ready("COBRA", TREE) }
    fun locked() { cards.now = CardState.NeedsAccess("COBRA", Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)) }
    fun refused(status: Int, message: String) { drop.fail = DropException(status, message) }
}
