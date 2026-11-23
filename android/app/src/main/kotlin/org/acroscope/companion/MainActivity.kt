package org.acroscope.companion

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.storage.StorageManager
import android.os.storage.StorageVolume
import android.provider.DocumentsContract
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import java.io.FileNotFoundException

class MainActivity : ComponentActivity() {
    private val storage by lazy { getSystemService(StorageManager::class.java) }
    private val volumes = object : StorageManager.StorageVolumeCallback() {
        override fun onStateChanged(volume: StorageVolume) {
            graph.session.lookAtCard()                       // a card went in or out: the reader often shows up seconds after the plug
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        take(intent)
        setContent {
            val session = graph.session
            val tree = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { r -> r.data?.data?.let(session::allow) }
            val logs = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris -> if (uris.isNotEmpty()) session.addLogs(uris.map(::picked)) }
            val notify = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
            Screen(graph, openCard = { tree.launch(it) }, pickLogs = { logs.launch(arrayOf("*/*")) },
                beforeJob = { notify.launch(Manifest.permission.POST_NOTIFICATIONS) })   // the job's progress is a notification; it runs without one too
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        take(intent)
    }

    /** A pairing code opened as a link waits for the pilot's yes on the screen. */
    private fun take(intent: Intent?) {
        val data = intent?.data ?: return
        if (data.scheme == "acroscope") graph.session.pairRequest.value = data.toString()
    }

    override fun onStart() {
        super.onStart()
        storage.registerStorageVolumeCallback(mainExecutor, volumes)
        graph.session.lookAtCard()
        graph.session.askServer()
    }

    override fun onStop() {
        storage.unregisterStorageVolumeCallback(volumes)
        super.onStop()
    }

    private fun picked(uri: Uri): Picked {
        var name = uri.lastPathSegment ?: "log.bbl"
        var modified: Long? = null
        contentResolver.query(uri, arrayOf(DocumentsContract.Document.COLUMN_DISPLAY_NAME, DocumentsContract.Document.COLUMN_LAST_MODIFIED), null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                c.getString(0)?.let { name = it }
                if (!c.isNull(1)) modified = c.getLong(1)
            }
        }
        return Picked(name, modified) { contentResolver.openInputStream(uri) ?: throw FileNotFoundException(name) }
    }
}
