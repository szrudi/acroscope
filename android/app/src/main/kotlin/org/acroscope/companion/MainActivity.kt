package org.acroscope.companion

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbManager
import android.media.AudioManager
import android.media.ToneGenerator
import android.net.Uri
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.storage.StorageManager
import android.os.storage.StorageVolume
import android.provider.DocumentsContract
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import java.io.FileNotFoundException

class MainActivity : ComponentActivity() {
    private val storage by lazy { getSystemService(StorageManager::class.java) }
    private val volumes = object : StorageManager.StorageVolumeCallback() {
        override fun onStateChanged(volume: StorageVolume) {
            graph.session.lookAtCard()                       // a card went in or out: the reader often shows up seconds after the plug
        }
    }

    /** A flight controller went in or out, or the pilot answered whether the app may talk to it. */
    private val usb = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            graph.session.lookAtQuad()
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
                beforeJob = { notify.launch(Manifest.permission.POST_NOTIFICATIONS) },   // the job's progress is a notification; it runs without one too
                openStorage = { runCatching { startActivity(Intent(Settings.ACTION_INTERNAL_STORAGE_SETTINGS)) } },
                alarm = ::alarm)
        }
    }

    private val tone by lazy { ToneGenerator(AudioManager.STREAM_ALARM, ToneGenerator.MAX_VOLUME) }

    /** A quad too warm on the bench: a buzz and a tone, while this screen is the one in front. */
    private fun alarm() {
        if (!lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return
        runCatching { getSystemService(Vibrator::class.java).vibrate(VibrationEffect.createWaveform(longArrayOf(0, 300, 150, 300), -1)) }
        runCatching { tone.startTone(ToneGenerator.TONE_CDMA_ALERT_CALL_GUARD, 700) }
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
        val plugs = IntentFilter(UsbManager.ACTION_USB_DEVICE_ATTACHED).apply { addAction(UsbManager.ACTION_USB_DEVICE_DETACHED); addAction(UsbQuads.PERMISSION) }
        ContextCompat.registerReceiver(this, usb, plugs, ContextCompat.RECEIVER_NOT_EXPORTED)
        graph.session.lookAtCard()
        graph.session.lookAtQuad()
        graph.session.askServer()
        graph.session.kick()
        graph.session.watch(true)
    }

    override fun onStop() {
        graph.session.left()
        graph.session.watch(false)
        storage.unregisterStorageVolumeCallback(volumes)
        unregisterReceiver(usb)
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
