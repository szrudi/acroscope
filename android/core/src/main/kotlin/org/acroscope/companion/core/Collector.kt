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
}

/** A DVR's card: the files in its DCIM folder. */
interface Card {
    val label: String
    fun files(): List<CardFile>
}

/** What a collect would take from a card: the clips not collected yet, in the DVR's recording order (their names
 *  sort that way, and ingest numbers the day's clips in that order), how many were collected before, and what
 *  else lies in the folder. */
data class Plan(val clips: List<CardFile>, val collected: Int, val others: List<String>) {
    val bytes: Long get() = clips.sumOf { it.size }
}

/** Where a collect is: the clip in hand, its place among those to copy, and the bytes copied of all of them. */
data class Progress(val name: String, val index: Int, val count: Int, val done: Long, val total: Long)

/** What a collect brought in, and why it ended early when it did. */
data class Outcome(val items: List<Item>, val error: String?)

/**
 * Collecting: clips from the DVR's card and logs saved from a quad, copied into the pending batch of the event the
 * pilot chose. Each file is copied under a `.part` name and renamed once it is whole, and is only then noted, so a
 * card pulled in the middle leaves nothing half behind. The card is never written to.
 */
class Collector(private val store: Store, private val clock: () -> ZonedDateTime, private val random: Random = Random.Default) {
    companion object {
        /** Room the phone keeps after a collect. */
        const val MARGIN = 256L shl 20
        /** Event names remembered. */
        const val RECENT = 12
        private val STAMP = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ssxxx")
        private val CONFIGURATOR = Regex("BTFL_BLACKBOX_LOG_.*_[0-9]{8}_[0-9]{6}_.*\\.bbl", RegexOption.IGNORE_CASE)
        private val LOG_MAGIC = "H Product:Blackbox".toByteArray()
    }

    fun plan(card: Card): Plan {
        val seen = store.now.seen.toSet()
        val (clips, others) = card.files().partition { Names.isClip(it.name) }
        val (old, new) = clips.partition { Seen(it.name, it.size, it.modified) in seen }
        return Plan(new.sortedBy { it.name }, old.size, others.map { it.name }.sorted())
    }

    /** Copy what [plan] finds new on the card, given `free` bytes on the phone. `stop` is asked before each clip. */
    fun collect(card: Card, free: Long, progress: (Progress) -> Unit = {}, stop: () -> Boolean = { false }): Outcome {
        val plan = plan(card)
        if (plan.clips.isEmpty()) return Outcome(emptyList(), null)
        if (plan.bytes + MARGIN > free)
            return Outcome(emptyList(), "not enough room on the phone: ${plan.clips.size} clips need ${plan.bytes shr 20} MB, ${free shr 20} MB are free")
        val items = mutableListOf<Item>()
        var done = 0L
        for ((i, clip) in plan.clips.withIndex()) {
            if (stop()) return Outcome(items, "stopped")
            val batch = batchFor(clip.name)
            val dir = store.dir(batch.id)
            val part = File(dir, clip.name + ".part")
            try {
                dir.mkdirs()
                val before = done
                val copied = copy(clip.open(), part) { n -> done = before + n; progress(Progress(clip.name, i + 1, plan.clips.size, done, plan.bytes)) }
                if (copied != clip.size) throw IOException("${copied} of ${clip.size} bytes came off the card")
            } catch (e: IOException) {
                part.delete()
                if (dir.list().isNullOrEmpty()) dir.delete()
                return Outcome(items, "${clip.name}: ${e.message}")
            }
            val file = File(dir, clip.name)
            if (!part.renameTo(file)) return Outcome(items, "${clip.name}: could not be put in place")
            val item = Item(clip.name, clip.size, Kind.CLIP, stamp(), unfinalised = hasIndex(file) == Mp4.Index.MISSING)
            note(batch, item, Seen(clip.name, clip.size, clip.modified))
            items += item
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
        val batch = batchFor(null)
        val dir = store.dir(batch.id)
        val part = File(dir, "log.part")
        try {
            dir.mkdirs()
            copy(open(), part) {}
            val head = part.inputStream().use { it.readNBytes(64 shl 10) }
            require(head.size >= LOG_MAGIC.size && head.copyOf(LOG_MAGIC.size).contentEquals(LOG_MAGIC)) { "$name: not a blackbox log (it does not start like one)" }
            val wanted = if (CONFIGURATOR.matches(name)) name else {
                val at = if (modified != null && modified > 0) Instant.ofEpochMilli(modified).atZone(clock().zone) else clock()
                Names.log(header(head, "Craft name"), header(head, "Board information").substringAfterLast(' ').ifEmpty { "UNKNOWN" }, at.toLocalDateTime())
            }
            val known = store.now.batch(batch.id)?.items.orEmpty()
            known.firstOrNull { it.name == wanted && it.size == part.length() }?.let { part.delete(); return it }
            val final = generateSequence(1) { it + 1 }.map { if (it == 1) wanted else wanted.removeSuffix(".bbl") + "_$it.bbl" }.first { n -> known.none { it.name == n } }
            if (!part.renameTo(File(dir, final))) throw IOException("$final: could not be put in place")
            return Item(final, File(dir, final).length(), Kind.LOG, stamp()).also { note(batch, it, null) }
        } finally {
            part.delete()
            if (dir.list().isNullOrEmpty()) dir.delete()
        }
    }

    /** The pending batch a file goes to: the newest one of the chosen event started today, or a new one. A clip
     *  named like one already there is another recording (the card was formatted in between): it starts a batch
     *  of its own, later than the first, so the day's clips stay in the order they were flown. */
    private fun batchFor(clip: String?): Batch {
        val now = clock()
        val event = store.now.settings.event
        return store.now.pending.lastOrNull { b ->
            b.event == event && b.createdAt.take(10) == now.toLocalDate().toString() && b.items.none { it.name == clip }
        } ?: Batch(Names.batchId(now.toLocalDateTime(), random), event, now.toLocalDate().toString(), stamp())
    }

    private fun note(batch: Batch, item: Item, seen: Seen?) {
        store.update { s ->
            val batches = if (s.batch(batch.id) == null) s.batches + batch else s.batches
            // the event's name is remembered for the next time it is flown, the latest first
            val recent = if (batch.event.isBlank()) s.settings.recentEvents else (listOf(batch.event) + s.settings.recentEvents).distinct().take(RECENT)
            s.copy(batches = batches.map { if (it.id == batch.id) it.copy(items = it.items + item) else it }, seen = if (seen == null) s.seen else s.seen + seen,
                settings = s.settings.copy(recentEvents = recent))
        }
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

    private fun hasIndex(file: File): Mp4.Index {
        if (!file.name.lowercase().let { it.endsWith(".mov") || it.endsWith(".mp4") }) return Mp4.Index.NOT_A_MOVIE
        return RandomAccessFile(file, "r").use { f ->
            Mp4.index(f.length()) { at, n -> ByteArray(n).let { b -> f.seek(at); val got = f.read(b); if (got <= 0) ByteArray(0) else b.copyOf(got) } }
        }
    }

    private fun header(head: ByteArray, key: String): String =
        String(head, Charsets.ISO_8859_1).lineSequence().takeWhile { it.startsWith("H ") }
            .firstOrNull { it.startsWith("H $key:") }?.substringAfter(':')?.trim().orEmpty()
}
