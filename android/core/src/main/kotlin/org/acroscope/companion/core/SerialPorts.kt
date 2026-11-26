package org.acroscope.companion.core

/**
 * What tells a flight controller's USB serial port from the other things on USB. A serial port is the
 * communications class's ACM subclass; a network adapter (NCM, ECM: the network chip in a USB-C hub) is the same
 * class with another subclass and the same data interface, so the class alone is not enough.
 */
object SerialPorts {
    const val COMM = 2
    const val CDC_DATA = 10
    const val ACM = 2

    /** ST's and Artery's virtual COM port, Betaflight's on their chips: known by these ids when no interface says ACM. */
    val VCP = setOf(0x0483 to 0x5740, 0x2E3C to 0x5740)

    /** A USB interface as it names itself: its class and subclass. */
    data class Face(val kind: Int, val subclass: Int)

    /** The index of the control interface (null: none) and of the data interface, or null when this is no serial port. */
    fun find(vendor: Int, product: Int, faces: List<Face>): Pair<Int?, Int>? {
        val data = faces.indexOfFirst { it.kind == CDC_DATA }.takeIf { it >= 0 } ?: return null
        val acm = faces.indexOfFirst { it.kind == COMM && it.subclass == ACM }.takeIf { it >= 0 }
        if (acm != null) return acm to data
        if (vendor to product !in VCP) return null
        return faces.indexOfFirst { it.kind == COMM }.takeIf { it >= 0 } to data
    }
}
