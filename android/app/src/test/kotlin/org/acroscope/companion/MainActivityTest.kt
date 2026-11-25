package org.acroscope.companion

import android.content.Context
import android.content.Intent
import android.hardware.usb.UsbManager
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.GraphicsMode
import kotlin.test.assertEquals
import kotlin.test.assertNull

/** The activity itself, as Android starts it: with a link, turned round, and told of a plug while it is in front. */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class MainActivityTest {
    private val w = World().also { (ApplicationProvider.getApplicationContext<Context>() as App).graph = it.graph }

    private fun until(what: () -> Boolean) {
        val end = System.currentTimeMillis() + 5000
        while (!what()) {
            check(System.currentTimeMillis() < end) { "not within 5 s" }
            Thread.sleep(20)
        }
    }

    @Test
    fun `a pairing link is asked about once, not again when the screen turns or after a no`() {
        val started = Robolectric.buildActivity(MainActivity::class.java, Intent(Intent.ACTION_VIEW, Uri.parse(CODE))).setup()
        assertEquals(CODE, w.session.pairRequest.value)
        w.session.pairRequest.value = null                                     // the pilot said no
        started.recreate()                                                     // the phone turned: the same intent, a new activity
        assertNull(w.session.pairRequest.value)
        started.newIntent(Intent(Intent.ACTION_VIEW, Uri.parse(CODE)))          // opened as a link again: asked again
        assertEquals(CODE, w.session.pairRequest.value)
    }

    @Test
    fun `a link that is not a pairing code is not asked about`() {
        Robolectric.buildActivity(MainActivity::class.java, Intent(Intent.ACTION_VIEW, Uri.parse("https://drop.example.org/pair"))).setup()
        assertNull(w.session.pairRequest.value)
    }

    @Test
    fun `a quad chosen for the app while it is in front is looked at`() {
        val started = Robolectric.buildActivity(MainActivity::class.java).setup()
        until { w.session.quad.value == QuadView.None }
        w.quads.now = QuadState.NeedsPermission("OTTO") {}
        w.cards.now = CardState.NeedsAccess("COBRA", Intent(Intent.ACTION_OPEN_DOCUMENT_TREE))
        started.newIntent(Intent(UsbManager.ACTION_USB_DEVICE_ATTACHED))      // "always open with" this app: it comes as an intent
        until { w.session.quad.value is QuadView.Locked && w.session.card.value is CardView.Locked }
    }
}
