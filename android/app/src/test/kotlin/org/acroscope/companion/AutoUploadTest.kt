package org.acroscope.companion

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.work.ListenableWorker.Result
import androidx.work.NetworkType
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.testing.TestListenableWorkerBuilder
import androidx.work.testing.WorkManagerTestInitHelper
import kotlinx.coroutines.runBlocking
import org.acroscope.companion.core.DropException
import org.acroscope.companion.core.ServerStatus
import org.acroscope.companion.core.Stage
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import java.io.File
import java.io.IOException
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/** The unattended jobs as WorkManager runs them, with the app's parts replaced by stand-ins. */
@RunWith(RobolectricTestRunner::class)
class AutoUploadTest {
    private val context: Context = ApplicationProvider.getApplicationContext()
    private val w = World()

    @Before
    fun install() {
        (context as App).graph = w.graph
        WorkManagerTestInitHelper.initializeTestWorkManager(context)
    }

    private fun pending(): String {
        w.card(FakeFile("VID00001.mov", movie(100)))
        w.store.update { it.copy(settings = it.settings.copy(event = "park", autoUpload = true, url = "https://drop.example.org", token = "t".repeat(43), device = "field phone")) }
        w.session.collectNow(TREE) { false }
        w.runner.ran.clear()
        return w.store.now.batches.single().id
    }

    private fun auto(): Result = runBlocking { TestListenableWorkerBuilder<AutoUploadWorker>(context).build().doWork() }
    private fun follow(attempt: Int = 0): Result = runBlocking { TestListenableWorkerBuilder<FollowWorker>(context).setRunAttemptCount(attempt).build().doWork() }
    private fun notes() = shadowOf(context.getSystemService(android.app.NotificationManager::class.java)).allNotifications.map { shadowOf(it).contentText.toString() }

    @Test
    fun `at home the batch goes up, and a look at the server is asked for`() {
        val id = pending()
        assertEquals(Result.success(), auto())
        assertEquals(Stage.COMMITTED, w.store.now.batch(id)!!.stage)
        assertEquals(listOf("follow"), w.runner.ran)
        assertEquals(emptyList(), notes())                                       // nothing to tell: it went
    }

    @Test
    fun `away from home it is tried again later, without a word`() {
        val id = pending()
        w.drop.fail = IOException("no route to host")
        assertEquals(Result.retry(), auto())
        assertEquals(Stage.PENDING, w.store.now.batch(id)!!.stage)
        w.refused(507, "the drop is full")
        assertEquals(Result.retry(), auto())
        assertEquals(emptyList(), notes())
        assertEquals(emptyList(), w.runner.ran)
    }

    @Test
    fun `what only the pilot can put right is said once and not tried again`() {
        pending()
        w.refused(401, "this door takes a paired device's token")
        assertEquals(Result.success(), auto())
        assertEquals(1, notes().size)
        assertTrue("pair it again" in notes().single(), notes().toString())
    }

    @Test
    fun `switched off, or with nothing waiting, a run does nothing`() {
        val id = pending()
        w.store.update { it.copy(settings = it.settings.copy(autoUpload = false)) }
        assertEquals(Result.success(), auto())
        assertEquals(Stage.PENDING, w.store.now.batch(id)!!.stage)
        assertEquals(emptyList(), w.drop.calls)
    }

    @Test
    fun `the server is looked in on until it is done, and then the phone's copy goes`() {
        val id = pending()
        auto()
        w.drop.statuses[id] = ServerStatus("running")
        assertEquals(Result.retry(), follow())                                   // still busy: look again later
        assertTrue(File(w.store.dir(id), "VID00001.mov").exists())
        w.drop.fail = IOException("no route to host")
        assertEquals(Result.retry(), follow())                                   // not reachable now: later
        assertEquals(Result.success(), follow(attempt = FollowWorker.TRIES))     // but not for ever
        w.drop.fail = null
        assertEquals(Result.success(), follow(attempt = FollowWorker.TRIES))
        w.drop.statuses[id] = ServerStatus("done", "2026-12-12-park", null, listOf("2026-12-12_001"), emptyList())
        assertEquals(Result.success(), follow())
        assertEquals(Stage.DONE, w.store.now.batch(id)!!.stage)
        assertFalse(w.store.dir(id).exists())
    }

    @Test
    fun `a failed batch is not looked in on again by itself`() {
        val id = pending()
        auto()
        w.drop.statuses[id] = ServerStatus("failed", error = "boom")
        assertEquals(Result.success(), follow())
        assertEquals("failed", w.store.now.batch(id)!!.server!!.status)
    }

    @Test
    fun `the unattended upload waits for a network that costs nothing`() {
        val runner = WorkRunner(context)
        runner.auto()
        runner.follow()
        runner.upload()
        val work = WorkManager.getInstance(context)
        fun info(name: String): WorkInfo = work.getWorkInfosForUniqueWork(name).get().single()
        assertEquals(NetworkType.UNMETERED, info(WorkRunner.AUTO).constraints.requiredNetworkType)
        assertEquals(NetworkType.CONNECTED, info(WorkRunner.FOLLOW).constraints.requiredNetworkType)
        assertEquals(NetworkType.CONNECTED, info("upload").constraints.requiredNetworkType)   // the button's upload goes on any network: the pilot asked
        assertTrue(info(WorkRunner.FOLLOW).initialDelayMillis >= 60_000)
        runner.auto()                                                            // asked again while it waits: still one
        assertEquals(1, work.getWorkInfosForUniqueWork(WorkRunner.AUTO).get().size)
    }
}
