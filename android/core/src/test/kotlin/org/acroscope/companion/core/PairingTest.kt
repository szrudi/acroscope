package org.acroscope.companion.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class PairingTest {
    private val token = "sII2Bs1adMHfku5e1T3FYIh9W1U7m8OASln7pIJCU08"

    @Test
    fun `the code from the configuration page`() {
        val p = Pairing.parse("acroscope://pair?url=https%3A%2F%2Facroscope.example.org&token=$token")
        assertEquals(Pairing("https://acroscope.example.org", token), p)
        assertEquals(p, Pairing.parse("  acroscope://pair?token=$token&url=https%3A%2F%2Facroscope.example.org%2F \n"))   // pasted with what came along
        assertEquals("https://acroscope.example.org:8443/base", Pairing.parse("acroscope://pair?url=https%3A%2F%2Facroscope.example.org%3A8443%2Fbase%2F&token=$token").url)
    }

    @Test
    fun `a server on this machine or the home network may be plain http`() {
        for (url in listOf("http://127.0.0.1:8070", "http://localhost:8070", "http://10.0.2.2:8070", "http://192.168.1.20:8070", "http://10.10.10.17:8070", "http://172.16.0.9", "http://172.31.255.1"))
            assertEquals(url, Pairing.parse("acroscope://pair?url=${java.net.URLEncoder.encode(url, "UTF-8")}&token=$token").url)
    }

    @Test
    fun `what is not a code says why`() {
        val cases = mapOf(
            "" to "code",
            "hello" to "code",
            "https://acroscope.example.org/config#devices" to "code",
            "acroscope://pair?url=https%3A%2F%2Fx.example.org" to "token",
            "acroscope://pair?token=$token" to "no server address",
            "acroscope://pair?url=ftp%3A%2F%2Fx.example.org&token=$token" to "no server address",
            "acroscope://pair?url=not%20a%20url&token=$token" to "no server address",
            "acroscope://pair?url=https%3A%2Fonly-a-path&token=$token" to "no server address",          // a scheme and no host
            "acroscope://pair?url=http%3A%2F%2Facroscope.example.org&token=$token" to "must be https",   // a token over plain http across the internet
            "acroscope://pair?url=http%3A%2F%2F172.32.0.9&token=$token" to "must be https",              // just past the private 172.16 to 172.31
            "acroscope://pair?url=http%3A%2F%2F172.15.0.9&token=$token" to "must be https",              // just before it
            "acroscope://pair?url=http%3A%2F%2F11.0.0.1&token=$token" to "must be https",
            // a name that starts like a private address is a name, wherever it points
            "acroscope://pair?url=http%3A%2F%2F10.example.com&token=$token" to "must be https",
            "acroscope://pair?url=http%3A%2F%2F192.168.1.1.example.com&token=$token" to "must be https",
            "acroscope://pair?url=http%3A%2F%2F127.0.0.1.example.com&token=$token" to "must be https",
            "acroscope://pair?url=http%3A%2F%2Flocalhost.example.com&token=$token" to "must be https",
            // a key said twice: which one counts is not left to chance (a page could show one and pair with the other)
            "acroscope://pair?url=https%3A%2F%2Fhome.example.org&token=$token&url=https%3A%2F%2Fother.example.org" to "twice",
            "acroscope://pair?url=https%3A%2F%2Fx.example.org&token=$token&token=$token" to "twice",
            "acroscope://pair?url=%&token=$token" to "damaged",                                           // an escape cut off
            "acroscope://pair?url=https%3A%2F%2Fx.example.org%zz&token=$token" to "damaged",
            "acroscope://pair?url=https%3A%2F%2Fx.example.org&token=short" to "token",
            "acroscope://pair?url=https%3A%2F%2Fx.example.org&token=${token}%20x" to "token",
            "acroscope://other?url=https%3A%2F%2Fx.example.org&token=$token" to "code",
        )
        for ((text, why) in cases) {
            val e = assertFailsWith<IllegalArgumentException>(text) { Pairing.parse(text) }
            assertTrue(why in (e.message ?: ""), "$text: ${e.message}")
        }
    }
}
