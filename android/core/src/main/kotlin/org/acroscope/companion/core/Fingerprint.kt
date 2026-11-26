package org.acroscope.companion.core

import java.io.IOException
import java.io.InputStream
import java.nio.ByteBuffer
import java.security.MessageDigest

/**
 * What tells one recording from another with the same name, size and time (a DVR that splits at a fixed size and
 * has no clock, a card formatted and its numbers again): SHA-256 over the size, the first [PIECE] bytes and the last
 * [PIECE]. Two recordings differ in their first frames, and a finished clip's last bytes are its index with its own
 * times. Reading 2 MiB keeps checking a card's worth of clips to seconds; a file shorter than its size gives
 * another fingerprint.
 */
object Fingerprint {
    const val PIECE = 1 shl 20

    fun of(size: Long, open: () -> InputStream): String = open().use { input ->
        val sha = MessageDigest.getInstance("SHA-256")
        sha.update(ByteBuffer.allocate(8).putLong(size).array())
        val head = input.readNBytes(PIECE)
        sha.update(head)
        skip(input, size - head.size - PIECE)
        sha.update(input.readNBytes(PIECE))
        sha.digest().joinToString("") { "%02x".format(it) }
    }

    private fun skip(input: InputStream, n: Long) {
        var left = n
        while (left > 0) {
            val k = input.skip(left)
            if (k > 0) left -= k
            else if (input.read() < 0) throw IOException("the file ends before its size")
            else left--
        }
    }
}
