package org.acroscope.companion.core

import java.time.LocalDateTime
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotEquals
import kotlin.test.assertTrue

class NamesTest {
    @Test
    fun `a clip is what ingest takes for one`() {
        for (good in listOf("VID00001.mov", "vid_2.MP4", "VID0003.avi", "Vid.MOV")) assertTrue(Names.isClip(good), good)
        for (bad in listOf("clip.mov", "VID00001.mov.part", "VID00001.mkv", "xVID00001.mov", "VID00001.mov\n", "", ".VID00001.mov")) assertFalse(Names.isClip(bad), bad)
    }

    @Test
    fun `a log is a bbl file that is not hidden`() {
        for (good in listOf("x.bbl", "LOUD.BBL", "BTFL_BLACKBOX_LOG_OTTO_20261010_203503_BETAFPVG473_V2.bbl", "dump #2.bbl")) assertTrue(Names.isLog(good), good)
        for (bad in listOf(".bbl", ".hidden.bbl", "x.bbl.part", "x.txt", "")) assertFalse(Names.isLog(bad), bad)
    }

    @Test
    fun `a name the server takes is plain and one of the three kinds`() {
        for (good in listOf("VID00001.mov", "x.bbl", "import.json", "x".repeat(196) + ".bbl")) assertTrue(Names.accepted(good), good)
        for (bad in listOf("sub/x.bbl", "sub\\x.bbl", "..", ".", "", "notes.txt", "IMPORT.JSON", ".done", "VID\t1.mov", "VID\u007f1.mov", "x".repeat(197) + ".bbl"))
            assertFalse(Names.accepted(bad), bad)
    }

    @Test
    fun `the session is the date and the event as ingest cleans it`() {
        assertEquals("2026-12-12-whoop-race", Names.session("2026-12-12", "whoop race"))
        assertEquals("2026-12-12-Test-Flight", Names.session("2026-12-12", "Test Flight"))
        assertEquals("2026-12-12-a_b-c", Names.session("2026-12-12", "  a_b / c! "))
        assertEquals("2026-12-12-unsorted", Names.session("2026-12-12", ""))
        assertEquals("2026-12-12-unsorted", Names.session("2026-12-12", " ?! "))
        assertEquals("2026-12-12-f-t-r", Names.session("2026-12-12", "fő tér"))       // ingest keeps ASCII letters, digits, _ and -
    }

    @Test
    fun `a batch id is the time and a suffix, as the laptop names one`() {
        val at = LocalDateTime.of(2026, 12, 12, 9, 5, 7)
        val id = Names.batchId(at, Random(1))
        assertTrue(Regex("20261212-090507-[a-z0-9]{6}").matches(id), id)
        assertTrue(Names.isBatchId(id))
        assertNotEquals(id, Names.batchId(at, Random(2)))                      // two in one second do not share an id
        assertTrue(Names.batchId(at, Random(1)) < Names.batchId(at.plusSeconds(1), Random(1)))   // ids sort in the order they were made
        for (bad in listOf("b1", "20261212-090507", "20261212-090507-", "20261212-090507-AB", "20261212-090507-" + "a".repeat(17), "../20261212-090507-ab"))
            assertFalse(Names.isBatchId(bad), bad)
        assertTrue(Names.isBatchId("20261010-203503-564331"))                  // the laptop's
    }

    @Test
    fun `a log is named as the configurator names one`() {
        val at = LocalDateTime.of(2026, 10, 10, 12, 30, 23)
        assertEquals("BTFL_BLACKBOX_LOG_OTTO_20261010_123023_BETAFPVG473_V2.bbl", Names.log("OttO", "BETAFPVG473_V2", at))
        assertEquals("BTFL_BLACKBOX_LOG_AIR65_F_20261010_123023_X.bbl", Names.log("AIR65 F", "X", at))
        assertEquals("BTFL_BLACKBOX_LOG_20261010_123023_X.bbl", Names.log("  ", "X", at))       // no craft name: left out
        // the board's name as the quad says it, cleaned like the craft's: one plain file name, which the server takes
        val odd = Names.log("OttO", "G4 7/3", at)
        assertEquals("BTFL_BLACKBOX_LOG_OTTO_20261010_123023_G4_7_3.bbl", odd)
        assertTrue(Names.accepted(odd), odd)
    }
}
