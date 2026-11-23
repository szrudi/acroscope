package org.acroscope.companion

import android.content.Intent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.acroscope.companion.core.Batch
import org.acroscope.companion.core.Format
import org.acroscope.companion.core.Names
import org.acroscope.companion.core.Stage
import java.time.LocalDate

private val Scheme = darkColorScheme(
    primary = Color(0xFFF5A524), onPrimary = Color(0xFF1A1200), secondary = Color(0xFF4CC2FF), error = Color(0xFFFF6B6B),
    background = Color(0xFF14161A), surface = Color(0xFF14161A), surfaceContainerHighest = Color(0xFF1C1F25), surfaceVariant = Color(0xFF1C1F25),
    onSurface = Color(0xFFE6E8EC), onSurfaceVariant = Color(0xFF9AA3B2), outline = Color(0xFF3A404B),
)

/**
 * The one screen: where you fly, what is plugged in, what waits to go up, how what went up is doing. `openCard`
 * asks the pilot for a card's folder, `pickLogs` for saved logs, `beforeJob` runs before a collect or an upload
 * starts (the notification permission).
 */
@Composable
fun Screen(g: Graph, openCard: (Intent) -> Unit, pickLogs: () -> Unit, beforeJob: () -> Unit = {}) {
    val state by g.store.state.collectAsStateWithLifecycle()
    val card by g.session.card.collectAsStateWithLifecycle()
    val collect by g.session.collect.collectAsStateWithLifecycle()
    val upload by g.session.upload.collectAsStateWithLifecycle()
    val note by g.session.note.collectAsStateWithLifecycle()
    val events by g.session.events.collectAsStateWithLifecycle()
    val linked by g.session.pairRequest.collectAsStateWithLifecycle()
    var pairing by remember { mutableStateOf(false) }
    var editing by remember { mutableStateOf<String?>(null) }
    val sent = state.batches.filter { it.stage != Stage.PENDING }.sortedByDescending { it.id }

    MaterialTheme(colorScheme = Scheme) {
        Surface(Modifier.fillMaxSize()) {
            LazyColumn(Modifier.fillMaxSize().safeDrawingPadding().testTag("list"), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                item {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
                        Text("acroscope", style = MaterialTheme.typography.titleLarge, color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold)
                        TextButton({ pairing = !pairing }, Modifier.testTag("pair")) { Text(state.settings.device?.let { "paired as $it" } ?: "pair with a server") }
                    }
                }
                if (pairing) item { PairCard(onPair = { g.session.pair(it); pairing = false }, onClose = { pairing = false }) }
                note?.let { item { Notice(it, Modifier.testTag("note")) { g.session.note.value = null } } }
                item {
                    Section("Where are you flying?") {
                        OutlinedTextField(state.settings.event, g.session::setEvent, Modifier.fillMaxWidth().testTag("event"), singleLine = true, placeholder = { Text("home, park, whoop race…") })
                        LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            items((state.settings.recentEvents + events).distinct().filter { it != state.settings.event }) { name ->
                                AssistChip({ g.session.setEvent(name) }, { Text(name) }, Modifier.testTag("event-$name"))
                            }
                        }
                        Dim("today's flying is filed under ${Names.session(LocalDate.now().toString(), state.settings.event)}", Modifier.testTag("session"))
                    }
                }
                item {
                    Section("Collect") {
                        Text(g.session.describe(card), Modifier.testTag("card"))
                        Progress(collect, Modifier.testTag("collect-status"))
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            when (val c = card) {
                                is CardView.Locked -> Button({ openCard(c.request) }, Modifier.testTag("allow")) { Text("Allow the card") }
                                is CardView.Open -> Button({ beforeJob(); g.session.startCollect() }, Modifier.testTag("start"), enabled = c.clips > 0 && collect !is Work.Running) { Text("Start") }
                                else -> Button({}, Modifier.testTag("start"), enabled = false) { Text("Start") }
                            }
                            OutlinedButton(pickLogs, Modifier.testTag("add-log")) { Text("Add a saved log") }
                        }
                        Dim("${Format.bytes(g.store.free)} free on the phone")
                    }
                }
                if (state.pending.isNotEmpty()) item {
                    Section("Waiting to go up") {
                        state.pending.forEach { b ->
                            PendingBatch(b, onEdit = { editing = if (editing == b.id) null else b.id }, onRemove = { g.session.remove(b.id, it) })
                            if (editing == b.id)
                                EditBatch(b, onSave = { e, d -> if (g.session.edit(b.id, e, d)) editing = null }, onDiscard = { g.session.discard(b.id); editing = null }, onClose = { editing = null })
                        }
                        Progress(upload, Modifier.testTag("upload-status"))
                        Button({ beforeJob(); g.session.startUpload() }, Modifier.testTag("upload"), enabled = upload !is Work.Running) { Text("Upload") }
                    }
                } else if (upload is Work.Finished) item { Section("Upload") { Progress(upload, Modifier.testTag("upload-status")) } }
                if (sent.isNotEmpty()) item {
                    Section("With the server") {
                        sent.forEach { b ->
                            Column(Modifier.fillMaxWidth().testTag("sent-${b.id}")) {
                                Text(b.session, fontWeight = FontWeight.Medium)
                                Text(Format.status(b.server), Modifier.testTag("status-${b.id}"), color = if (b.server?.status == "failed") MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
                                    style = MaterialTheme.typography.bodySmall)
                                if (b.stage == Stage.COMMITTED) Dim("${Format.contents(b.items)} kept on the phone until the server is done")
                                if (b.server?.status == "failed") TextButton({ g.session.retry(b.id) }, Modifier.testTag("retry-${b.id}")) { Text("Try again on the server") }
                            }
                        }
                        TextButton({ g.session.askServer() }, Modifier.testTag("check")) { Text("Check now") }
                    }
                }
            }
        }
        linked?.let { code ->
            AlertDialog(onDismissRequest = { g.session.pairRequest.value = null }, title = { Text("Pair with this server?") },
                text = { Text(code.substringAfter("url=").substringBefore("&").let { java.net.URLDecoder.decode(it, "UTF-8") }, Modifier.testTag("pair-host")) },
                confirmButton = { TextButton({ g.session.pair(code); g.session.pairRequest.value = null }, Modifier.testTag("pair-yes")) { Text("Pair") } },
                dismissButton = { TextButton({ g.session.pairRequest.value = null }) { Text("No") } })
        }
    }
}

@Composable
private fun Section(title: String, content: @Composable () -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title.uppercase(), style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            content()
        }
    }
}

@Composable
private fun Dim(text: String, modifier: Modifier = Modifier) = Text(text, modifier, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)

@Composable
private fun Notice(text: String, modifier: Modifier, onClose: () -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Row(Modifier.padding(start = 14.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(text, modifier.weight(1f))
            TextButton(onClose) { Text("OK") }
        }
    }
}

@Composable
private fun Progress(work: Work, modifier: Modifier) {
    when (work) {
        Work.Idle -> {}
        is Work.Running -> Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("${work.what} · ${Format.bytes(work.done)} of ${Format.bytes(work.total)}", modifier, style = MaterialTheme.typography.bodySmall)
            LinearProgressIndicator({ if (work.total > 0) work.done.toFloat() / work.total else 0f }, Modifier.fillMaxWidth())
        }
        is Work.Finished -> Text(work.message, modifier, color = if (work.ok) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.error)
    }
}

@Composable
private fun PendingBatch(b: Batch, onEdit: () -> Unit, onRemove: (String) -> Unit) {
    Column(Modifier.fillMaxWidth().testTag("batch-${b.id}")) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
            Column(Modifier.weight(1f)) {
                Text(b.session, fontWeight = FontWeight.Medium)
                Dim(Format.contents(b.items))
            }
            TextButton(onEdit, Modifier.testTag("edit-${b.id}")) { Text("Change") }
        }
        b.items.forEach { i ->
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(i.name, style = MaterialTheme.typography.bodySmall)
                    if (i.unfinalised) Text("no index: the recording was cut off", Modifier.testTag("flag-${i.name}"), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
                }
                Dim(Format.bytes(i.size))
                TextButton({ onRemove(i.name) }, Modifier.testTag("remove-${i.name}")) { Text("Remove") }
            }
        }
    }
}

/** Pairing, in the screen itself: a field for the code, not a window over it (a keyboard and a modal quarrel). */
@Composable
private fun PairCard(onPair: (String) -> Unit, onClose: () -> Unit) {
    var code by remember { mutableStateOf("") }
    Section("Pair with a server") {
        Dim("On the server's configuration page, under devices, pair a device. Open the code's link on this phone, or paste the code here.")
        OutlinedTextField(code, { code = it }, Modifier.fillMaxWidth().testTag("pair-code"), placeholder = { Text("acroscope://pair?…") })
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button({ onPair(code) }, Modifier.testTag("pair-ok"), enabled = code.isNotBlank()) { Text("Pair") }
            TextButton(onClose) { Text("Cancel") }
        }
    }
}

/** A pending batch's event and date put right, under the batch itself. */
@Composable
private fun EditBatch(b: Batch, onSave: (String, String) -> Unit, onDiscard: () -> Unit, onClose: () -> Unit) {
    var event by remember(b.id) { mutableStateOf(b.event) }
    var date by remember(b.id) { mutableStateOf(b.date) }
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedTextField(event, { event = it }, Modifier.fillMaxWidth().testTag("edit-event"), singleLine = true, label = { Text("event") })
        OutlinedTextField(date, { date = it }, Modifier.fillMaxWidth().testTag("edit-date"), singleLine = true, label = { Text("date (YYYY-MM-DD)") })
        Dim("filed under ${Names.session(date, event)}", Modifier.testTag("edit-session"))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button({ onSave(event, date) }, Modifier.testTag("edit-save")) { Text("Save") }
            TextButton(onClose) { Text("Cancel") }
            TextButton(onDiscard, Modifier.testTag("edit-discard")) { Text("Discard the batch", color = MaterialTheme.colorScheme.error) }
        }
    }
}
