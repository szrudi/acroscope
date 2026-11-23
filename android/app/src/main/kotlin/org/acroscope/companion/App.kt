package org.acroscope.companion

import android.app.Application
import android.content.Context
import org.acroscope.companion.core.Collector
import org.acroscope.companion.core.Drop
import org.acroscope.companion.core.DropClient
import org.acroscope.companion.core.Pairing
import org.acroscope.companion.core.Store
import org.acroscope.companion.core.Uploader
import java.io.File
import java.time.ZonedDateTime

/** Everything the app is made of, in one place a test can build with stand-ins. */
class Graph(
    val store: Store,
    val cards: Cards,
    val runner: Runner,
    drop: (Pairing) -> Drop = { DropClient(it) },
    clock: () -> ZonedDateTime = ZonedDateTime::now,
) {
    val collector = Collector(store, clock)
    val uploader = Uploader(store, drop)
    val session = Session(this)
}

class App : Application() {
    /** Built at the first use; a test puts its own here before the activity starts. */
    var graph: Graph? = null
        get() = field ?: real().also { field = it }

    private fun real(): Graph {
        // the state (with the token) in the app's own storage, the footage where there is room
        val files = getExternalFilesDir(null) ?: filesDir
        return Graph(Store(File(filesDir, "state"), files), SafCards(this), WorkRunner(this))
    }
}

val Context.graph: Graph get() = (applicationContext as App).graph!!
