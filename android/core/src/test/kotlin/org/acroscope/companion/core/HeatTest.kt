package org.acroscope.companion.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class HeatTest {
    @Test
    fun `a quad is hot from the limit on, and one that does not say is not`() {
        assertEquals(65, Heat.LIMIT)
        assertTrue(Heat.hot(65))
        assertTrue(Heat.hot(90))
        assertFalse(Heat.hot(64))
        assertFalse(Heat.hot(null))
    }

    @Test
    fun `the core temperature can be asked for at any time`() {
        val fc = FakeFc()
        fc.temps = mutableListOf(47, 0)
        val box = Blackbox(fc, { java.time.ZonedDateTime.now() })
        assertEquals(47, box.temp())
        assertEquals(null, box.temp())                                          // 0: an MCU without the sensor
        assertEquals(listOf(Msp.STATUS.toString(), Msp.STATUS.toString()), fc.asked)
    }
}
