package org.acroscope.companion.core

import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.RandomAccessFile
import java.time.Instant
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import kotlin.random.Random

/** A file on a card: what its directory says of it, and its bytes. */
interface CardFile {
    val name: String
    val size: Long
    /** Last modified, milliseconds since the epoch: with name and size, what tells one recording from another. */
    val modified: Long
    fun open(): InputStream
    /** Take it off the card: only ever a clip the server reported done, on the pilot's yes ([Collector.free]). */
    fun delete(): Unit = throw IOException("the card cannot be changed")
}

/** A DVR's card: the files in its DCIM folder. `label` tells one card from another (its volume's id, which a format
 *  gives anew). */
interface Card {
    val label: String
    fun files(): List<CardFile>
}

/** What a collect would take from a card: the clips not collected yet, in the DVR's recording order (their names
 *  sort that way, and ingest numbers the day's clips in that order), how many were collected before, and what
 *  else lies in the folder. `done`: the clips collected before that went up in a batch the server reported done
 *  (name, size, time and [Fingerprint] as the collect kept them), which the pilot may delete from the card;
 *  [Collector.free] reads each one's fingerprint off the card again before it goes. */
data class Plan(val clips: List<CardFile>, val collected: Int, val others: List<String>, val done: List<CardFile> = emptyList()) {
    val bytes: Long get() = clips.sumOf { it.size }
    val doneBytes: Long get() = done.sumOf { it.size }
}

/** What freeing the card deleted, and each clip it could not, with why. */
data class Freed(val count: Int, val bytes: Long, val failed: List<String>)

/** Where a collect is: the clip in hand, its place among those to copy, and the bytes copied of all of them. */
data class Progress(val name: String, val index: Int, val count: Int, val done: Long, val total: Long)

/** What a collect brought in, and why it ended early when it did. */
data class Outcome(val items: List<Item>, val error: String?)

/**
 * Collecting: clips from the DVR's card and logs saved from a quad, copied into the pending batch of the event the
 * pilot chose. Each file is copied under a `.part` name aside, and only once it is whole is it put in its batch: the
 * batch chosen, the file renamed into its folder and put on its list in one change of the state, so a card pulled in
 * the middle leaves nothing half behind, a batch's folder holds only what its list names, and a batch closed for
 * handing in meanwhile is not joined. A collect never writes to the card; [free] is the one thing that changes it.
 */
class Collector(private val store: Store, private val clock: () -> ZonedDateTime, private val random: Random = Random.Default) {
    companion object {
        /** Room the phone keeps after a collect. */
        const val MARGIN = 256L shl 20
        /** Event names remembered. */
        const val RECENT = 12
        /** A copy on its way in not written to for this long was left by a process that died. */
        const val STALE_MS = 10 * 60_000L
        private val STAMP = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ssxxx")
        private val CONFIGURATOR = Regex("BTFL_BLACKBOX_LOG_(.*_)?[0-9]{8}_[0-9]{6}_.*\\.bbl", RegexOption.IGNORE_CASE)   // the craft left out when it has none
        private val LOG_MAGIC = "H Product:Blackbox".toByteArray()
    }

    fun plan(card: Card, s: State = store.now): Plan {
        val seen = s.seen.toSet()
        val (clips, others) = card.files().partition { Names.isClip(it.name) }
        val kept = kept(s.batches)
        val up = kept(s.batches.filter { it.stage == Stage.DONE })
        fun key(c: CardFile) = Seen(c.name, c.size, c.modified)
        // collected before: seen by name, size and time, unless that name, size and time went up as another recording
        // (a DVR with no clock, a card formatted) and no batch holds this one: it is new, whatever its directory says.
        // Only what went up is read for it, once per card and clip: the rest of a card is not read again.
        fun twin(c: CardFile) = key(c) in up && fingerprint(card, c)?.let { it !in kept[key(c)].orEmpty() } == true
        val (old, new) = clips.partition { c -> key(c) in seen && !twin(c) }
        // done with: a recording that went up in a batch the server reported done, whose name and size no batch still
        // on its way holds, whatever became of it on the server since
        val waiting = s.batches.filter { it.stage != Stage.DONE }.flatMap { b -> b.items.map { it.name to it.size } }.toSet()
        val done = old.filter { c -> (c.name to c.size) !in waiting && up[key(c)]?.let { fingerprint(card, c) in it } == true }
        return Plan(new.sortedBy { it.name }, old.size, others.map { it.name }.sorted(), done.sortedBy { it.name })
    }

    /** What the batches hold, by name, size and time, with each one's fingerprints (items that kept them). */
    private fun kept(batches: List<Batch>): Map<Seen, Set<String>> = batches.flatMap { it.items }
        .mapNotNull { i -> if (i.modified == null || i.fingerprint == null) null else Seen(i.name, i.size, i.modified) to i.fingerprint }
        .groupBy({ it.first }, { it.second }).mapValues { it.value.toSet() }

    /** A clip's fingerprint on this card, read once per card and clip while the app runs; null when it cannot be read. */
    private fun fingerprint(card: Card, clip: CardFile): String? {
        val at = "${card.label}/${clip.name}/${clip.size}/${clip.modified}"
        prints[at]?.let { return it }
        val read = try { Fingerprint.of(clip.size, clip::open) } catch (e: Exception) { return null }
        prints[at] = read
        return read
    }

    private val prints = java.util.concurrent.ConcurrentHashMap<String, String>()

    /**
     * Delete from the card the clips the pilot agreed to that [plan] still finds done with, each only when its
     * fingerprint read off the card now (not the one [plan] remembers) is one that went up. One the card will not
     * let go, or not let be read, does not stop the rest.
     */
    fun free(card: Card, agreed: Collection<Seen>): Freed {
        var count = 0
        var bytes = 0L
        val failed = mutableListOf<String>()
        val s = store.now
        val up = kept(s.batches.filter { it.stage == Stage.DONE })
        for (clip in plan(card, s).done) {
            val key = Seen(clip.name, clip.size, clip.modified)
            if (key !in agreed) continue
            try {
                if (Fingerprint.of(clip.size, clip::open) !in up[key].orEmpty()) {
                    failed += "${clip.name}: not the recording that went up"
                    continue
                }
                clip.delete()
                count++
                bytes += clip.size
            } catch (e: Exception) {
                failed += "${clip.name}: ${e.message ?: e.javaClass.simpleName}"
            }
        }
        return Freed(count, bytes, failed)
    }

    /** Copy what [plan] finds new on the card, given `free` bytes on the phone. `stop` is asked before each clip. */
    fun collect(card: Card, free: Long, progress: (Progress) -> Unit = {}, stop: () -> Boolean = { false }): Outcome {
        val plan = plan(card)
        if (plan.clips.isEmpty()) return Outcome(emptyList(), null)
        if (plan.bytes + MARGIN > free)
            return Outcome(emptyList(), "not enough room on the phone: ${plan.clips.size} clips need ${plan.bytes shr 20} MB, ${free shr 20} MB are free")
        val event = store.now.settings.event
        val items = mutableListOf<Item>()
        var done = 0L
        for ((i, clip) in plan.clips.withIndex()) {
            if (stop()) return Outcome(items, "stopped")
            var part: File? = null
            try {
                part = part()
                val before = done
                val copied = copy(clip.open(), part) { n -> done = before + n; progress(Progress(clip.name, i + 1, plan.clips.size, done, plan.bytes)) }
                if (copied != clip.size) throw IOException("${copied} of ${clip.size} bytes came off the card")
                val item = Item(clip.name, clip.size, Kind.CLIP, stamp(), unfinalised = hasIndex(part, clip.name) == Mp4.Index.MISSING,
                    modified = clip.modified, fingerprint = Fingerprint.of(copied) { part.inputStream() })
                place(part) { s -> batchFor(s, event, clip.name).let { b -> Triple(b, item, Seen(clip.name, clip.size, clip.modified)) } }
                items += item
            } catch (e: IOException) {
                return Outcome(items, "${clip.name}: ${e.message}")
            } finally {
                part?.delete()
            }
        }
        return Outcome(items, null)
    }

    /**
     * A blackbox log saved elsewhere (the Betaflight app's "save flash to file") into the pending batch. It keeps a
     * name in the Configurator's pattern, which the aligner reads the download time from; under any other name it
     * is named from its own header and `modified` (now when unknown). The same dump picked twice is one log.
     */
    fun addLog(name: String, modified: Long?, open: () -> InputStream): Item {
        require(Names.isLog(name)) { "$name: not a blackbox log (.bbl)" }
        val event = store.now.settings.event
        val part = part()
        try {
            copy(open(), part) {}
            val head = part.inputStream().use { it.readNBytes(64 shl 10) }
            require(head.size >= LOG_MAGIC.size && head.copyOf(LOG_MAGIC.size).contentEquals(LOG_MAGIC)) { "$name: not a blackbox log (it does not start like one)" }
            val wanted = if (CONFIGURATOR.matches(name)) name else {
                val at = if (modified != null && modified > 0) Instant.ofEpochMilli(modified).atZone(clock().zone) else clock()
                Names.log(header(head, "Craft name"), header(head, "Board information").substringAfterLast(' ').ifEmpty { "UNKNOWN" }, at.toLocalDateTime())
            }
            val length = part.length()
            return place(part) { s ->
                val batch = batchFor(s, event, null)
                val known = s.batch(batch.id)?.items.orEmpty()
                known.firstOrNull { it.name == wanted && it.size == length }?.let { return@place Triple(batch, it, null) }   // already there: nothing moves
                val final = generateSequence(1) { it + 1 }.map { if (it == 1) wanted else wanted.removeSuffix(".bbl") + "_$it.bbl" }.first { n -> known.none { it.name == n } }
                Triple(batch, Item(final, length, Kind.LOG, stamp()), null)
            }
        } finally {
            part.delete()
        }
    }

    /** A file on its way in, under a name of its own beside the batches' folders (the same disk: it is renamed in).
     *  What a process that died left there goes first: a copy in hand is written to all the time, one left alone
     *  for [STALE_MS] is nobody's. */
    private fun part(): File {
        store.scratch.mkdirs()
        val now = System.currentTimeMillis()
        store.scratch.listFiles { f -> f.name.startsWith("in-") && f.name.endsWith(".part") && now - f.lastModified() > STALE_MS }?.forEach { it.delete() }
        return File.createTempFile("in-", ".part", store.scratch)
    }

    /**
     * `part` into its batch, in one change of the state: `choose` picks the batch, the item and what the card is
     * to remember from the state as it is then; the file is renamed into the batch's folder and the item put on its
     * list. An item the batch holds already is returned as it is, and nothing moves. When the state cannot be
     * written, the file goes again: the clip is still on the card and still new there.
     */
    private fun place(part: File, choose: (State) -> Triple<Batch, Item, Seen?>): Item {
        var placed: Item? = null
        var moved: File? = null
        try {
            store.update { s ->
                val (batch, item, seen) = choose(s)
                placed = item
                if (s.batch(batch.id)?.items?.contains(item) == true) return@update s
                val dir = store.dir(batch.id)
                dir.mkdirs()
                val file = File(dir, item.name)
                if (!part.renameTo(file)) throw IOException("${item.name}: could not be put in place")
                moved = file
                // the event's name is remembered for the next time it is flown, the latest first
                val recent = if (batch.event.isBlank()) s.settings.recentEvents else (listOf(batch.event) + s.settings.recentEvents).distinct().take(RECENT)
                val batches = if (s.batch(batch.id) == null) s.batches + batch else s.batches
                s.copy(batches = batches.map { if (it.id == batch.id) it.copy(items = it.items + item) else it }, seen = if (seen == null) s.seen else s.seen + seen,
                    settings = s.settings.copy(recentEvents = recent))
            }
        } catch (e: Exception) {
            moved?.let { f -> f.delete(); f.parentFile?.let { if (it.list().isNullOrEmpty()) it.delete() } }
            throw e
        }
        return placed!!
    }

    /** The pending batch a file goes to, chosen from `s`: the newest of the chosen event started today when it is
     *  not being handed in and, for a clip, when the clip sorts after every clip in it; else a new one. A clip that
     *  does not sort after them is another recording (the card was formatted in between): it starts a batch of its
     *  own, later than the first, so the day's clips stay in the order they were flown. */
    private fun batchFor(s: State, event: String, clip: String?): Batch {
        val now = clock()
        val today = now.toLocalDate().toString()
        val newest = s.pending.lastOrNull { it.event == event && it.createdAt.take(10) == today }
        if (newest != null && !newest.closed && (clip == null || newest.items.none { it.kind == Kind.CLIP && it.name >= clip })) return newest
        return Batch(Names.batchId(now.toLocalDateTime(), random), event, today, stamp())
    }

    private fun stamp(): String = clock().format(STAMP)

    private fun copy(source: InputStream, to: File, copied: (Long) -> Unit): Long {
        var n = 0L
        source.use { input ->
            to.outputStream().use { out ->
                val buf = ByteArray(1 shl 20)
                while (true) {
                    val got = input.read(buf, 0, buf.size)
                    if (got < 0) break
                    out.write(buf, 0, got)
                    n += got
                    copied(n)
                }
                out.fd.sync()
            }
        }
        return n
    }

    private fun hasIndex(file: File, name: String): Mp4.Index {
        if (!name.lowercase().let { it.endsWith(".mov") || it.endsWith(".mp4") }) return Mp4.Index.NOT_A_MOVIE
        return RandomAccessFile(file, "r").use { f ->
            Mp4.index(f.length()) { at, n -> ByteArray(n).let { b -> f.seek(at); val got = f.read(b); if (got <= 0) ByteArray(0) else b.copyOf(got) } }
        }
    }

    private fun header(head: ByteArray, key: String): String =
        String(head, Charsets.ISO_8859_1).lineSequence().takeWhile { it.startsWith("H ") }
            .firstOrNull { it.startsWith("H $key:") }?.substringAfter(':')?.trim().orEmpty()
}
