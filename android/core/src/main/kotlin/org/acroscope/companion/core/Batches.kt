package org.acroscope.companion.core

import java.io.File
import java.time.LocalDate
import java.time.format.DateTimeParseException

/**
 * What the pilot may change about a batch before it goes up: its event and its date, a file out of it, the whole
 * of it. Once a batch is handed to the server it is the server's: its session is renamed and its clips are moved
 * or deleted there, in the player.
 */
class Batches(private val store: Store) {

    private fun pending(id: String): Batch? {
        val b = store.now.batch(id) ?: return null
        check(b.stage == Stage.PENDING) { "${b.session} is with the server already: change it there" }
        return b
    }

    fun edit(id: String, event: String, date: String) {
        pending(id) ?: return
        val day = try { LocalDate.parse(date) } catch (e: DateTimeParseException) { null }
        require(day != null && day.toString() == date) { "the date is a day like 2026-12-12" }
        store.updateBatch(id) { it.copy(event = event.trim(), date = date) }
    }

    /** A file out of a pending batch and off the phone. What the card has of it stays collected: it is not taken
     *  again at the next collect. */
    fun remove(id: String, name: String) {
        val b = pending(id) ?: return
        if (b.items.none { it.name == name }) return
        File(store.dir(id), name).delete()
        if (b.items.size == 1) return discard(id)
        store.updateBatch(id) { it.copy(items = it.items.filterNot { i -> i.name == name }) }
    }

    fun discard(id: String) {
        pending(id) ?: return
        store.dir(id).deleteRecursively()
        store.update { s -> s.copy(batches = s.batches.filterNot { it.id == id }) }
    }

    /** Keep the newest `keep` finished batches as the record of what went up; everything not finished stays. */
    fun prune(keep: Int = 20) {
        store.update { s ->
            val old = s.batches.filter { it.stage == Stage.DONE }.sortedBy { it.id }.dropLast(keep).map { it.id }.toSet()
            s.copy(batches = s.batches.filterNot { it.id in old })
        }
    }

    /** Forget which files of which card were collected: the next collect takes everything on the card again. */
    fun forgetCollected() {
        store.update { it.copy(seen = emptyList()) }
    }
}
