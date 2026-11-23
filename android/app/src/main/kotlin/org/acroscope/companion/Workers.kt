package org.acroscope.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.ServiceInfo
import android.net.Uri
import androidx.core.app.NotificationCompat
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

/** What starts a collect and an upload so that they go on when the screen is left. */
interface Runner {
    /** Collect what is there: the quad's log, and the clips of the card under `tree` when there is one. */
    fun collect(tree: Uri?)
    fun upload()
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
    override fun run() = applicationContext.graph.session.uploadNow { isStopped }
}
