package org.acroscope.companion.core

import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import kotlin.random.Random

/**
 * The names the server and its ingest go by, said once on this side: what counts as a clip and a log, what a file
 * and a batch may be called at the drop (`ingest/drop.py`: `name_ok`, `batch_ok`), the session a batch is filed
 * under (`ingest.batch_date_session`) and the name of a log (the Configurator's own pattern, which the aligner
 * reads the download time from).
 */
object Names {
    private val CLIP = Regex("vid.*\\.(mov|mp4|avi)", RegexOption.IGNORE_CASE)
    private val BATCH = Regex("[0-9]{8}-[0-9]{6}-[a-z0-9]{1,16}")
    private val STAMP = DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss")
    private val LOG_STAMP = DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss")
    private const val SUFFIX = "abcdefghijklmnopqrstuvwxyz0123456789"
    const val SIDECAR = "import.json"

    fun isClip(name: String): Boolean = CLIP.matches(name)

    fun isLog(name: String): Boolean = name.lowercase().endsWith(".bbl") && !name.startsWith(".")

    /** A file the drop takes: one plain path component of at most 200 characters, and a clip, a log or the sidecar. */
    fun accepted(name: String): Boolean =
        name.length in 1..200 && name != "." && name != ".." && name.none { it == '/' || it == '\\' || it.code < 32 || it.code == 127 } &&
            (name == SIDECAR || isClip(name) || isLog(name))

    /** `<date>-<event>` with the event as ingest cleans it, `<date>-unsorted` when nothing is left of it. */
    fun session(date: String, event: String): String {
        val clean = event.replace(Regex("[^A-Za-z0-9_-]+"), "-").trim('-')
        return if (clean.isEmpty()) "$date-unsorted" else "$date-$clean"
    }

    /** `YYYYmmdd-HHMMSS-<suffix>`: ids sort in the order the batches were made, and two in one second differ. */
    fun batchId(at: LocalDateTime, random: Random): String =
        at.format(STAMP) + "-" + (1..6).map { SUFFIX[random.nextInt(SUFFIX.length)] }.joinToString("")

    fun isBatchId(id: String): Boolean = BATCH.matches(id)

    /** `BTFL_BLACKBOX_LOG_<CRAFT>_<YYYYMMDD_HHMMSS>_<board>.bbl`; the craft upper-cased, left out when empty; both as
     *  the quad says them, so cleaned into one plain file name. */
    fun log(craft: String, board: String, at: LocalDateTime): String {
        val c = craft.trim().uppercase().replace(Regex("[^A-Z0-9_]"), "_")
        val b = board.trim().replace(Regex("[^A-Za-z0-9_-]"), "_")
        return (listOf("BTFL_BLACKBOX_LOG") + (if (c.isEmpty()) emptyList() else listOf(c)) + listOf(at.format(LOG_STAMP), b)).joinToString("_") + ".bbl"
    }
}
