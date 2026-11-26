package org.acroscope.companion.core

import java.io.ByteArrayInputStream
import java.io.InputStream
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotEquals

class FingerprintTest {
    private val piece = Fingerprint.PIECE
    private fun bytes(n: Int) = Random(7).nextBytes(n)
    private fun of(b: ByteArray, size: Long = b.size.toLong()) = Fingerprint.of(size) { ByteArrayInputStream(b) }

    /** A stream that skips nothing, as some providers' do: it is read through instead. */
    private class NoSkip(b: ByteArray) : ByteArrayInputStream(b) { override fun skip(n: Long) = 0L }

    @Test
    fun `a change at the start or at the end of a long clip is another fingerprint, one in the middle is not looked at`() {
        val clip = bytes(3 * piece + 17)
        fun changedAt(i: Int) = clip.copyOf().also { it[i] = (it[i] + 1).toByte() }
        assertNotEquals(of(clip), of(changedAt(0)))
        assertNotEquals(of(clip), of(changedAt(piece - 1)))
        assertNotEquals(of(clip), of(changedAt(clip.size - piece)))
        assertNotEquals(of(clip), of(changedAt(clip.size - 1)))
        assertEquals(of(clip), of(changedAt(piece + 5)))                           // the middle is not read
    }

    @Test
    fun `a short clip is read whole`() {
        val clip = bytes(piece + 300)
        for (i in listOf(0, piece, clip.size - 1))
            assertNotEquals(of(clip), of(clip.copyOf().also { it[i] = (it[i] + 1).toByte() }))
        assertEquals(64, of(ByteArray(0)).length)
    }

    @Test
    fun `the size is part of it, and a file shorter than its size is not taken for itself`() {
        val clip = bytes(3 * piece)
        assertNotEquals(of(clip), of(clip.copyOf(clip.size - 1)))
        assertNotEquals(of(clip), of(clip.copyOf(clip.size - 1), size = clip.size.toLong()))
        assertFailsWith<java.io.IOException> { Fingerprint.of(10L * piece) { ByteArrayInputStream(clip) as InputStream } }
    }

    @Test
    fun `a stream that cannot skip gives the same fingerprint`() {
        val clip = bytes(3 * piece + 17)
        assertEquals(of(clip), Fingerprint.of(clip.size.toLong()) { NoSkip(clip) })
    }
}
