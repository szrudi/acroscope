package org.acroscope.companion

import android.content.Intent
import android.net.Uri
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.acroscope.companion.core.Auto
import org.acroscope.companion.core.Batches
import org.acroscope.companion.core.Blackbox
import org.acroscope.companion.core.SerialGone
import org.acroscope.companion.core.SerialPort
import org.acroscope.companion.core.DropException
import org.acroscope.companion.core.Format
import org.acroscope.companion.core.Item
import org.acroscope.companion.core.Pairing
import org.acroscope.companion.core.Seen
import org.acroscope.companion.core.UploadResult
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/** The card as the screen shows it: what the phone knows of it and what a collect would take. */
sealed interface CardView {
    data object None : CardView
    data class Locked(val label: String, val request: Intent) : CardView
    /** `done`: the clips on it the server reported done, which may be deleted from it (with changes allowed, `writable`). */
    data class Open(val label: String, val tree: Uri, val clips: Int, val bytes: Long, val collected: Int, val others: List<String>,
                    val done: List<Seen> = emptyList(), val writable: Boolean = false, val request: Intent? = null) : CardView {
        val doneBytes: Long get() = done.sumOf { it.size }
    }
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
    /** Deleting from the card the clips the server has. */
    val free = MutableStateFlow<Work>(Work.Idle)
    /** The line to a quad whose log was just saved, kept open: only that connection may erase the flash. With it,
     *  which device it is (a quad swapped meanwhile is another) and the log it saved (the erase is on offer only
     *  while that log is still on the phone). */
    private class Held(val port: SerialPort, val box: Blackbox, val device: String, val log: Item)
    @Volatile private var held: Held? = null
    /** The quad's view is worked out and shown in one step: one worked out before a save ended must not cover it. */
    private val seeing = Any()
    /** The card's view likewise: one read before a free deleted must not land after the view read once it did. */
    private val looking = Any()
    /** The quad's core temperature as last read, in °C: while it is plugged in and the screen is there to show it. */
    val heat = MutableStateFlow<Int?>(null)
    /** How much log is on the quad's flash as last read, in bytes: 0 is an empty flash, null is not known (not asked
     *  yet, no answer, or a board without a flash). */
    val flash = MutableStateFlow<Long?>(null)
    /** One talker on the quad's line at a time: a save, an erase, or a reading of the temperature and the flash. */
    private val wire = ReentrantLock()
    /** The line the temperature is read on while no saved log holds one open. Let go when the screen is left: only
     *  one app can talk to a flight controller. */
    @Volatile private var watch: Pair<SerialPort, Blackbox>? = null
    @Volatile private var watched = false
    private var watching: Job? = null
    val collect = MutableStateFlow<Work>(Work.Idle)
    val upload = MutableStateFlow<Work>(Work.Idle)
    /** The last thing worth telling the pilot that belongs to no job: a pairing, a log taken in, an edit refused. */
    val note = MutableStateFlow<String?>(null)
    val events = MutableStateFlow<List<String>>(emptyList())
    /** A pairing code that came in as a link and waits for the pilot's yes. */
    val pairRequest = MutableStateFlow<String?>(null)

    /**
     * A finished job's line says what just happened, and goes when what it speaks of has moved on: the card or the
     * quad it was about is another one or gone, a new collect makes the last erase and the last upload old news, and
     * a line the pilot has seen is not there again when they come back to the screen. What went wrong stays until
     * the next try: `failures` false keeps those lines.
     */
    private fun settle(vararg lines: MutableStateFlow<Work>, failures: Boolean = true) = lines.forEach {
        val done = it.value as? Work.Finished ?: return@forEach
        if (failures || done.ok) it.value = Work.Idle
    }

    /** The screen was left. */
    fun left() = settle(collect, erase, upload, free, failures = false)

    private fun plugged(view: CardView): String? = when (view) {
        CardView.None -> null
        is CardView.Locked -> view.label
        is CardView.Open -> view.label
        is CardView.Unreadable -> view.label
    }

    private fun plugged(view: QuadView): String? = when (view) {
        QuadView.None -> null
        is QuadView.Locked -> view.name
        is QuadView.Ready -> view.name
        is QuadView.Saved -> view.name
    }

    fun lookAtCard() = scope.launch { showCard(plug = true) }

    /** Read the card and show it; `plug`: a look from outside, where another card (or none) takes the last lines. */
    private fun showCard(plug: Boolean) = synchronized(looking) {
        val now = see()
        if (plug && plugged(now) != plugged(card.value)) settle(collect, free)
        card.value = now
    }

    private fun see(): CardView = when (val s = g.cards.state()) {
        CardState.None -> CardView.None
        is CardState.NeedsAccess -> CardView.Locked(s.label, s.request)
        is CardState.Ready -> try {
            val plan = g.collector.plan(g.cards.open(s.tree))
            CardView.Open(s.label, s.tree, plan.clips.size, plan.bytes, plan.collected, plan.others,
                plan.done.map { Seen(it.name, it.size, it.modified) }, s.writable, s.request)
        } catch (e: Exception) {
            CardView.Unreadable(s.label, e.message ?: e.javaClass.simpleName)
        }
    }

    fun lookAtQuad() = scope.launch {
        val (before, now) = synchronized(seeing) { quad.value to showQuad() }
        if (plugged(now) != plugged(before)) settle(collect, erase)
        if (now is QuadView.Ready) feel(looping = true)     // just plugged in or allowed: ask it now, not at the next reading
    }

    /** Work out what the quad's line says and show it, in one step (see [seeing]). */
    private fun showQuad(): QuadView = synchronized(seeing) { seeQuad().also { quad.value = it } }

    private fun seeQuad(): QuadView = when (val s = g.quads.state()) {
        QuadState.None -> QuadView.None.also { letGo() }
        is QuadState.NeedsPermission -> QuadView.Locked(s.name, s.ask)
        is QuadState.Ready -> {
            val h = held
            when {
                h == null -> QuadView.Ready(s.name)
                erase.value is Work.Running -> QuadView.Saved(s.name, h.log.name)   // never the line from under an erase
                h.device != s.id || !kept(h) -> QuadView.Ready(s.name).also { letGo() }   // another quad, or its log is gone
                else -> QuadView.Saved(s.name, h.log.name)
            }
        }
    }

    /** The saved log is still on the phone, or with the server: what the erase stands on. */
    private fun kept(h: Held): Boolean = g.store.now.batches.any { h.log in it.items }

    private fun letGo() {
        held?.let { runCatching { it.port.close() } }
        held = null
        unwatch()
        forget()
    }

    /** What was read off the quad is about a quad that is gone, or one that did not answer. */
    private fun forget() {
        heat.value = null
        flash.value = null
    }

    private fun unwatch() {
        watch?.let { runCatching { it.first.close() } }
        watch = null
    }

    /** The screen is there (`on`) or left: the quad's temperature and what is on its flash are read every few seconds
     *  while someone can see them. */
    fun watch(on: Boolean): Job {
        watched = on
        watching?.cancel()
        return scope.launch {
            if (on) while (isActive) {
                feel(looping = true)
                delay(WATCH_MS)
            } else wire.withLock { unwatch() }
        }.also { watching = it }
    }

    /** One reading of the quad's temperature and of how much log its flash holds, unless a save or an erase has its
     *  line: those say it themselves. */
    fun feel(looping: Boolean = false) {
        if (!wire.tryLock()) return
        try {
            if (looping && !watched) return             // the screen was left while this reading waited its turn
            if (g.quads.state() !is QuadState.Ready) {
                unwatch()
                forget()
                return
            }
            val box = held?.box ?: (watch ?: g.quads.open().let { it to Blackbox(it, g.clock) }.also { watch = it }).second
            heat.value = box.temp()
            flash.value = box.flash().takeIf { it.present }?.used
        } catch (e: Exception) {            // unplugged under the question, or held by another app: nothing to show
            unwatch()
            forget()
        } finally {
            wire.unlock()
        }
    }

    /** How long a quad that dropped off USB under the erase is waited for. */
    @Volatile var returnMs = 30_000L

    /**
     * A quad can restart under the erase (some boards drop off USB when the chip is done, or while it erases): the
     * erase's own line is then gone. Wait for the quad to come back by the same name, and read its flash on a new
     * line: empty is erased.
     */
    private suspend fun emptyWhenBack(name: String?): Boolean {
        if (name == null) return false
        erase.value = Work.Running("the quad dropped off USB during the erase (it may have restarted): keep it plugged in, waiting for it to come back", 0, 0)
        val until = System.currentTimeMillis() + returnMs
        while (System.currentTimeMillis() < until) {
            delay(1000)
            val s = g.quads.state() as? QuadState.Ready ?: continue
            if (s.name != name) continue
            val flash = wire.withLock {
                runCatching { g.quads.open().use { Blackbox(it, g.clock).flash() } }.getOrNull()
            } ?: continue
            if (flash.present && flash.ready) return flash.used == 0L
        }
        return false
    }

    fun allow(tree: Uri) {
        g.cards.grant(tree)
        lookAtCard()
    }

    /** Delete from the card the clips the server reported done that `asked`, the card as the question showed it, named;
     *  the pilot pressed for it and said yes. */
    fun freeCard(asked: CardView.Open) = scope.launch {
        synchronized(seeing) {
            if (free.value is Work.Running || collect.value is Work.Running || !asked.writable || asked.done.isEmpty()) return@launch
            free.value = Work.Running("deleting ${asked.done.size} clip${if (asked.done.size == 1) "" else "s"} the server has from the card: keep it in", 0, 0)
        }
        free.value = try {
            val out = g.collector.free(g.cards.open(asked.tree), asked.done)
            val said = "deleted ${out.count} clip${if (out.count == 1) "" else "s"} (${Format.bytes(out.bytes)}) from the card"
            if (out.failed.isEmpty()) Work.Finished(said, true)
            else Work.Finished("$said; not deleted: ${out.failed.joinToString("; ")}", false)
        } catch (e: Exception) {
            Work.Finished("nothing was deleted: ${e.message ?: e.javaClass.simpleName}", false)
        }
        showCard(plug = false)
    }

    fun setEvent(name: String) {
        g.store.update { it.copy(settings = it.settings.copy(event = name)) }
    }

    /** One press collects whatever is there: the quad's log, the card's clips, or both. */
    fun startCollect() {
        val open = (card.value as? CardView.Open)?.takeIf { it.clips > 0 }
        val log = quad.value is QuadView.Ready && flash.value != 0L
        synchronized(seeing) {
            if ((open == null && !log) || collect.value is Work.Running || free.value is Work.Running) return
            settle(erase, upload, free)
            collect.value = Work.Running(if (log) "asking the quad" else "reading the card", 0, open?.bytes ?: 0)
        }
        g.runner.collect(open?.tree)
    }

    /** The collect itself, on the caller's thread: the log first, then the clips, as the laptop's import does. */
    fun collectNow(tree: Uri?, stop: () -> Boolean): Work.Finished {
        val said = mutableListOf<String>()
        var ok = true
        if (g.quads.state() is QuadState.Ready && held == null) {
            try {
                said += wire.withLock {
                    unwatch()                       // the save opens the line itself, and keeps it for the erase
                    saveLog(stop)
                }
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
        showCard(plug = false)
        showQuad()
        kick()
        return done
    }

    /** Uploading by itself, switched on or off; on, it starts as soon as the phone is on a network that costs nothing. */
    fun setAuto(on: Boolean) {
        g.store.update { it.copy(settings = it.settings.copy(autoUpload = on)) }
        kick()
    }

    /** Ask for an unattended upload when one is wanted, and for a later look at what the server is still busy with. */
    fun kick() {
        if (Auto.wanted(g.store.now)) g.runner.auto()
        if (Auto.following(g.store.now)) g.runner.follow()
    }

    /** The quad's log into the pending batch. The line stays open afterwards: erasing the flash is offered, and only
     *  the connection that saved and verified the log may do it. */
    private fun saveLog(stop: () -> Boolean): String {
        val plugged = g.quads.state() as? QuadState.Ready
        val name = plugged?.name ?: "the quad"
        val port = g.quads.open()
        try {
            val box = Blackbox(port, g.clock)
            val saved = box.download(g.store.scratch, { p ->
                collect.value = Work.Running("the log off $name", p.done, p.total)
                heat.value = p.temp
            }, stop)
            if (saved == null) {
                port.close()
                flash.value = 0L
                return "$name's flash is empty"
            }
            val item = try { g.collector.addLog(saved.file.name, null) { saved.file.inputStream() } } finally { saved.file.delete() }
            synchronized(seeing) {
                held = Held(port, box, plugged?.id ?: name, item)
                quad.value = QuadView.Saved(name, item.name)
            }
            return "log saved (${Format.bytes(item.size)})" + (saved.peakTemp?.let { ", the core got to $it °C" } ?: "")
        } catch (e: Exception) {
            runCatching { port.close() }
            throw e
        }
    }

    /** Erase the flash of the quad whose log was just saved. Up to a minute; the quad must stay plugged in. */
    fun eraseQuad() = scope.launch {
        val h = held ?: return@launch
        synchronized(seeing) {                              // two taps: one erase
            if (erase.value is Work.Running) return@launch
            if (!kept(h)) {                                 // the log it would stand on is gone: no erase, and none offered
                showQuad()
                return@launch
            }
            erase.value = Work.Running("erasing the quad's flash: keep it plugged in until this says it is done (up to a minute)", 0, 0)
        }
        val box = h.box
        val name = (quad.value as? QuadView.Saved)?.name
        val done = try {
            wire.withLock { box.erase() }
            Work.Finished("the quad's flash is erased", true)
        } catch (e: Exception) {
            letGo()
            if (e is SerialGone && emptyWhenBack(name)) Work.Finished("the quad's flash is erased (it restarted on the way)", true)
            else Work.Finished("the flash was not erased: ${e.message ?: e.javaClass.simpleName}", false)
        }
        erase.value = done
        letGo()
        if (done.ok) flash.value = 0L           // the erase ends on a flash that says it is empty
        showQuad()
    }

    fun startUpload() {
        if (upload.value is Work.Running) return
        upload.value = Work.Running("connecting", 0, g.store.now.pending.sumOf { it.bytes })
        g.runner.upload()
    }

    /** The upload itself, on the caller's thread; what came of it is on the screen and returned for the job to act on. */
    fun uploadNow(stop: () -> Boolean): UploadResult {
        val result = g.uploader.run({ p -> upload.value = Work.Running(p.name, p.done, p.total) }, stop)
        batches.prune()
        upload.value = say(result)
        if (Auto.following(g.store.now)) g.runner.follow()     // what was handed in is the server's now: look in on it, also with the app closed
        return result
    }

    fun say(result: UploadResult): Work.Finished {
        val next = if (g.store.now.settings.autoUpload) "it goes on by itself when the phone is back on Wi-Fi" else "Upload goes on from where it stopped"
        val done = when (result) {
            is UploadResult.Done -> Work.Finished(if (result.batches == 0) "nothing to upload" else "uploaded: the server takes it from here", true)
            UploadResult.NotPaired -> Work.Finished("pair the app with the server first", false)
            UploadResult.Stopped -> Work.Finished("stopped; $next", false)
            is UploadResult.Unreachable -> Work.Finished("the server could not be reached (${result.message}); $next", false)
            is UploadResult.Broken -> Work.Finished(result.message, false)
            is UploadResult.Refused -> Work.Finished(when (result.status) {
                401 -> "the server does not know this device any more: pair it again"
                507 -> "the server has no room right now: ${result.message}"
                else -> "the server refused: ${result.message}"
            }, false)
        }
        return done
    }

    fun addLogs(picked: List<Picked>) = scope.launch {
        val said = picked.map { p ->
            try { "${g.collector.addLog(p.name, p.modified, p.open).name} added" } catch (e: Exception) { e.message ?: "${p.name}: could not be read" }
        }
        note.value = said.joinToString("\n")
        kick()
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
        lookAtCard().join()                                 // a batch reported done may have freed clips on the card
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
        lookAtQuad()                                        // a saved log taken out takes its erase with it
        true
    } catch (e: RuntimeException) {
        note.value = e.message
        false
    }

    companion object {
        const val WATCH_MS = 3000L
    }

    /** `flash` is how much log the quad's flash holds, when that was read. */
    fun describe(view: QuadView, flash: Long? = null, erasing: Boolean = false): String = when (view) {
        QuadView.None -> "no quad plugged in"
        is QuadView.Locked -> "${view.name}: allow the app to talk to it"
        is QuadView.Ready -> when (flash) {
            null -> "${view.name}: its log will be saved"
            0L -> "${view.name}: its flash is empty, there is no log to save"
            else -> "${view.name}: its log (${Format.bytes(flash)}) will be saved"
        }
        is QuadView.Saved -> if (erasing) "${view.name}: erasing its flash" else "${view.name}: its log is saved, its flash can be erased"
    }

    /** The card's line; `freeing` while clips are deleted from it. */
    fun describe(view: CardView, freeing: Boolean = false): String = when (view) {
        CardView.None -> "no card plugged in"
        is CardView.Locked -> "${view.label}: allow the app to read it"
        is CardView.Unreadable -> "${view.label}: could not be read (${view.why})"
        is CardView.Open -> (if (view.clips == 0) "${view.label}: nothing new" else "${view.label}: ${view.clips} new clip${if (view.clips == 1) "" else "s"}, ${Format.bytes(view.bytes)}") +
            (if (view.collected > 0) ", ${view.collected} collected before" else "") +
            (if (view.others.isNotEmpty()) "\nalso on the card, not collected: ${view.others.take(5).joinToString(", ")}${if (view.others.size > 5) " and ${view.others.size - 5} more" else ""}" else "") +
            (if (freeing) "\nthe app is deleting from it: keep it in" else if (view.clips == 0) "\nthe app has nothing open on it: eject it, then pull it" else "")
    }
}
