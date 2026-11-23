package org.acroscope.companion.core

import kotlin.test.Test
import kotlin.test.assertEquals

class FormatTest {
    @Test
    fun `sizes as a person reads them`() {
        assertEquals(listOf("0 B", "999 B", "1.0 kB", "1.5 MB", "550 MB", "999 MB", "1.0 GB", "5.3 GB", "128 GB"),
            listOf(0L, 999L, 1_000L, 1_500_000L, 550_000_000L, 999_400_000L, 999_500_000L, 5_300_000_000L, 128_000_000_000L).map(Format::bytes))
    }

    @Test
    fun `a batch in a line`() {
        val items = listOf(Item("VID00001.mov", 500_000_000, Kind.CLIP, ""), Item("VID00002.mov", 600_000_000, Kind.CLIP, "", unfinalised = true), Item("x.bbl", 14_000_000, Kind.LOG, ""))
        assertEquals("2 clips, 1 log, 1.1 GB", Format.contents(items))
        assertEquals("1 clip, 500 MB", Format.contents(items.take(1)))
        assertEquals("1 log, 14 MB", Format.contents(items.takeLast(1)))
        assertEquals("nothing yet", Format.contents(emptyList()))
    }

    @Test
    fun `what the server says about a batch`() {
        assertEquals("handed in, waiting its turn", Format.status(ServerStatus("pending")))
        assertEquals("being ingested", Format.status(ServerStatus("running")))
        assertEquals("done: 2 clips, 1 log in 2026-12-12-whoop-race", Format.status(ServerStatus("done", "2026-12-12-whoop-race", null, listOf("a", "b"), listOf("x.bbl"))))
        assertEquals("done", Format.status(ServerStatus("done")))
        assertEquals("failed: ValueError: VID00003.mov: moov atom not found", Format.status(ServerStatus("failed", error = "ValueError: VID00003.mov: moov atom not found")))
        assertEquals("handed in", Format.status(null))                          // the server has not said yet
        assertEquals("arriving", Format.status(ServerStatus("arriving")))        // a word this version does not know is shown as it is
    }
}
