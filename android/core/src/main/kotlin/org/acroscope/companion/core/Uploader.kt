package org.acroscope.companion.core

import java.io.File
import java.io.IOException

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
 */
class Uploader(private val store: Store, private val drop: (Pairing) -> Drop) {

    fun run(progress: (UploadProgress) -> Unit = {}, stop: () -> Boolean = { false }): UploadResult {
        val pairing = store.now.settings.pairing ?: return UploadResult.NotPaired
        store.update { s -> s.copy(batches = s.batches.filterNot { it.stage == Stage.PENDING && it.items.isEmpty() }) }
        val pending = store.now.pending
        for (b in pending) for (i in b.items) {
            val f = File(store.dir(b.id), i.name)
            if (f.length() != i.size) return UploadResult.Broken("${i.name} of ${b.session} is not on the phone as it was collected")
        }
        val total = pending.sumOf { it.bytes }
        var before = 0L
        var committed = 0
        try {
            val server = drop(pairing)
            for (b in pending) {
                for (i in b.items.sortedWith(compareBy({ it.kind != Kind.LOG }, { it.name }))) {      // the logs first: small, and ingest wants them first
                    val base = before
                    server.upload(b.id, i.name, File(store.dir(b.id), i.name), { progress(UploadProgress(b.id, i.name, base + it, total)) }, stop)
                    before += i.size
                }
                val now = store.now.batch(b.id) ?: continue
                val sidecar = now.sidecar(store.now.settings.device).toByteArray()
                server.put(b.id, Names.SIDECAR, sidecar)
                val status = server.commit(b.id, now.items.associate { it.name to it.size } + (Names.SIDECAR to sidecar.size.toLong()))
                store.updateBatch(b.id) { it.copy(stage = Stage.COMMITTED, server = status) }
                committed++
            }
            follow(server)
        } catch (e: DropStopped) {
            return UploadResult.Stopped
        } catch (e: DropException) {
            return UploadResult.Refused(e.status, e.message ?: "refused")
        } catch (e: IOException) {
            return UploadResult.Unreachable(e.message ?: e.javaClass.simpleName)
        }
        return UploadResult.Done(committed)
    }

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
