package org.acroscope.companion

import android.content.Intent
import android.net.Uri
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import org.acroscope.companion.core.Batches
import org.acroscope.companion.core.Blackbox
import org.acroscope.companion.core.SerialPort
import org.acroscope.companion.core.DropException
import org.acroscope.companion.core.Format
import org.acroscope.companion.core.Pairing
import org.acroscope.companion.core.UploadResult
import java.io.IOException
import java.io.InputStream

/** The card as the screen shows it: what the phone knows of it and what a collect would take. */
sealed interface CardView {
    data object None : CardView
    data class Locked(val label: String, val request: Intent) : CardView
    data class Open(val label: String, val tree: Uri, val clips: Int, val bytes: Long, val collected: Int, val others: List<String>) : CardView
    data class Unreadable(val label: String, val why: String) : CardView
}

/** The quad as the screen shows it. */
sealed interface QuadView {
    data object None : QuadView
    data class Locked(val name: String, val ask: () -> Unit) : QuadView
    data class Ready(val name: String) : QuadView
    /** Its log was saved and verified; erasing its flash is on offer while it stays plugged in. */
    data class Saved(val name: String, val log: String) : QuadView
}

/** A job the screen follows: a collect, an upload or an erase. */
sealed interface Work {
    data object Idle : Work
    data class Running(val what: String, val done: Long, val total: Long) : Work
    data class Finished(val message: String, val ok: Boolean) : Work
}

/** A file the pilot picked: its name, when it was last changed (if the picker says), and its bytes. */
class Picked(val name: String, val modified: Long?, val open: () -> InputStream)

/**
 * What the screen shows beyond the stored state, and what it can ask for. The slow things (reading the card,
 * talking to the server) run off the main thread here; the collect and the upload themselves are `collectNow` and
 * `uploadNow`, which a background job calls (see [Runner]) so they outlive the screen.
 */
class Session(private val g: Graph) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val batches = Batches(g.store)

    val card = MutableStateFlow<CardView>(CardView.None)
    val quad = MutableStateFlow<QuadView>(QuadView.None)
    val erase = MutableStateFlow<Work>(Work.Idle)
    /** The line to a quad whose log was just saved, kept open: only that connection may erase the flash. */
    private var held: Pair<SerialPort, Blackbox>? = null
    val collect = MutableStateFlow<Work>(Work.Idle)
    val upload = MutableStateFlow<Work>(Work.Idle)
    /** The last thing worth telling the pilot that belongs to no job: a pairing, a log taken in, an edit refused. */
    val note = MutableStateFlow<String?>(null)
    val events = MutableStateFlow<List<String>>(emptyList())
    /** A pairing code that came in as a link and waits for the pilot's yes. */
    val pairRequest = MutableStateFlow<String?>(null)

    fun lookAtCard() = scope.launch { card.value = see() }

    private fun see(): CardView = when (val s = g.cards.state()) {
        CardState.None -> CardView.None
        is CardState.NeedsAccess -> CardView.Locked(s.label, s.request)
        is CardState.Ready -> try {
            val plan = g.collector.plan(g.cards.open(s.tree))
            CardView.Open(s.label, s.tree, plan.clips.size, plan.bytes, plan.collected, plan.others)
        } catch (e: Exception) {
            CardView.Unreadable(s.label, e.message ?: e.javaClass.simpleName)
        }
    }

    fun lookAtQuad() = scope.launch { quad.value = seeQuad() }

    private fun seeQuad(): QuadView = when (val s = g.quads.state()) {
        QuadState.None -> QuadView.None.also { letGo() }
        is QuadState.NeedsPermission -> QuadView.Locked(s.name, s.ask)
        is QuadState.Ready -> (quad.value as? QuadView.Saved)?.takeIf { held != null } ?: QuadView.Ready(s.name)
    }

    private fun letGo() {
        held?.let { runCatching { it.first.close() } }
        held = null
    }

    fun allow(tree: Uri) {
        g.cards.grant(tree)
        lookAtCard()
    }

    fun setEvent(name: String) {
        g.store.update { it.copy(settings = it.settings.copy(event = name)) }
    }

    /** One press collects whatever is there: the quad's log, the card's clips, or both. */
    fun startCollect() {
        val open = (card.value as? CardView.Open)?.takeIf { it.clips > 0 }
        val log = quad.value is QuadView.Ready
        if ((open == null && !log) || collect.value is Work.Running) return
        collect.value = Work.Running(if (log) "asking the quad" else "reading the card", 0, open?.bytes ?: 0)
        g.runner.collect(open?.tree)
    }

    /** The collect itself, on the caller's thread: the log first, then the clips, as the laptop's import does. */
    fun collectNow(tree: Uri?, stop: () -> Boolean): Work.Finished {
        val said = mutableListOf<String>()
        var ok = true
        if (g.quads.state() is QuadState.Ready && held == null) {
            try {
                said += saveLog(stop)
            } catch (e: Exception) {
                ok = false
                said += "the log was not saved (${e.message ?: e.javaClass.simpleName}); the quad's flash is as it was"
            }
        }
        if (tree != null) {
            val out = try {
                g.collector.collect(g.cards.open(tree), g.store.free, { p -> collect.value = Work.Running("${p.name} (${p.index} of ${p.count})", p.done, p.total) }, stop)
            } catch (e: Exception) {
                org.acroscope.companion.core.Outcome(emptyList(), e.message ?: e.javaClass.simpleName)
            }
            val flagged = out.items.count { it.unfinalised }
            val got = "${out.items.size} clip${if (out.items.size == 1) "" else "s"} collected" +
                if (flagged > 0) ", $flagged without an index (the recording was cut off: the server cannot read it as it is)" else ""
            if (out.error != null) ok = false
            said += if (out.error == null) got else "${out.error}; $got"
        }
        val done = Work.Finished(said.joinToString("\n").ifEmpty { "nothing to collect" }, ok)
        collect.value = done
        card.value = see()
        quad.value = seeQuad()
        return done
    }

    /** The quad's log into the pending batch. The line stays open afterwards: erasing the flash is offered, and only
     *  the connection that saved and verified the log may do it. */
    private fun saveLog(stop: () -> Boolean): String {
        val name = (g.quads.state() as? QuadState.Ready)?.name ?: "the quad"
        val port = g.quads.open()
        try {
            val box = Blackbox(port, g.clock)
            val saved = box.download(g.store.scratch, { p ->
                collect.value = Work.Running("the log off $name" + (p.temp?.let { " · core $it °C" } ?: ""), p.done, p.total)
            }, stop)
            if (saved == null) {
                port.close()
                return "$name's flash is empty"
            }
            val item = try { g.collector.addLog(saved.file.name, null) { saved.file.inputStream() } } finally { saved.file.delete() }
            held = port to box
            quad.value = QuadView.Saved(name, item.name)
            return "log saved (${Format.bytes(item.size)})" + (saved.peakTemp?.let { ", the core got to $it °C" } ?: "")
        } catch (e: Exception) {
            runCatching { port.close() }
            throw e
        }
    }

    /** Erase the flash of the quad whose log was just saved. Up to a minute; the quad must stay plugged in. */
    fun eraseQuad() = scope.launch {
        val (_, box) = held ?: return@launch
        if (erase.value is Work.Running) return@launch
        erase.value = Work.Running("erasing the quad's flash (up to a minute)", 0, 0)
        erase.value = try {
            box.erase()
            Work.Finished("the quad's flash is erased", true)
        } catch (e: Exception) {
            Work.Finished("the flash was not erased: ${e.message ?: e.javaClass.simpleName}", false)
        }
        letGo()
        quad.value = seeQuad()
    }

    fun startUpload() {
        if (upload.value is Work.Running) return
        upload.value = Work.Running("connecting", 0, g.store.now.pending.sumOf { it.bytes })
        g.runner.upload()
    }

    /** The upload itself, on the caller's thread. */
    fun uploadNow(stop: () -> Boolean): Work.Finished {
        val result = g.uploader.run({ p -> upload.value = Work.Running(p.name, p.done, p.total) }, stop)
        val done = when (result) {
            is UploadResult.Done -> Work.Finished(if (result.batches == 0) "nothing to upload" else "uploaded: the server takes it from here", true)
            UploadResult.NotPaired -> Work.Finished("pair the app with the server first", false)
            UploadResult.Stopped -> Work.Finished("stopped; the next upload goes on from here", false)
            is UploadResult.Unreachable -> Work.Finished("the server did not answer (${result.message}); the next upload goes on from here", false)
            is UploadResult.Broken -> Work.Finished(result.message, false)
            is UploadResult.Refused -> Work.Finished(when (result.status) {
                401 -> "the server does not know this device any more: pair it again"
                507 -> "the server has no room right now: ${result.message}"
                else -> "the server refused: ${result.message}"
            }, false)
        }
        batches.prune()
        upload.value = done
        return done
    }

    fun addLogs(picked: List<Picked>) = scope.launch {
        val said = picked.map { p ->
            try { "${g.collector.addLog(p.name, p.modified, p.open).name} added" } catch (e: Exception) { e.message ?: "${p.name}: could not be read" }
        }
        note.value = said.joinToString("\n")
    }

    fun pair(code: String) = scope.launch {
        note.value = try {
            "paired as ${g.uploader.pair(Pairing.parse(code))}"
        } catch (e: IllegalArgumentException) {
            e.message
        } catch (e: DropException) {
            if (e.status == 401) "the server does not take this code: it was revoked, or it is another server's" else "the server refused: ${e.message}"
        } catch (e: IOException) {
            "the server did not answer: ${e.message}"
        }
        askServer()
    }

    /** How the handed-in batches are doing, and what events may be called. Quiet when the server cannot be asked. */
    fun askServer() = scope.launch {
        try { g.uploader.refresh() } catch (e: IOException) { }
        events.value = g.uploader.events()
    }

    fun retry(batch: String) = scope.launch {
        try { g.uploader.retry(batch) } catch (e: IOException) { note.value = "could not ask the server to try again: ${e.message}" }
    }

    fun edit(batch: String, event: String, date: String): Boolean = change { batches.edit(batch, event, date) }

    fun remove(batch: String, name: String): Boolean = change { batches.remove(batch, name) }

    fun discard(batch: String): Boolean = change { batches.discard(batch) }

    private fun change(what: () -> Unit): Boolean = try {
        what()
        lookAtCard()
        true
    } catch (e: RuntimeException) {
        note.value = e.message
        false
    }

    fun describe(view: QuadView): String = when (view) {
        QuadView.None -> "no quad plugged in"
        is QuadView.Locked -> "${view.name}: allow the app to talk to it"
        is QuadView.Ready -> "${view.name}: its log will be saved"
        is QuadView.Saved -> "${view.name}: log saved as ${view.log}"
    }

    fun describe(view: CardView): String = when (view) {
        CardView.None -> "no card plugged in"
        is CardView.Locked -> "${view.label}: allow the app to read it"
        is CardView.Unreadable -> "${view.label}: could not be read (${view.why})"
        is CardView.Open -> (if (view.clips == 0) "${view.label}: nothing new" else "${view.label}: ${view.clips} new clip${if (view.clips == 1) "" else "s"}, ${Format.bytes(view.bytes)}") +
            (if (view.collected > 0) ", ${view.collected} collected before" else "") +
            (if (view.others.isNotEmpty()) "\nalso on the card, not collected: ${view.others.take(5).joinToString(", ")}${if (view.others.size > 5) " and ${view.others.size - 5} more" else ""}" else "")
    }
}
