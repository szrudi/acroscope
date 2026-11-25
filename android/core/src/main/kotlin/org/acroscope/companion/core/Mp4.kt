package org.acroscope.companion.core

/**
 * Whether a QuickTime/MP4 recording has its index. A DVR that loses power while recording leaves the data atom
 * (`mdat`) running to the end of the file and never writes the index (`moov`): nothing plays such a file until it
 * is repaired, and it is better known when the clip is collected than when ingest fails on it.
 */
object Mp4 {
    enum class Index { PRESENT, MISSING, NOT_A_MOVIE }

    private val KNOWN = setOf("ftyp", "mdat", "moov", "wide", "free", "skip", "pnot", "uuid")

    /** Walks the top-level atoms of a file of `size` bytes; `read(at, n)` gives up to n bytes at an offset. Only
     *  the atoms' headers are read, never their contents. */
    fun index(size: Long, read: (at: Long, n: Int) -> ByteArray): Index {
        var at = 0L
        var movie = false
        while (at + 8 <= size) {
            val head = read(at, 16)
            if (head.size < 8) break
            val type = String(head, 4, 4, Charsets.ISO_8859_1)
            if (type !in KNOWN) break
            if (type == "moov") return Index.PRESENT
            movie = true
            val short = u32(head, 0)
            val length = when (short) {
                0L -> size - at                                   // "to the end of the file"
                1L -> if (head.size >= 16) u64(head, 8) else break   // the 64-bit form
                else -> short
            }
            if (length < 8 || length > size - at) break             // past the end of the file: no index after it
            at += length
        }
        return if (movie) Index.MISSING else Index.NOT_A_MOVIE
    }

    private fun u32(b: ByteArray, at: Int): Long = (0..3).fold(0L) { v, i -> (v shl 8) or (b[at + i].toLong() and 0xff) }

    private fun u64(b: ByteArray, at: Int): Long = (0..7).fold(0L) { v, i -> (v shl 8) or (b[at + i].toLong() and 0xff) }
}
