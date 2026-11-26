package org.acroscope.companion.core

import java.io.File
import java.io.IOException
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/** Where an upload run is: the file in hand, and the bytes that are up of everything pending. */
data class UploadProgress(val batch: String, val name: String, val done: Long, val total: Long)

sealed interface UploadResult {
    /** Everything pending went up; `batches` were committed in this run. */
    data class Done(val batches: Int) : UploadResult
    data object NotPaired : UploadResult
    data object Stopped : UploadResult
    /** The server could not be reached, or the connection broke: try again later, what went up stays. */
    data class Unreachable(val message: String) : UploadResult
    /** The server said no: 401 not paired any more, 507 no room, 409 the batch id is taken. */
    data class Refused(val status: Int, val message: String) : UploadResult
    /** A batch on the phone is not what was collected (a file gone or changed): nothing of it was sent. */
    data class Broken(val message: String) : UploadResult
}

/**
 * Handing the pending batches to the server and following them there. Batches go up oldest first and a failure
 * stops the run, so they arrive in the order they were made (ingest numbers a day's clips in that order). Nothing
 * about an upload is remembered here: a run sends everything pending again, and the client asks the server where
 * each file ends before it sends a byte. The sidecar goes last and whole, so an event or a date changed after a
 * first try is what the server gets. The phone's copy of a batch is deleted when the server reports the batch
 * done, not when the upload ends.
 *
 * Collecting goes on while a run sends: a file that joins a batch before it is closed goes up with it, one that
 * comes later starts another batch, which the same run sends after it. A batch is closed only once every file it
 * holds is up, and its manifest is what it holds then.
 */
class Uploader(private val store: Store, private val drop: (Pairing) -> Drop) {
    /** One run at a time: the button, the job by itself and a run asked for twice would send the same files. */
    private val running = ReentrantLock()

    private class Broken(message: String) : Exception(message)

    fun run(progress: (UploadProgress) -> Unit = {}, stop: () -> Boolean = { false }): UploadResult = running.withLock {
        val pairing = store.now.settings.pairing ?: return UploadResult.NotPaired
        store.update { s -> s.copy(batches = s.batches.filterNot { it.stage == Stage.PENDING && it.items.isEmpty() }) }
        for (b in store.now.pending) for (i in b.items) broken(b, i)?.let { return UploadResult.Broken(it) }
        val sent = mutableMapOf<String, MutableSet<Item>>()
        var before = 0L
        // the bytes sent so far and the bytes still to send: a file collected meanwhile makes the total grow
        fun total() = before + store.now.pending.sumOf { b -> b.items.filterNot { it in sent[b.id].orEmpty() }.sumOf { it.size } }

        /** One batch handed in: its files until none is left unsent, then closed, its sidecar, the commit. False
         *  when it is gone meanwhile (discarded on the phone). */
        fun handIn(server: Drop, id: String): Boolean {
            val mine = sent.getOrPut(id) { mutableSetOf() }
            while (true) {
                // what is left to send, and the close when nothing is, decided in one change of the state: a file
                // placed by a collect comes before it (and is sent) or after it (and starts another batch)
                var b: Batch? = null
                var left: List<Item> = emptyList()
                store.update { s ->
                    b = s.batch(id)?.takeIf { it.stage == Stage.PENDING }
                    left = b?.items.orEmpty().filterNot { it in mine }
                    if (b == null || left.isNotEmpty()) s else s.copy(batches = s.batches.map { if (it.id == id) it.copy(closed = true) else it })
                }
                val batch = b ?: return false
                if (left.isEmpty()) break
                for (i in left.sortedWith(compareBy({ it.kind != Kind.LOG }, { it.name }))) {      // the logs first: small, and ingest wants them first
                    if (store.now.batch(id)?.items?.contains(i) != true) continue                    // taken out meanwhile
                    broken(batch, i)?.let { throw Broken(it) }
                    val base = before
                    val all = total()
                    server.upload(id, i.name, File(store.dir(id), i.name), { progress(UploadProgress(id, i.name, base + it, all)) }, stop)
                    before += i.size
                    mine += i
                }
            }
            val now = store.now.batch(id) ?: return false
            val sidecar = now.sidecar(store.now.settings.device).toByteArray()
            server.put(id, Names.SIDECAR, sidecar)
            val status = server.commit(id, now.items.associate { it.name to it.size } + (Names.SIDECAR to sidecar.size.toLong()))
            store.updateBatch(id) { it.copy(stage = Stage.COMMITTED, server = status) }
            return true
        }

        var committed = 0
        try {
            val server = drop(pairing)
            val tried = mutableSetOf<String>()
            while (true) {
                val b = store.now.pending.firstOrNull { it.id !in tried } ?: break                  // live: a batch started during the run goes up in it
                tried += b.id
                try {
                    if (handIn(server, b.id)) committed++
                } catch (e: DropException) {
                    // a batch id the server has as handed in: when it is this phone's (a commit whose answer was
                    // lost), the batch is with the server and what is behind it goes on
                    if (e.status != 409) throw e
                    val status = server.batches()[b.id] ?: throw e
                    store.updateBatch(b.id) { it.copy(stage = Stage.COMMITTED, server = status) }
                    committed++
                }
            }
            follow(server)
        } catch (e: Broken) {
            return UploadResult.Broken(e.message!!)
        } catch (e: DropStopped) {
            return UploadResult.Stopped
        } catch (e: DropException) {
            return UploadResult.Refused(e.status, e.message ?: "refused")
        } catch (e: IOException) {
            return UploadResult.Unreachable(e.message ?: e.javaClass.simpleName)
        }
        UploadResult.Done(committed)
    }

    /** Why a file of a batch cannot go up as it is: gone from the phone, or not the size it was collected with. */
    private fun broken(b: Batch, i: Item): String? =
        if (File(store.dir(b.id), i.name).length() != i.size) "${i.name} of ${b.session} is not on the phone as it was collected" else null

    /** Ask the server how the committed batches are doing; a batch it reports done loses its copy on the phone. */
    fun refresh() {
        val pairing = store.now.settings.pairing ?: return
        follow(drop(pairing))
    }

    private fun follow(server: Drop) {
        val waiting = store.now.batches.filter { it.stage == Stage.COMMITTED }
        if (waiting.isEmpty()) return
        val said = server.batches()
        for (b in waiting) {
            val status = said[b.id] ?: continue                                  // not this server's: leave it, files and all
            if (status.status == "done") store.dir(b.id).deleteRecursively()
            store.updateBatch(b.id) { it.copy(stage = if (status.status == "done") Stage.DONE else Stage.COMMITTED, server = status) }
        }
    }

    fun retry(batch: String) {
        val pairing = store.now.settings.pairing ?: return
        val status = drop(pairing).retry(batch)
        store.updateBatch(batch) { it.copy(server = status) }
    }

    /** What the next event may be called: the names used on this phone first, then the server's, each once. When
     *  the server cannot be asked (out in the field), the remembered ones. */
    fun events(): List<String> {
        val mine = store.now.settings.recentEvents
        val pairing = store.now.settings.pairing ?: return mine
        return try { (mine + drop(pairing).events()).distinct() } catch (e: IOException) { mine }
    }

    /** Take a pairing if the server accepts it: returns the name the device is known by there. */
    fun pair(pairing: Pairing): String {
        val device = drop(pairing).device()
        store.update { it.copy(settings = it.settings.copy(url = pairing.url, token = pairing.token, device = device.name)) }
        return device.name
    }
}
