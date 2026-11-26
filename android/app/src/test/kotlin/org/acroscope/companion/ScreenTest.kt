package org.acroscope.companion

import android.content.Intent
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.assertIsNotFocused
import androidx.compose.ui.test.performImeAction
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.v2.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.onAllNodesWithTag
import org.acroscope.companion.core.Format
import org.acroscope.companion.core.Kind
import org.acroscope.companion.core.ServerStatus
import org.acroscope.companion.core.Stage
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.GraphicsMode
import java.io.File
import java.time.LocalDate
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** The screen driven as a pilot drives it, with a card and a server that are stand-ins and the jobs run inline. */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class ScreenTest {
    @get:Rule
    val rule = createComposeRule()
    private val w = World()
    private val asked = mutableListOf<Intent>()
    private var logsPicked = 0
    private var beforeJob = 0
    private var storageOpened = 0
    private var alarms = 0
    private val today = LocalDate.now().toString()

    /** The screen with what the first looks found already on it: a tap sent while it still fills in can miss its mark. */
    private fun show() {
        rule.setContent { Screen(w.graph, openCard = { asked += it }, pickLogs = { logsPicked++ }, beforeJob = { beforeJob++ }, openStorage = { storageOpened++ }, alarm = { alarms++ }) }
        kotlinx.coroutines.runBlocking { kotlinx.coroutines.joinAll(w.session.lookAtCard(), w.session.lookAtQuad(), w.session.askServer()) }
        rule.waitForIdle()
    }

    private fun tag(t: String) = rule.onNodeWithTag(t, useUnmergedTree = true)
    private fun seen(t: String) = rule.onAllNodesWithTag(t, useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty()
    private fun wait(what: () -> Boolean) = rule.waitUntil(5000) { what() }
    /** Scroll the screen's list to one of its parts. */
    private fun bring(t: String) {
        rule.onNodeWithTag("list").performScrollToNode(hasTestTag(t))
    }

    /** Tap a part of the list, brought onto the screen first: a tap on what is scrolled out lands on nothing. */
    private fun click(t: String) {
        bring(t)
        tag(t).performClick()
    }
    private fun pairButton(): String = rule.onNodeWithTag("pair").fetchSemanticsNode().config.toString()

    private fun text(t: String): String {
        if (!seen(t)) bring(t)
        return tag(t).fetchSemanticsNode().config.first { it.key.name == "Text" }.value.toString()
    }

    @Test
    fun `the event typed is where today's flying is filed, and known names are a tap away`() {
        w.paired()
        w.store.update { it.copy(settings = it.settings.copy(recentEvents = listOf("park"))) }
        show()
        assertTrue("$today-unsorted" in text("session"))                        // no name yet: ingest would file it as unsorted
        tag("event").performTextInput("whoop race")
        assertEquals("whoop race", w.store.now.settings.event)
        assertTrue("$today-whoop-race" in text("session"), text("session"))
        wait { seen("event-home") }                                             // the server's sessions, once it answered
        click("event-park")                                        // and the ones used on this phone
        assertEquals("park", w.store.now.settings.event)
        assertFalse(seen("event-park"))                                         // the one chosen is not offered again
    }

    @Test
    fun `done on the keyboard lets go of the event field, and so does starting a job`() {
        w.card(FakeFile("VID00001.mov", movie(100)))
        show()
        tag("event").performClick()
        tag("event").performTextInput("park")
        tag("event").assertIsFocused()
        tag("event").performImeAction()                                         // the keyboard's tick
        tag("event").assertIsNotFocused()                                       // and with the focus goes the keyboard
        tag("event").performClick()
        tag("event").assertIsFocused()
        wait { seen("start") && "1 new clip" in text("card") }
        click("start")
        tag("event").assertIsNotFocused()
    }

    @Test
    fun `a card is allowed once, then says what it holds, and start collects it`() {
        w.locked()
        show()
        wait { seen("allow") }
        assertTrue("allow the app to read it" in text("card"))
        click("allow")
        assertEquals(1, asked.size)                                             // the system's folder picker, opened at the card
        w.cards.files = listOf(FakeFile("VID00001.mov", movie(100)), FakeFile("VID00002.mov", movie(200)), FakeFile("notes.txt", ByteArray(3)))
        w.session.allow(TREE)                                                   // the folder the pilot chose there
        assertEquals(listOf(TREE), w.cards.granted)                             // kept: the card is not asked about again
        wait { seen("start") && "2 new clips" in text("card") }
        assertFalse("eject it" in text("card"), text("card"))                   // there is something to collect first
        assertTrue("notes.txt" in text("card"), text("card"))                   // what would be left on the card is said
        tag("event").performTextInput("park")
        tag("start").assertIsEnabled()
        click("start")
        assertEquals(listOf("collect"), w.runner.ran)
        assertEquals(1, beforeJob)
        wait { seen("collect-status") && "2 clips collected" in text("collect-status") }
        val batch = w.store.now.batches.single()
        assertEquals("park" to listOf("VID00001.mov", "VID00002.mov"), batch.event to batch.items.map { it.name })
        assertTrue(File(w.store.dir(batch.id), "VID00002.mov").length() == movie(200).size.toLong())
        wait { "nothing new" in text("card") && "2 collected before" in text("card") }
        assertTrue("the app has nothing open on it: eject it, then pull it" in text("card"), text("card"))
        click("eject")
        assertEquals(1, storageOpened)                                          // the phone's own storage screen: an app may not eject a card
        tag("start").assertIsNotEnabled()
        bring("batch-${batch.id}")
        assertTrue(seen("remove-VID00001.mov") && seen("remove-VID00002.mov"))
        assertEquals(listOf("park"), w.store.now.settings.recentEvents)
    }

    @Test
    fun `without a card there is nothing to start, and a saved log can still be added`() {
        show()
        assertTrue("no card plugged in" in text("card"))
        assertFalse(seen("eject"))
        tag("start").assertIsNotEnabled()
        click("add-log")
        assertEquals(1, logsPicked)
        val log = "H Product:Blackbox flight data recorder by Nicholas Sherlock\nH Craft name:OttO\n".toByteArray() + ByteArray(50)
        w.session.addLogs(listOf(Picked("BTFL_BLACKBOX_LOG_OTTO_20261212_084500_BETAFPVG473_V2.bbl", null) { log.inputStream() }, Picked("holiday.jpg", null) { ByteArray(4).inputStream() }))
        wait { seen("note") }
        assertTrue("BTFL_BLACKBOX_LOG_OTTO_20261212_084500_BETAFPVG473_V2.bbl added" in text("note") && "holiday.jpg: not a blackbox log" in text("note"), text("note"))
        assertEquals(listOf(Kind.LOG), w.store.now.batches.single().items.map { it.kind })
    }

    @Test
    fun `a clip cut off by a power loss is collected and flagged`() {
        w.card(FakeFile("VID00001.mov", movie(100, finished = false)))
        show()
        wait { seen("start") && "1 new clip," in text("card") }
        click("start")
        wait { seen("collect-status") && "without an index" in text("collect-status") }
        bring("flag-VID00001.mov")
        assertTrue(seen("flag-VID00001.mov"))
    }

    @Test
    fun `a collect that ended early says so as a failure, and the line stays when the screen is left`() {
        w.card(FakeFile("VID00001.mov", movie(100)), object : org.acroscope.companion.core.CardFile {
            override val name = "VID00002.mov"
            override val size = 500L
            override val modified = 1000L
            override fun open(): java.io.InputStream = throw java.io.IOException("card removed")
        })
        show()
        wait { "2 new clips" in text("card") }
        click("start")
        wait { seen("collect-status") && "card removed" in text("collect-status") }
        assertEquals(false, (w.session.collect.value as Work.Finished).ok)
        w.session.left()
        rule.waitForIdle()
        assertTrue(seen("collect-status"))                                      // what went wrong stays until the next try
    }

    @Test
    fun `a card that cannot be read says so`() {
        w.cards.now = CardState.Ready("COBRA", TREE)
        w.cards.files = listOf(object : org.acroscope.companion.core.CardFile {
            override val name get() = throw IllegalStateException("the card does not answer")
            override val size = 0L
            override val modified = 0L
            override fun open() = throw UnsupportedOperationException()
        })
        show()
        wait { "could not be read" in text("card") }
        assertTrue("the card does not answer" in text("card"))
        tag("start").assertIsNotEnabled()
    }

    // ---- the quad on USB

    private val flightLog = "H Product:Blackbox flight data recorder by Nicholas Sherlock\nH Craft name:OttO\n".toByteArray() + ByteArray(9000) { 3 }

    @Test
    fun `a quad on usb has its log saved by start, and the erase is offered after`() {
        w.clock = java.time.ZonedDateTime.parse("2026-12-12T08:45:00+01:00")
        val fc = org.acroscope.companion.core.FakeFc(flightLog)
        w.quads.fc = fc
        w.quads.now = QuadState.Ready("OTTO")
        show()
        wait { "its log will be saved" in text("quad") }
        assertFalse(seen("erase"))                                              // nothing saved yet: nothing to erase
        tag("event").performTextInput("whoop race")
        click("start")                                                          // no card: the quad alone is something to collect
        wait { "log saved" in text("collect-status") }
        val item = w.store.now.batches.single().items.single()
        assertEquals("BTFL_BLACKBOX_LOG_OTTO_20261212_084500_BETAFPVG473_V2.bbl" to Kind.LOG, item.name to item.kind)
        assertEquals(flightLog.toList(), File(w.store.dir(w.store.now.batches.single().id), item.name).readBytes().toList())
        assertTrue(w.store.scratch.list().isNullOrEmpty())                       // the copy it came through is gone
        assertTrue("its log is saved, its flash can be erased" in text("quad"), text("quad"))
        assertFalse(fc.closed)                                                  // the line stays open: only it may erase
        assertFalse(org.acroscope.companion.core.Msp.DATAFLASH_ERASE.toString() in fc.asked)   // and saving erased nothing
        click("start")                                                          // again: the log is not taken a second time
        assertEquals(1, w.quads.opened)
        click("erase")
        wait { seen("erase-status") && "flash is erased" in text("erase-status") }
        assertEquals(0, fc.flash.size)
        assertTrue(fc.closed)
        wait { !seen("erase") && "OTTO: its flash is empty, there is no log to save" in text("quad") }
        tag("start").assertIsNotEnabled()                                       // and nothing is left for Start
        w.session.lookAtQuad()                                                  // the same quad, looked at again
        rule.waitForIdle()
        assertTrue("its flash is empty" in text("quad"))
        assertTrue("flash is erased" in text("erase-status"))                   // still plugged in: the line stands
        assertTrue("log saved" in text("collect-status"))
        w.quads.now = QuadState.None
        w.session.lookAtQuad()
        wait { "no quad plugged in" in text("quad") && !seen("erase-status") && !seen("collect-status") }   // unplugged: what was said about it goes with it
    }

    /** A quad whose log was saved through the screen: its erase is on offer. */
    private fun saved(): org.acroscope.companion.core.FakeFc {
        val fc = org.acroscope.companion.core.FakeFc(flightLog)
        w.quads.fc = fc
        w.quads.now = QuadState.Ready("OTTO", "/dev/bus/usb/001/002")
        show()
        wait { "its log will be saved" in text("quad") }
        click("start")
        wait { seen("erase") }
        return fc
    }

    private val erased get() = org.acroscope.companion.core.Msp.DATAFLASH_ERASE.toString()

    @Test
    fun `a quad that restarts under the erase and comes back empty is erased`() {
        val fc = saved()
        fc.goneAfterErase = true                                                // it drops off USB as the chip erases
        click("erase")
        wait { seen("erase-warning") && "restarted" in text("erase-warning") }  // the app waits for it to come back
        w.quads.fc = org.acroscope.companion.core.FakeFc(ByteArray(0))           // back, on a new line, its flash empty
        wait { seen("erase-status") && "flash is erased" in text("erase-status") }
        assertTrue("restarted" in text("erase-status"), text("erase-status"))
    }

    @Test
    fun `a quad that restarts under the erase and comes back with its log is not erased`() {
        w.session.returnMs = 1500
        val fc = saved()
        fc.goneAfterErase = true
        click("erase")
        wait { seen("erase-warning") && "restarted" in text("erase-warning") }
        w.quads.fc = org.acroscope.companion.core.FakeFc(flightLog)              // back, the log still on it
        rule.waitUntil(8000) { seen("erase-status") && "not erased" in text("erase-status") }
    }

    @Test
    fun `while the flash is erased the screen says to keep the quad plugged in, and the quad's line says it is erasing`() {
        val fc = saved()
        fc.eraseNeverEnds = true
        click("erase")
        wait { seen("erase-warning") }
        assertTrue("keep it plugged in" in text("erase-warning"), text("erase-warning"))
        assertTrue("OTTO: erasing its flash" in text("quad"), text("quad"))
        assertFalse("can be erased" in text("quad"))
        fc.eraseNeverEnds = false
        wait { seen("erase-status") && "flash is erased" in text("erase-status") }
        assertFalse(seen("erase-warning"))                                      // done: the warning goes
    }

    @Test
    fun `the erase is withdrawn when the saved log is taken off the phone`() {
        val fc = saved()
        val batch = w.store.now.batches.single()
        w.session.remove(batch.id, batch.items.single().name)                   // the batch's only file: the batch goes with it
        wait { !seen("erase") && "its log" in text("quad") && "will be saved" in text("quad") }
        assertTrue(fc.closed)                                                   // the line that could erase is let go
        assertFalse(erased in fc.asked)
    }

    @Test
    fun `an erase asked for after the saved log went another way erases nothing`() {
        val fc = saved()
        w.store.update { it.copy(batches = emptyList()) }                        // gone without the screen hearing of it
        click("erase")
        wait { !seen("erase") && "will be saved" in text("quad") }
        assertFalse(erased in fc.asked)
        assertEquals(flightLog.size, fc.flash.size)
    }

    @Test
    fun `a quad swapped while the app was away is another quad, though it has the same name`() {
        val first = saved()
        val second = org.acroscope.companion.core.FakeFc(flightLog + ByteArray(100) { 5 })
        w.quads.fc = second
        w.quads.now = QuadState.Ready("OTTO", "/dev/bus/usb/001/003")           // unplugged and another plugged in, with the screen off
        w.session.lookAtQuad()
        wait { !seen("erase") && "will be saved" in text("quad") }
        assertTrue(first.closed)
        tag("start").assertIsEnabled()                                          // the new one's log is there to save
        click("start")
        wait { seen("erase") }
        assertEquals(2, w.store.now.batches.single().items.size)
        assertFalse(erased in first.asked || erased in second.asked)
    }

    @Test
    fun `a card collected while a saved log waits for its erase does not save the log again`() {
        saved()
        w.card(FakeFile("VID00001.mov", movie(100)))
        w.session.lookAtCard()
        wait { "1 new clip" in text("card") }
        click("start")
        wait { "1 clip collected" in text("collect-status") }
        assertEquals(1, w.quads.opened)
        assertEquals(listOf(Kind.LOG, Kind.CLIP), w.store.now.batches.single().items.map { it.kind })
        assertTrue(seen("erase"))
    }

    private fun quad(vararg temps: Int): org.acroscope.companion.core.FakeFc {
        val fc = org.acroscope.companion.core.FakeFc(flightLog)
        fc.temps = temps.toMutableList()
        w.quads.fc = fc
        w.quads.now = QuadState.Ready("OTTO")
        return fc
    }

    @Test
    fun `a quad on usb says how warm it is, and a hot one is an alarm until it has cooled`() {
        val fc = quad(41)
        show()
        wait { "its log will be saved" in text("quad") }
        assertFalse(seen("heat"))                                               // nothing read yet
        w.session.feel()
        wait { seen("heat") && "41 °C" in text("heat") }
        fc.temps = mutableListOf(64)
        w.session.feel()
        wait { "64 °C" in text("heat") }
        assertFalse("unplug" in text("heat"))
        rule.mainClock.advanceTimeBy(3 * ALARM_EVERY_MS)
        assertEquals(0, alarms)                                                 // warm is not hot
        fc.temps = mutableListOf(65)
        w.session.feel()
        wait { "65 °C: unplug it and let it cool" in text("heat") }
        wait { alarms >= 1 }
        val first = alarms
        rule.mainClock.advanceTimeBy(ALARM_EVERY_MS + 100)
        wait { alarms > first }                                                 // again and again while it stays hot
        fc.temps = mutableListOf(50)
        w.session.feel()
        wait { "50 °C" in text("heat") && "unplug" !in text("heat") }
        val rung = alarms
        rule.mainClock.advanceTimeBy(3 * ALARM_EVERY_MS)
        assertEquals(rung, alarms)                                              // cooled: quiet
        w.quads.now = QuadState.None
        w.session.feel()                                                        // unplugged, noticed by the next reading
        wait { !seen("heat") }
        assertTrue(fc.closed)
    }

    @Test
    fun `a save takes the quad's line from the temperature, and an unplugged quad has none`() {
        val fc = quad(41)
        show()
        w.session.feel()
        wait { seen("heat") }
        click("start")
        wait { seen("erase") }
        assertTrue(fc.closed)                                                   // the reading's line was let go before the save opened its own
        assertEquals(2, w.quads.opened)
        w.quads.now = QuadState.None
        w.session.lookAtQuad()                                                  // unplugged, told by the phone
        wait { "no quad plugged in" in text("quad") && !seen("heat") }
    }

    @Test
    fun `the temperature has a line to the quad of its own, kept between readings and let go with the screen`() {
        val fc = quad(41)
        show()
        w.session.feel()
        w.session.feel()
        wait { seen("heat") }
        assertEquals(1, w.quads.opened)                                         // one line, not one a reading
        assertFalse(fc.closed)
        w.quads.now = QuadState.None
        w.session.lookAtQuad()                                                  // unplugged, told by the phone: its line is closed with it
        wait { !seen("heat") }
        assertTrue(fc.closed)
        fc.closed = false
        w.quads.now = QuadState.Ready("OTTO")                                   // plugged in again
        w.session.feel()
        wait { seen("heat") }
        assertEquals(2, w.quads.opened)
        assertFalse(fc.closed)
        kotlinx.coroutines.runBlocking { w.session.watch(false).join() }        // the screen is left: another app may have the quad
        assertTrue(fc.closed)
        kotlinx.coroutines.runBlocking { w.session.watch(true) }                // back: read again, by itself
        wait { w.quads.opened == 3 }
        kotlinx.coroutines.runBlocking { w.session.watch(false).join() }
        w.quads.fc = null                                                       // held by another app, or gone under the question
        w.session.feel()
        wait { !seen("heat") }
    }

    @Test
    fun `a save says the temperature itself, and the erase's line outlives the screen`() {
        val fc = quad(44)
        show()
        wait { "its log will be saved" in text("quad") }
        click("start")
        wait { seen("erase") }
        wait { "44 °C" in text("heat") }                                        // read by the save, on its own line
        assertEquals(1, w.quads.opened)
        fc.temps = mutableListOf(46)
        w.session.feel()
        wait { "46 °C" in text("heat") }
        assertEquals(1, w.quads.opened)                                         // asked on the line the saved log keeps open
        kotlinx.coroutines.runBlocking { w.session.watch(false).join() }
        assertFalse(fc.closed)                                                  // leaving the screen does not take the erase away
        click("erase")
        wait { seen("erase-status") && "flash is erased" in text("erase-status") }
    }

    @Test
    fun `the quad's line says what is on its flash, and an empty one is nothing to start`() {
        val fc = quad(41)
        show()
        wait { "OTTO: its log will be saved" in text("quad") }                  // not asked yet
        w.session.feel()
        wait { "OTTO: its log (${Format.bytes(flightLog.size.toLong())}) will be saved" in text("quad") }
        tag("start").assertIsEnabled()
        fc.flash = ByteArray(0)
        w.session.feel()
        wait { "OTTO: its flash is empty, there is no log to save" in text("quad") }
        tag("start").assertIsNotEnabled()
        w.session.startCollect()                                                // asked for all the same: nothing runs
        assertEquals(emptyList(), w.runner.ran)
        w.card(FakeFile("VID00001.mov", movie(100)))                            // a card beside it is still something to collect
        w.session.lookAtCard()
        wait { "1 new clip" in text("card") }
        tag("start").assertIsEnabled()
        tag("event").performTextInput("park")
        click("start")
        wait { seen("collect-status") && "1 clip collected" in text("collect-status") }
        assertEquals(listOf(Kind.CLIP), w.store.now.batches.single().items.map { it.kind })
        w.quads.now = QuadState.None
        w.session.lookAtQuad()
        wait { "no quad plugged in" in text("quad") }
        w.quads.now = QuadState.Ready("OTTO")                                   // plugged in again, or another quad
        w.session.lookAtQuad()
        wait { "OTTO: its log will be saved" in text("quad") }                  // what the last one said is forgotten
        tag("start").assertIsEnabled()
    }

    @Test
    fun `what was read of the flash is forgotten when the quad stops answering`() {
        val fc = quad(41)
        fc.flash = ByteArray(0)
        show()
        w.session.feel()
        wait { "its flash is empty" in text("quad") }
        w.quads.now = QuadState.None                                            // pulled, and the phone has not said so yet
        w.session.feel()
        wait { "OTTO: its log will be saved" in text("quad") }
        w.quads.now = QuadState.Ready("OTTO")
        w.session.feel()
        wait { "its flash is empty" in text("quad") }
        kotlinx.coroutines.runBlocking { w.session.watch(false).join() }
        w.quads.fc = null                                                       // held by another app
        w.session.feel()
        wait { "OTTO: its log will be saved" in text("quad") }
        tag("start").assertIsEnabled()                                          // Start is where the pilot hears why
    }

    @Test
    fun `a board without a flash is not called empty`() {
        val fc = quad(41)
        fc.flash = ByteArray(0)
        fc.present = false
        show()
        w.session.feel()
        wait { seen("heat") }
        assertTrue("OTTO: its log will be saved" in text("quad"), text("quad"))
        click("start")
        wait { seen("collect-status") && "this flight controller has no flash for logs" in text("collect-status") }
    }

    @Test
    fun `an erase that fails leaves the flash unknown, not empty`() {
        val fc = quad(41)
        show()
        wait { "its log will be saved" in text("quad") }
        click("start")
        wait { seen("erase") }
        fc.spoil = { command, reply -> if (command == org.acroscope.companion.core.Msp.DATAFLASH_SUMMARY) org.acroscope.companion.core.Msp.frame(command, ByteArray(3), '>') else reply }
        click("erase")
        wait { seen("erase-status") && "the flash was not erased" in text("erase-status") }
        wait { "OTTO: its log will be saved" in text("quad") }
        tag("start").assertIsEnabled()
    }

    @Test
    fun `a quad plugged in while the screen is there is asked at once`() {
        show()
        w.session.watch(true)
        Thread.sleep(300)                                                       // the first reading has found no quad; the next is WATCH_MS away
        quad(41)
        w.session.lookAtQuad()                                                  // plugged in, told by the phone
        rule.waitUntil(1500) { seen("heat") && "its log (" in text("quad") }
        kotlinx.coroutines.runBlocking { w.session.watch(false).join() }
    }

    @Test
    fun `a new collect makes the last erase old news`() {
        val fc = org.acroscope.companion.core.FakeFc(flightLog)
        w.quads.fc = fc
        w.quads.now = QuadState.Ready("OTTO")
        show()
        wait { "its log will be saved" in text("quad") }
        click("start")
        wait { seen("erase") }
        click("erase")
        wait { seen("erase-status") && "flash is erased" in text("erase-status") }
        w.quads.fc = org.acroscope.companion.core.FakeFc(flightLog)             // flown again, plugged in again, unseen by the app in between
        w.session.feel()                                                        // back on the screen, the quad is asked again
        wait { "its log (" in text("quad") }
        click("start")
        wait { seen("erase") }                                                  // a log is saved and the flash is not erased: nothing says it is
        assertFalse(seen("erase-status"))
    }

    @Test
    fun `a finished line goes when what it spoke of has moved on`() {
        w.card(FakeFile("VID00001.mov", movie(100)))
        w.paired()
        show()
        tag("event").performTextInput("park")
        wait { seen("start") && "1 new clip" in text("card") }
        click("start")
        wait { seen("collect-status") && "1 clip collected" in text("collect-status") }
        click("upload")
        assertTrue("uploaded" in text("upload-status"), text("upload-status"))
        assertTrue("1 clip collected" in text("collect-status"))                // nothing moved: both lines stand
        w.card(FakeFile("VID00001.mov", movie(100)), FakeFile("VID00002.mov", movie(200)))
        w.session.lookAtCard()                                                  // the same card, looked at again
        wait { "1 new clip" in text("card") }
        assertTrue(seen("collect-status"))
        click("start")                                                          // a new collect: the last upload is old news
        bring("upload")
        assertFalse(seen("upload-status"))                                      // not "uploaded" under a batch that has not gone up
        assertTrue("1 clip collected" in text("collect-status"))
        w.cards.now = CardState.None
        w.session.lookAtCard()                                                  // the card is pulled: the line about it goes
        wait { "no card plugged in" in text("card") && !seen("collect-status") }
    }

    @Test
    fun `leaving the screen takes the lines the pilot has seen, and keeps what went wrong`() {
        collected()
        show()
        wait { seen("collect-status") && "2 clips collected" in text("collect-status") }
        w.session.upload.value = Work.Finished("uploaded: the server takes it from here", true)
        wait { seen("upload-status") || run { bring("upload"); seen("upload-status") } }
        w.session.left()
        wait { !seen("collect-status") && !seen("upload-status") }
        click("upload")                                                         // not paired: this one fails
        assertTrue("pair the app with the server first" in text("upload-status"))
        w.session.left()
        rule.waitForIdle()
        assertTrue("pair the app with the server first" in text("upload-status"))   // until the next try
    }

    @Test
    fun `a quad the app may not talk to yet is asked about`() {
        var asked = 0
        w.quads.now = QuadState.NeedsPermission("OTTO") { asked++ }
        show()
        wait { seen("allow-quad") }
        assertTrue("allow the app to talk to it" in text("quad"))
        tag("start").assertIsNotEnabled()
        click("allow-quad")
        assertEquals(1, asked)
    }

    @Test
    fun `a log that does not come off whole leaves the flash alone, and the card is still collected`() {
        val fc = org.acroscope.companion.core.FakeFc(flightLog)
        fc.spoil = { command, reply -> if (command == org.acroscope.companion.core.Msp.DATAFLASH_READ) org.acroscope.companion.core.Msp.frame(command, ByteArray(3), '>') else reply }
        w.quads.fc = fc
        w.quads.now = QuadState.Ready("OTTO")
        w.card(FakeFile("VID00001.mov", movie(100)))
        show()
        wait { "1 new clip" in text("card") && "its log will be saved" in text("quad") }
        click("start")
        wait { seen("collect-status") && "1 clip collected" in text("collect-status") }
        assertTrue("the log was not saved (a bad flash read at 0); the quad's flash is as it was" in text("collect-status"), text("collect-status"))
        assertEquals(listOf(Kind.CLIP), w.store.now.batches.single().items.map { it.kind })
        assertTrue(fc.closed)
        assertFalse(seen("erase"))                                              // nothing verified: no erase on offer
        assertEquals(flightLog.size, fc.flash.size)
        assertTrue(w.store.scratch.list().isNullOrEmpty())
        w.session.left()
        rule.waitForIdle()
        assertTrue("the log was not saved" in text("collect-status"))           // what went wrong is still there after a look elsewhere
        w.quads.now = QuadState.None
        w.session.lookAtQuad()
        wait { !seen("collect-status") }                                        // and goes with the quad it was about
    }

    @Test
    fun `an empty flash and an unplugged quad are said plainly`() {
        val fc = org.acroscope.companion.core.FakeFc(ByteArray(0))
        w.quads.fc = fc
        w.quads.now = QuadState.Ready("OTTO")
        show()
        wait { "its log will be saved" in text("quad") }
        click("start")
        wait { "OTTO's flash is empty" in text("collect-status") }
        assertTrue(fc.closed)
        assertEquals(emptyList(), w.store.now.batches)
        wait { "OTTO: its flash is empty, there is no log to save" in text("quad") }   // told by the save that found nothing
        tag("start").assertIsNotEnabled()
        w.quads.now = QuadState.None
        w.session.lookAtQuad()
        wait { "no quad plugged in" in text("quad") }
        tag("start").assertIsNotEnabled()
    }

    private fun collected(): String {
        w.card(FakeFile("VID00001.mov", movie(100)), FakeFile("VID00002.mov", movie(200)))
        w.store.update { it.copy(settings = it.settings.copy(event = "park")) }
        w.session.collectNow(TREE) { false }
        return w.store.now.batches.single().id
    }

    @Test
    fun `before it goes up a batch can lose a file, change its event and date, or be discarded`() {
        val id = collected()
        show()
        bring("batch-$id")
        click("remove-VID00001.mov")
        assertEquals(listOf("VID00002.mov"), w.store.now.batch(id)!!.items.map { it.name })
        assertFalse(File(w.store.dir(id), "VID00001.mov").exists())
        click("edit-$id")
        tag("edit-date").performTextClearance()
        tag("edit-date").performTextInput("12/11/2026")
        click("edit-save")
        assertEquals(today, w.store.now.batch(id)!!.date)                       // not a day: nothing changed, and the fields stay to be put right
        assertTrue(seen("edit-date"))
        assertTrue("the date is a day like" in w.session.note.value!!)
        tag("edit-date").performTextClearance()
        tag("edit-date").performTextInput("2026-12-11")
        tag("edit-event").performTextClearance()
        tag("edit-event").performTextInput("whoop race")
        assertTrue("2026-12-11-whoop-race" in text("edit-session"))
        click("edit-save")
        assertEquals("whoop race" to "2026-12-11", w.store.now.batch(id)!!.let { it.event to it.date })
        assertFalse(seen("edit-date"))
        click("edit-$id")
        click("edit-discard")
        assertNull(w.store.now.batch(id))
        assertFalse(seen("batch-$id"))
        assertFalse(seen("upload"))                                             // nothing waits: nothing to upload
    }

    @Test
    fun `upload hands the batches over, and their copies go when the server is done`() {
        val id = collected()
        w.paired()
        show()
        bring("upload")
        click("upload")
        assertEquals(listOf("upload", "follow"), w.runner.ran)                  // handed in by a press: looked in on afterwards all the same
        assertEquals(listOf("upload $id VID00001.mov", "upload $id VID00002.mov", "put $id import.json", "commit $id"), w.drop.calls.filter { !it.startsWith("batches") && it != "events" })
        wait { seen("sent-$id") }
        assertTrue("uploaded" in text("upload-status"), text("upload-status"))
        assertEquals(Stage.COMMITTED, w.store.now.batch(id)!!.stage)
        assertTrue("waiting its turn" in text("status-$id"))
        assertTrue("kept on the phone until the server is done" in text("kept-$id"), text("kept-$id"))
        assertTrue(File(w.store.dir(id), "VID00001.mov").exists())
        w.drop.statuses[id] = ServerStatus("failed", error = "ValueError: VID00002.mov: no video stream")
        bring("check")
        click("check")
        wait { seen("retry-$id") }
        assertTrue("no video stream" in text("status-$id"), text("status-$id"))
        click("retry-$id")
        wait { "retry $id" in w.drop.calls && w.store.now.batch(id)!!.server?.status == "pending" }   // asked, and the answer is in the store
        w.drop.statuses[id] = ServerStatus("done", "$today-park", null, listOf("${today}_001", "${today}_002"), emptyList())
        click("check")
        wait { w.store.now.batch(id)!!.stage == Stage.DONE }
        assertFalse(w.store.dir(id).exists())                                   // the server has it: the phone's copy is gone
        wait { "done: 2 clips" in text("status-$id") }
        assertFalse(seen("kept-$id"))                                           // its copy is gone: nothing is kept
        assertFalse("park" in text("status-$id"), text("status-$id"))           // the session is the line above it
        assertFalse(seen("retry-$id"))
    }

    @Test
    fun `uploading by itself is a switch, there once the app is paired`() {
        val id = collected()
        show()
        assertFalse(seen("auto"))                                               // not paired: nothing to switch on
        w.paired()
        wait { seen("auto") }
        assertEquals(emptyList(), w.runner.ran)
        click("auto")
        assertTrue(w.store.now.settings.autoUpload)
        assertEquals(listOf("auto"), w.runner.ran)                              // a batch waits: an unattended run is asked for at once
        click("auto")
        assertFalse(w.store.now.settings.autoUpload)
        assertEquals(listOf("auto"), w.runner.ran)                              // off: nothing more is asked for
        w.runner.ran.clear()
        w.store.update { it.copy(settings = it.settings.copy(autoUpload = true)) }
        w.card(FakeFile("VID00003.mov", movie(50)))
        w.session.collectNow(TREE) { false }                                    // switched on, and something new was collected
        assertEquals(listOf("auto"), w.runner.ran)
        w.runner.ran.clear()
        w.session.uploadNow { false }                                           // handed in: now it is the server's turn, to be looked in on
        assertEquals(listOf("follow"), w.runner.ran)
        assertEquals(Stage.COMMITTED, w.store.now.batch(id)!!.stage)
    }

    @Test
    fun `an upload that cannot go says why in the pilot's words`() {
        val id = collected()
        show()
        bring("upload")
        click("upload")
        assertTrue("pair the app with the server first" in text("upload-status"))
        w.paired()
        for ((failure, said) in listOf(java.io.IOException("no route to host") to "the server could not be reached (no route to host); Upload goes on from where it stopped",
            org.acroscope.companion.core.DropException(401, "this door takes a paired device's token") to "pair it again",
            org.acroscope.companion.core.DropException(507, "the drop is full: 1 of 2 bytes") to "no room right now: the drop is full")) {
            w.drop.fail = failure
            click("upload")
            assertTrue(said in text("upload-status"), text("upload-status"))
            assertEquals(Stage.PENDING, w.store.now.batch(id)!!.stage)
        }
        w.store.update { it.copy(settings = it.settings.copy(autoUpload = true)) }
        w.drop.fail = java.io.IOException("Software caused connection abort")
        click("upload")                                                         // with uploading by itself on, the next step is the app's
        assertTrue("it goes on by itself when the phone is back on Wi-Fi" in text("upload-status"), text("upload-status"))
        w.drop.fail = null
        click("upload")
        wait { seen("sent-$id") }
    }

    @Test
    fun `the app is paired with a pasted code, and told why a code does not do`() {
        show()
        assertTrue("pair with a server" in pairButton())
        click("pair")
        tag("pair-code").performTextInput("https://drop.example.org/config#devices")
        click("pair-ok")
        wait { seen("note") }
        assertTrue("not a pairing code" in text("note"))
        assertNull(w.store.now.settings.pairing)
        click("pair")
        tag("pair-code").performTextInput(CODE)
        click("pair-ok")
        wait { w.store.now.settings.device == "field phone" }
        assertEquals("https://drop.example.org" to "sII2Bs1adMHfku5e1T3FYIh9W1U7m8OASln7pIJCU08", w.pairings.first().let { it.url to it.token })
        wait { "paired as field phone" in pairButton() }
        assertTrue("paired as field phone" in text("note"))
    }

    @Test
    fun `a code opened as a link is asked about before it pairs, and a revoked one is refused`() {
        show()
        w.session.pairRequest.value = CODE
        wait { seen("pair-host") }
        assertTrue("https://drop.example.org" in text("pair-host"))
        assertNull(w.store.now.settings.pairing)                                // nothing yet: the pilot has not said yes
        w.refused(401, "this door takes a paired device's token")
        tag("pair-yes").performClick()
        wait { seen("note") }
        assertTrue("does not take this code" in text("note"), text("note"))
        assertNull(w.store.now.settings.pairing)
        assertFalse(seen("pair-host"))
        w.drop.fail = null
        w.session.pairRequest.value = CODE
        wait { seen("pair-yes") }
        tag("pair-yes").performClick()
        wait { w.store.now.settings.device == "field phone" }
    }

    @Test
    fun `a link's code is shown as it would pair, and one that would not pair cannot be said yes to`() {
        show()
        val token = "sII2Bs1adMHfku5e1T3FYIh9W1U7m8OASln7pIJCU08"
        w.session.pairRequest.value = "acroscope://pair?url=https%3A%2F%2Fdrop.example.org%2F&token=$token"
        wait { seen("pair-host") }
        assertEquals("https://drop.example.org", text("pair-host").removeSurrounding("[", "]"))   // what pairing would take, said the same way
        tag("pair-yes").assertIsEnabled()
        for ((code, why) in listOf("acroscope://pair?url=https%3A%2F%2Fdrop.example.org&token=$token&url=https%3A%2F%2Fother.example.org" to "twice", "acroscope://pair?url=%&token=$token" to "damaged")) {
            w.session.pairRequest.value = code
            wait { why in text("pair-host") }
            tag("pair-yes").assertIsNotEnabled()
        }
        assertEquals(emptyList(), w.pairings)
    }

    @Test
    fun `sizes are said as the rest of the app says them`() {
        collected()
        show()
        bring("upload")
        val id = w.store.now.batches.single().id
        assertEquals("2 clips, ${Format.bytes((movie(100).size + movie(200).size).toLong())}", text("contents-$id").removeSurrounding("[", "]"))   // as the screen says it
    }
}
