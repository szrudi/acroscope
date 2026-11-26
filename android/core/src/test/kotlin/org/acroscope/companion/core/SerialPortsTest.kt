package org.acroscope.companion.core

import org.acroscope.companion.core.SerialPorts.Face
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class SerialPortsTest {
    private val acm = Face(SerialPorts.COMM, SerialPorts.ACM)
    private val data = Face(SerialPorts.CDC_DATA, 0)
    private val storage = Face(8, 6)

    @Test
    fun `a Betaflight board's virtual COM port is a serial port`() {
        assertEquals(0 to 1, SerialPorts.find(0x0483, 0x5740, listOf(acm, data)))
        assertEquals(0 to 1, SerialPorts.find(0x2E3C, 0x5740, listOf(acm, data)))
    }

    @Test
    fun `so is any device whose control interface says ACM, whoever made it`() {
        assertEquals(0 to 1, SerialPorts.find(0x1234, 0x5678, listOf(acm, data)))
        assertEquals(1 to 2, SerialPorts.find(0x1234, 0x5678, listOf(storage, acm, data)))   // a composite board, the serial port second
    }

    @Test
    fun `a network adapter speaks the communications class too, and is no serial port`() {
        // ASIX's AX88179A, the network chip in a USB-C hub, in its NCM mode
        assertNull(SerialPorts.find(0x0B95, 0x1790, listOf(Face(SerialPorts.COMM, 0x0D), data)))
        // an ECM adapter
        assertNull(SerialPorts.find(0x1234, 0x5678, listOf(Face(SerialPorts.COMM, 0x06), data)))
    }

    @Test
    fun `a data interface alone is a serial port only on ST's or Artery's virtual COM port ids`() {
        assertEquals(null to 0, SerialPorts.find(0x0483, 0x5740, listOf(data)))
        assertEquals(null to 0, SerialPorts.find(0x2E3C, 0x5740, listOf(data)))
        assertNull(SerialPorts.find(0x0483, 0x1234, listOf(data)))
        assertNull(SerialPorts.find(0x1234, 0x5740, listOf(data)))
        assertNull(SerialPorts.find(0x1234, 0x5678, listOf(data)))
        assertEquals(0 to 1, SerialPorts.find(0x0483, 0x5740, listOf(Face(SerialPorts.COMM, 0), data)))   // its control interface, though it says no ACM
    }

    @Test
    fun `nothing to carry the data is no serial port`() {
        assertNull(SerialPorts.find(0x0483, 0x5740, listOf(acm)))
        assertNull(SerialPorts.find(0x1234, 0x5678, listOf(storage)))
    }
}
