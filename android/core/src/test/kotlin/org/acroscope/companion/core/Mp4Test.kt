package org.acroscope.companion.core

import java.nio.ByteBuffer
import kotlin.test.Test
import kotlin.test.assertEquals

class Mp4Test {
    private fun atom(type: String, payload: Int, size: Long? = null): ByteArray {
        val b = ByteBuffer.allocate(8 + payload)
        b.putInt((size ?: (8L + payload)).toInt()).put(type.toByteArray(Charsets.US_ASCII))
        return b.array()
    }

    private fun wide(type: String, payload: Int): ByteArray {                  // the 64-bit form: size 1, then the real size
        val b = ByteBuffer.allocate(16 + payload)
        b.putInt(1).put(type.toByteArray(Charsets.US_ASCII)).putLong(16L + payload)
        return b.array()
    }

    private fun check(vararg parts: ByteArray): Mp4.Index {
        val bytes = parts.fold(ByteArray(0)) { a, p -> a + p }
        return Mp4.index(bytes.size.toLong()) { at, n -> bytes.copyOfRange(at.toInt(), minOf(bytes.size, at.toInt() + n)) }
    }

    @Test
    fun `a finished recording has its index`() {
        assertEquals(Mp4.Index.PRESENT, check(atom("ftyp", 12), atom("wide", 0), atom("mdat", 500), atom("moov", 80)))
        assertEquals(Mp4.Index.PRESENT, check(atom("ftyp", 12), atom("moov", 80), atom("mdat", 500)))          // faststart: the index first
        assertEquals(Mp4.Index.PRESENT, check(atom("ftyp", 12), wide("mdat", 500), atom("moov", 80)))          // a clip past 4 GB
        assertEquals(Mp4.Index.PRESENT, check(atom("ftyp", 12), atom("free", 30), atom("skip", 4), atom("mdat", 10), atom("moov", 8)))
        assertEquals(Mp4.Index.PRESENT, check(atom("ftyp", 12), atom("mdat", 10), atom("moov", 0)))            // an index header as the file's last eight bytes
    }

    @Test
    fun `a recording cut off by a power loss has none`() {
        // the DVR's own shape: the data atom runs to the end of the file, whatever its size field says
        assertEquals(Mp4.Index.MISSING, check(atom("ftyp", 12), atom("wide", 0), atom("mdat", 500, size = 0)))                 // "to the end"
        assertEquals(Mp4.Index.MISSING, check(atom("ftyp", 12), atom("mdat", 500, size = 100_000)))                            // a size past the end
        assertEquals(Mp4.Index.MISSING, check(atom("ftyp", 12), atom("mdat", 500)))                                            // or simply nothing after
        assertEquals(Mp4.Index.MISSING, check(atom("ftyp", 12), atom("mdat", 0, size = 8), ByteArray(300) { 0x55 }))            // a placeholder size, then raw frames
        assertEquals(Mp4.Index.MISSING, check(atom("ftyp", 12), atom("mdat", 4, size = 3)))                                    // a size no atom can have
        assertEquals(Mp4.Index.MISSING, check(atom("ftyp", 12), atom("mdat", 300), ByteArray(5)))                               // a few stray bytes at the end
    }

    @Test
    fun `what is not a movie file is not judged`() {
        assertEquals(Mp4.Index.NOT_A_MOVIE, check("RIFF".toByteArray() + ByteArray(40)))                                       // an .avi
        assertEquals(Mp4.Index.NOT_A_MOVIE, check(ByteArray(64)))
        assertEquals(Mp4.Index.NOT_A_MOVIE, check(ByteArray(3)))
        assertEquals(Mp4.Index.NOT_A_MOVIE, check())
        assertEquals(Mp4.Index.NOT_A_MOVIE, check("H Product:Blackbox flight data recorder".toByteArray()))
    }

    @Test
    fun `only the atom headers are read`() {
        val bytes = atom("ftyp", 12) + atom("mdat", 4_000_000) + atom("moov", 80)
        var read = 0L
        val index = Mp4.index(bytes.size.toLong()) { at, n -> read += n; bytes.copyOfRange(at.toInt(), minOf(bytes.size, at.toInt() + n)) }
        assertEquals(Mp4.Index.PRESENT, index)
        assertEquals(48, read)                                                 // three headers of 16 bytes, not four megabytes
    }
}
