package org.acroscope.companion

import android.content.Intent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.acroscope.companion.core.Batch
import kotlinx.coroutines.delay
import org.acroscope.companion.core.Format
import org.acroscope.companion.core.Heat
import org.acroscope.companion.core.Names
import org.acroscope.companion.core.Pairing
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
 * starts (the notification permission), `openStorage` shows the phone's own storage screen, where a card is ejected
 * (an app may not eject one), `alarm` makes itself felt and heard once: it is called again and again while a quad on
 * USB is too warm.
 */
@Composable
fun Screen(g: Graph, openCard: (Intent) -> Unit, pickLogs: () -> Unit, beforeJob: () -> Unit = {}, openStorage: () -> Unit = {}, alarm: () -> Unit = {}) {
    val focus = LocalFocusManager.current
    val heat by g.session.heat.collectAsStateWithLifecycle()
    val hot = Heat.hot(heat)
    val flash by g.session.flash.collectAsStateWithLifecycle()
    LaunchedEffect(hot) {
        while (hot) {
            alarm()
            delay(ALARM_EVERY_MS)
        }
    }
    val state by g.store.state.collectAsStateWithLifecycle()
    val card by g.session.card.collectAsStateWithLifecycle()
    val quad by g.session.quad.collectAsStateWithLifecycle()
    val erase by g.session.erase.collectAsStateWithLifecycle()
    val free by g.session.free.collectAsStateWithLifecycle()
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
                if (state.settings.pairing != null) item {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
                        Column(Modifier.weight(1f)) {
                            Text("Upload by itself")
                            Dim("when the phone is on Wi-Fi that costs nothing and the server answers")
                        }
                        Switch(state.settings.autoUpload, { g.session.setAuto(it) }, Modifier.testTag("auto"))
                    }
                }
                note?.let { item { Notice(it, Modifier.testTag("note")) { g.session.note.value = null } } }
                item {
                    Section("Where are you flying?") {
                        OutlinedTextField(state.settings.event, g.session::setEvent, Modifier.fillMaxWidth().testTag("event"), singleLine = true, placeholder = { Text("home, park, whoop race…") },
                            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done), keyboardActions = KeyboardActions(onDone = { focus.clearFocus() }))   // the keyboard goes with the focus
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
                        Text(g.session.describe(card, freeing = free is Work.Running), Modifier.testTag("card"))
                        Text(g.session.describe(quad, flash, erasing = erase is Work.Running), Modifier.testTag("quad"))
                        heat?.let { t ->
                            if (hot) Text("the quad's core is at $t °C: unplug it and let it cool", Modifier.testTag("heat"), color = MaterialTheme.colorScheme.error, fontWeight = FontWeight.Bold)
                            else Dim("the quad's core is at $t °C", Modifier.testTag("heat"))
                        }
                        Progress(collect, Modifier.testTag("collect-status"))
                        (erase as? Work.Running)?.let { e ->                 // unplugging now leaves the flash half erased: hard to miss
                            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                Text(e.what, Modifier.testTag("erase-warning"), color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold)
                                LinearProgressIndicator(Modifier.fillMaxWidth())
                            }
                        } ?: Progress(erase, Modifier.testTag("erase-status"))
                        val something = ((card as? CardView.Open)?.clips ?: 0) > 0 || (quad is QuadView.Ready && flash != 0L)
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            (card as? CardView.Locked)?.let { c -> Button({ openCard(c.request) }, Modifier.testTag("allow")) { Text("Allow the card") } }
                            (quad as? QuadView.Locked)?.let { q -> Button(q.ask, Modifier.testTag("allow-quad")) { Text("Allow the quad") } }
                            Button({ focus.clearFocus(); beforeJob(); g.session.startCollect() }, Modifier.testTag("start"), enabled = something && collect !is Work.Running && free !is Work.Running) { Text("Start") }
                            if (card is CardView.Open || card is CardView.Unreadable)
                                OutlinedButton(openStorage, Modifier.testTag("eject"), enabled = collect !is Work.Running && free !is Work.Running) { Text("Eject the card…") }
                        }
                        (card as? CardView.Open)?.takeIf { it.done.isNotEmpty() }?.let { c ->
                            var freeing by remember(c.tree) { mutableStateOf(false) }     // asked of this card only: gone with it
                            val what = "${c.done.size} clip${if (c.done.size == 1) "" else "s"} (${Format.bytes(c.doneBytes)})"
                            Dim("on the card, $what went up to the server and can go", Modifier.testTag("free-note"))
                            when {
                                !c.writable -> c.request?.let { r ->
                                    OutlinedButton({ openCard(r) }, Modifier.testTag("allow-changes")) { Text("Allow changes to the card") }
                                }
                                freeing -> Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                    Text("Delete $what from the card? They went up to the server; the phone kept no copy.", Modifier.testTag("free-ask"))
                                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                        Button({ freeing = false; g.session.freeCard(c) }, Modifier.testTag("free-yes"),
                                            colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error)) { Text("Delete") }
                                        TextButton({ freeing = false }, Modifier.testTag("free-no")) { Text("Cancel") }
                                    }
                                }
                                else -> OutlinedButton({ freeing = true }, Modifier.testTag("free"), enabled = free !is Work.Running && collect !is Work.Running) { Text("Free the card…") }
                            }
                        }
                        Progress(free, Modifier.testTag("free-status"))
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            OutlinedButton(pickLogs, Modifier.testTag("add-log")) { Text("Add a saved log") }
                            if (quad is QuadView.Saved)
                                OutlinedButton({ g.session.eraseQuad() }, Modifier.testTag("erase"), enabled = erase !is Work.Running) { Text("Erase the quad's flash") }
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
                        Button({ focus.clearFocus(); beforeJob(); g.session.startUpload() }, Modifier.testTag("upload"), enabled = upload !is Work.Running) { Text("Upload") }
                    }
                } else if (upload is Work.Finished) item { Section("Upload") { Progress(upload, Modifier.testTag("upload-status")) } }
                if (sent.isNotEmpty()) item {
                    Section("With the server") {
                        sent.forEach { b ->
                            Column(Modifier.fillMaxWidth().testTag("sent-${b.id}")) {
                                Text(b.session, fontWeight = FontWeight.Medium)
                                Text(Format.status(b.server, b.session), Modifier.testTag("status-${b.id}"), color = if (b.server?.status == "failed") MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
                                    style = MaterialTheme.typography.bodySmall)
                                if (b.stage == Stage.COMMITTED) Dim("${Format.contents(b.items)} kept on the phone until the server is done", Modifier.testTag("kept-${b.id}"))
                                if (b.server?.status == "failed") TextButton({ g.session.retry(b.id) }, Modifier.testTag("retry-${b.id}")) { Text("Try again on the server") }
                            }
                        }
                        TextButton({ g.session.askServer() }, Modifier.testTag("check")) { Text("Check now") }
                    }
                }
            }
        }
        linked?.let { code ->
            // the server pairing would take, read the one way pairing reads it; a code that would not pair says why
            val read = remember(code) { runCatching { Pairing.parse(code) } }
            AlertDialog(onDismissRequest = { g.session.pairRequest.value = null }, title = { Text("Pair with this server?") },
                text = { Text(read.fold({ it.url }, { it.message ?: "this is not a pairing code" }), Modifier.testTag("pair-host")) },
                confirmButton = { TextButton({ g.session.pair(code); g.session.pairRequest.value = null }, Modifier.testTag("pair-yes"), enabled = read.isSuccess) { Text("Pair") } },
                dismissButton = { TextButton({ g.session.pairRequest.value = null }) { Text("No") } })
        }
    }
}

const val ALARM_EVERY_MS = 2000L

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
            Text(work.what + if (work.total > 0) " · ${Format.bytes(work.done)} of ${Format.bytes(work.total)}" else "", modifier, style = MaterialTheme.typography.bodySmall)
            if (work.total > 0) LinearProgressIndicator({ work.done.toFloat() / work.total }, Modifier.fillMaxWidth())
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
                Dim(Format.contents(b.items), Modifier.testTag("contents-${b.id}"))
            }
            TextButton(onEdit, Modifier.testTag("edit-${b.id}")) { Text("Change") }
        }
        b.items.forEach { i ->
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(i.name, style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.MiddleEllipsis)
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
