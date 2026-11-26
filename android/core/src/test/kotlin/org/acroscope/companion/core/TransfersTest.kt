package org.acroscope.companion.core

import kotlin.test.Test
import kotlin.test.assertIs
import kotlin.test.assertNull

class TransfersTest {
    @Test
    fun `a write that fails at once is a line gone, one that fails after its time a controller busy`() {
        assertIs<SerialGone>(Transfers.failedWrite(tookMs = 3, timeoutMs = 2000))
        assertIs<SerialGone>(Transfers.failedWrite(tookMs = 999, timeoutMs = 2000))
        assertIs<SerialBusy>(Transfers.failedWrite(tookMs = 1000, timeoutMs = 2000))
        assertIs<SerialBusy>(Transfers.failedWrite(tookMs = 2004, timeoutMs = 2000))
    }

    @Test
    fun `a read that fails at once is a line gone, one that fails after its time only silence`() {
        assertIs<SerialGone>(Transfers.failedRead(tookMs = 2, timeoutMs = 120))
        assertNull(Transfers.failedRead(tookMs = 60, timeoutMs = 120))
        assertNull(Transfers.failedRead(tookMs = 121, timeoutMs = 120))
    }
}
