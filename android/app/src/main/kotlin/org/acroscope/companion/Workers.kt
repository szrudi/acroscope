package org.acroscope.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.ServiceInfo
import android.net.Uri
import androidx.core.app.NotificationCompat
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.sample
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.acroscope.companion.core.Auto
import org.acroscope.companion.core.Next
import java.io.IOException
import java.util.concurrent.TimeUnit

/** What starts a collect and an upload so that they go on when the screen is left. */
interface Runner {
    /** Collect what is there: the quad's log, and the clips of the card under `tree` when there is one. */
    fun collect(tree: Uri?)
    fun upload()
    /** An upload by itself, as soon as the phone is on a network that costs nothing. */
    fun auto()
    /** A look, in a while, at how the server is doing with what was handed in. */
    fun follow()
}

/** The jobs as WorkManager work: one collect and one upload at a time, each showing its progress as a notification. */
class WorkRunner(private val context: Context) : Runner {
    override fun collect(tree: Uri?) {
        WorkManager.getInstance(context).enqueueUniqueWork("collect", ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<CollectWorker>().setInputData(workDataOf("tree" to tree?.toString())).build())
    }

    override fun upload() {
        WorkManager.getInstance(context).enqueueUniqueWork("upload", ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<UploadWorker>().setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()).build())
    }

    // Both survive the app being closed and the phone restarting: WorkManager keeps them. KEEP: asking again while
    // one waits (for the network, or for its next try) changes nothing, so its backing off is not started over.
    override fun auto() {
        WorkManager.getInstance(context).enqueueUniqueWork(AUTO, ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<AutoUploadWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.UNMETERED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 1, TimeUnit.MINUTES).build())
    }

    override fun follow() {
        WorkManager.getInstance(context).enqueueUniqueWork(FOLLOW, ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<FollowWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setInitialDelay(2, TimeUnit.MINUTES)
                .setBackoffCriteria(BackoffPolicy.LINEAR, 5, TimeUnit.MINUTES).build())
    }

    companion object {
        const val AUTO = "auto-upload"
        const val FOLLOW = "follow"
    }
}

private const val CHANNEL = "work"

private fun notification(context: Context, title: String, work: Work): Notification {
    val manager = context.getSystemService(NotificationManager::class.java)
    manager.createNotificationChannel(NotificationChannel(CHANNEL, "Collecting and uploading", NotificationManager.IMPORTANCE_LOW))
    val b = NotificationCompat.Builder(context, CHANNEL).setSmallIcon(R.drawable.ic_stat).setContentTitle(title).setOnlyAlertOnce(true)
    when (work) {
        is Work.Running -> b.setContentText(work.what).setOngoing(true)
            .setProgress(1000, if (work.total > 0) (work.done * 1000 / work.total).toInt() else 0, work.total <= 0)
        is Work.Finished -> b.setContentText(work.message).setStyle(NotificationCompat.BigTextStyle().bigText(work.message))
        Work.Idle -> b.setContentText("")
    }
    return b.build()
}

/** A job that copies or sends for minutes: in the foreground while the phone allows it, with its progress shown. */
abstract class Job(context: Context, params: WorkerParameters, private val id: Int, private val title: String) : CoroutineWorker(context, params) {
    protected abstract val progress: MutableStateFlow<Work>
    protected abstract fun run(): Work.Finished

    @OptIn(kotlinx.coroutines.FlowPreview::class)
    override suspend fun doWork(): Result {
        val manager = applicationContext.getSystemService(NotificationManager::class.java)
        try {
            setForeground(ForegroundInfo(id, notification(applicationContext, title, progress.value), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC))
        } catch (e: IllegalStateException) {
            // started while the app is not in front: the phone gives the job its ordinary ten minutes; an upload goes on next time
        }
        val done = withContext(Dispatchers.IO) {
            val shown = launch { progress.sample(1000).collect { if (it is Work.Running) manager.notify(id, notification(applicationContext, title, it)) } }
            try { run() } finally { shown.cancel() }
        }
        manager.notify(id + 100, notification(applicationContext, title, done))      // what came of it stays to be read
        return Result.success()
    }
}

class CollectWorker(context: Context, params: WorkerParameters) : Job(context, params, 1, "Collecting") {
    override val progress get() = applicationContext.graph.session.collect
    override fun run() = applicationContext.graph.session.collectNow(inputData.getString("tree")?.let(Uri::parse), { isStopped })
}

class UploadWorker(context: Context, params: WorkerParameters) : Job(context, params, 2, "Uploading to acroscope") {
    override val progress get() = applicationContext.graph.session.upload
    override fun run() = applicationContext.graph.session.let { s -> s.say(s.uploadNow { isStopped }) }
}

/**
 * The upload by itself. It runs when the phone is on an unmetered network; a run the phone cuts short, or one that
 * finds no server (not at home), is tried again later with what went up kept; what only the pilot can put right is
 * said in a notification and not tried again.
 */
class AutoUploadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val g = applicationContext.graph
        if (!Auto.wanted(g.store.now)) return Result.success()
        val result = withContext(Dispatchers.IO) { g.session.uploadNow { isStopped } }
        return when (Auto.next(result)) {
            Next.LATER -> Result.retry()
            Next.DONE -> Result.success()                                       // the server's turn; uploadNow asked for the look-in
            Next.PILOT -> Result.success().also {
                applicationContext.getSystemService(NotificationManager::class.java).notify(102, notification(applicationContext, "Uploading to acroscope", g.session.say(result)))
            }
        }
    }
}

/** A look at what the server made of the batches handed in; again later while it is still busy with one, for a
 *  day at most (the app looks too whenever it is opened). When it reports a batch done, the phone's copy goes. */
class FollowWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val g = applicationContext.graph
        try {
            withContext(Dispatchers.IO) { g.uploader.refresh() }
        } catch (e: IOException) {
            return if (runAttemptCount < TRIES) Result.retry() else Result.success()
        }
        return if (Auto.following(g.store.now) && runAttemptCount < TRIES) Result.retry() else Result.success()
    }

    companion object {
        const val TRIES = 24
    }
}
