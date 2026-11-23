package org.acroscope.companion

import android.content.Intent
import androidx.compose.ui.test.assertIsEnabled
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
    private val today = LocalDate.now().toString()

    private fun show() {
        rule.setContent { Screen(w.graph, openCard = { asked += it }, pickLogs = { logsPicked++ }, beforeJob = { beforeJob++ }) }
        w.session.lookAtCard()
        w.session.askServer()
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
    fun `a card is allowed once, then says what it holds, and start collects it`() {
        w.locked()
        show()
        wait { seen("allow") }
        assertTrue("allow the app to read it" in text("card"))
        click("allow")
        assertEquals(1, asked.size)                                             // the system's folder picker, opened at the card
        w.card(FakeFile("VID00001.mov", movie(100)), FakeFile("VID00002.mov", movie(200)), FakeFile("notes.txt", ByteArray(3)))
        w.session.lookAtCard()
        wait { seen("start") && "2 new clips" in text("card") }
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
        tag("start").assertIsNotEnabled()
        bring("batch-${batch.id}")
        assertTrue(seen("remove-VID00001.mov") && seen("remove-VID00002.mov"))
        assertEquals(listOf("park"), w.store.now.settings.recentEvents)
    }

    @Test
    fun `without a card there is nothing to start, and a saved log can still be added`() {
        show()
        assertTrue("no card plugged in" in text("card"))
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
        assertEquals(listOf("upload"), w.runner.ran)
        assertEquals(listOf("upload $id VID00001.mov", "upload $id VID00002.mov", "put $id import.json", "commit $id"), w.drop.calls.filter { !it.startsWith("batches") && it != "events" })
        wait { seen("sent-$id") }
        assertTrue("uploaded" in text("upload-status"), text("upload-status"))
        assertEquals(Stage.COMMITTED, w.store.now.batch(id)!!.stage)
        assertTrue("waiting its turn" in text("status-$id"))
        assertTrue(File(w.store.dir(id), "VID00001.mov").exists())
        w.drop.statuses[id] = ServerStatus("failed", error = "ValueError: VID00002.mov: no video stream")
        bring("check")
        click("check")
        wait { seen("retry-$id") }
        assertTrue("no video stream" in text("status-$id"), text("status-$id"))
        click("retry-$id")
        wait { "retry $id" in w.drop.calls }
        w.drop.statuses[id] = ServerStatus("done", "$today-park", null, listOf("${today}_001", "${today}_002"), emptyList())
        click("check")
        wait { w.store.now.batch(id)!!.stage == Stage.DONE }
        assertFalse(w.store.dir(id).exists())                                   // the server has it: the phone's copy is gone
        wait { "done: 2 clips in $today-park" in text("status-$id") }
        assertFalse(seen("retry-$id"))
    }

    @Test
    fun `an upload that cannot go says why in the pilot's words`() {
        val id = collected()
        show()
        bring("upload")
        click("upload")
        assertTrue("pair the app with the server first" in text("upload-status"))
        w.paired()
        for ((failure, said) in listOf(java.io.IOException("no route to host") to "did not answer (no route to host)",
            org.acroscope.companion.core.DropException(401, "this door takes a paired device's token") to "pair it again",
            org.acroscope.companion.core.DropException(507, "the drop is full: 1 of 2 bytes") to "no room right now: the drop is full")) {
            w.drop.fail = failure
            click("upload")
            assertTrue(said in text("upload-status"), text("upload-status"))
            assertEquals(Stage.PENDING, w.store.now.batch(id)!!.stage)
        }
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
    fun `sizes are said as the rest of the app says them`() {
        collected()
        show()
        bring("upload")
        assertTrue(rule.onAllNodesWithTag("batch-${w.store.now.batches.single().id}").fetchSemanticsNodes().isNotEmpty())
        assertEquals("2 clips, ${Format.bytes((movie(100).size + movie(200).size).toLong())}", Format.contents(w.store.now.batches.single().items))
    }
}
