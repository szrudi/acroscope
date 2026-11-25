package org.acroscope.companion.core

import java.net.URI
import java.net.URLDecoder

/**
 * What pairs the app with a server: the server's address and this device's token, as the configuration page's
 * devices tab hands them out in one code, `acroscope://pair?url=<address>&token=<token>` (opened as a link on the
 * phone, or pasted).
 */
data class Pairing(val url: String, val token: String) {
    companion object {
        private val TOKEN = Regex("[A-Za-z0-9_-]{32,128}")
        private const val OCTET = "(\\.[0-9]{1,3})"
        /** This machine or a home network, by address: a name that starts like one is a name, wherever it points. */
        private val PRIVATE = Regex("localhost|127$OCTET{3}|10$OCTET{3}|192\\.168$OCTET{2}|172\\.(1[6-9]|2[0-9]|3[01])$OCTET{2}")

        /** The pairing a code holds; an IllegalArgumentException says what is wrong with one that holds none. */
        fun parse(code: String): Pairing {
            val text = code.trim()
            require(text.startsWith("acroscope://pair?")) { "this is not a pairing code (it starts with acroscope://pair?)" }
            val pairs = text.substringAfter('?').split('&').filter { '=' in it }.map { it.substringBefore('=') to it.substringAfter('=') }
            // one value per key: with two, what the pilot is shown and what is paired with could differ
            pairs.groupBy { it.first }.filterValues { it.size > 1 }.keys.firstOrNull()?.let { throw IllegalArgumentException("the code names its $it twice") }
            val q = pairs.associate { (k, v) ->
                k to try { URLDecoder.decode(v, "UTF-8") } catch (e: IllegalArgumentException) { throw IllegalArgumentException("the code is damaged (its $k has a % that escapes nothing)") }
            }
            val token = q["token"] ?: ""
            require(TOKEN.matches(token)) { "the code has no valid token" }
            val uri = try { URI(q["url"] ?: "") } catch (e: Exception) { null }
            require(uri != null && uri.scheme in setOf("https", "http") && !uri.host.isNullOrEmpty()) { "the code has no server address" }
            // a token must not cross the internet in the clear; a server at home or on this machine has no certificate
            require(uri.scheme == "https" || PRIVATE.matches(uri.host)) { "the server's address must be https" }
            return Pairing(uri.toString().trimEnd('/'), token)
        }
    }
}
