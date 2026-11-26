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

    // Each change is decided inside one `store.update`, so a collect placing a file at the same moment comes before
    // it or after it, never between; files go off the phone only after the state no longer names them.

    private fun pending(s: State, id: String): Batch? {
        val b = s.batch(id) ?: return null
        check(b.stage == Stage.PENDING) { "${b.session} is with the server already: change it there" }
        return b
    }

    /** A batch whose content may still change: not with the server, and not being handed in (its commit asked for,
     *  the server may have it as it is). */
    private fun open(s: State, id: String): Batch? {
        val b = pending(s, id) ?: return null
        check(!b.closed) { "${b.session} is being handed in to the server: change it there once it is" }
        return b
    }

    fun edit(id: String, event: String, date: String) {
        // the plain form only: LocalDate also reads a sign and a year past 9999, which a session's name cannot carry
        val day = if (!Regex("[0-9]{4}-[0-9]{2}-[0-9]{2}").matches(date)) null else try { LocalDate.parse(date) } catch (e: DateTimeParseException) { null }
        store.update { s ->
            open(s, id) ?: return@update s
            require(day != null) { "the date is a day like 2026-12-12" }
            s.copy(batches = s.batches.map { if (it.id == id) it.copy(event = event.trim(), date = date) else it })
        }
    }

    /** A file out of a pending batch and off the phone; the last one out takes the batch with it. What the card has
     *  of it stays collected: it is not taken again at the next collect. */
    fun remove(id: String, name: String) {
        var gone: File? = null
        store.update { s ->
            val b = open(s, id) ?: return@update s
            if (b.items.none { it.name == name }) return@update s
            val left = b.items.filterNot { it.name == name }
            gone = if (left.isEmpty()) store.dir(id) else File(store.dir(id), name)
            if (left.isEmpty()) s.copy(batches = s.batches.filterNot { it.id == id })
            else s.copy(batches = s.batches.map { if (it.id == id) it.copy(items = left) else it })
        }
        gone?.deleteRecursively()
    }

    /** A pending batch away whole, also one being handed in: what the server took of it, it keeps. */
    fun discard(id: String) {
        var gone = false
        store.update { s ->
            pending(s, id) ?: return@update s
            gone = true
            s.copy(batches = s.batches.filterNot { it.id == id })
        }
        if (gone) store.dir(id).deleteRecursively()
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
        store.update { it.copy(seen = emptyList(), prints = emptyList()) }
    }
}
