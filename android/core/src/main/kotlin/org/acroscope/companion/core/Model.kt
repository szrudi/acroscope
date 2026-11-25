package org.acroscope.companion.core

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

@Serializable
enum class Kind { CLIP, LOG }

/** A file of a batch, as it lies in the batch's folder on the phone and goes up under that name. */
@Serializable
data class Item(
    val name: String,
    val size: Long,
    val kind: Kind,
    val collectedAt: String,
    /** A clip without its index: the DVR lost power while recording it (see [Mp4]). */
    val unfinalised: Boolean = false,
)

/** Where a batch is: on the phone (perhaps partly uploaded), handed to the server with the phone's copy kept until
 *  the server reports it done, or done with the copy deleted. */
@Serializable
enum class Stage { PENDING, COMMITTED, DONE }

/** What the server last said about a batch it was handed (`GET /drop/batches`). */
@Serializable
data class ServerStatus(
    val status: String,
    val session: String? = null,
    val error: String? = null,
    val clips: List<String> = emptyList(),
    val logs: List<String> = emptyList(),
)

@Serializable
data class Batch(
    val id: String,
    /** What the pilot called where they flew: the session's name after its date. */
    val event: String,
    /** The day it is filed under (YYYY-MM-DD): the day of the first collect unless changed. */
    val date: String,
    /** When it was started, local time with its offset: the sidecar's `imported_at`. */
    val createdAt: String,
    val items: List<Item> = emptyList(),
    val stage: Stage = Stage.PENDING,
    val server: ServerStatus? = null,
    /** Being handed in: nothing more is collected into it. Set before the commit is asked for and never lifted, since
     *  after a commit whose answer was lost the server may have it as it was then. */
    val closed: Boolean = false,
) {
    val bytes: Long get() = items.sumOf { it.size }
    val session: String get() = Names.session(date, event)

    /** `import.json` as ingest reads it: the date and the session always said, since the upload may be days later. */
    fun sidecar(host: String?): String = Json.encodeToString(buildJsonObject {
        put("date", date)
        if (event.isNotBlank()) put("session", event)
        put("imported_at", createdAt)
        if (!host.isNullOrBlank()) put("host", host)
    })
}

/** A file on a card that was collected already: the card is not wiped between collects, so it is skipped next time. */
@Serializable
data class Seen(val name: String, val size: Long, val modified: Long)

@Serializable
data class Settings(
    val url: String? = null,
    val token: String? = null,
    /** The name this device was paired under, as the server knows it. */
    val device: String? = null,
    /** The event new collects go to. */
    val event: String = "",
    val recentEvents: List<String> = emptyList(),
    val autoUpload: Boolean = false,
) {
    val pairing: Pairing? get() = if (url != null && token != null) Pairing(url, token) else null
}

@Serializable
data class State(
    val settings: Settings = Settings(),
    val batches: List<Batch> = emptyList(),
    val seen: List<Seen> = emptyList(),
) {
    fun batch(id: String): Batch? = batches.firstOrNull { it.id == id }
    val pending: List<Batch> get() = batches.filter { it.stage == Stage.PENDING }.sortedBy { it.id }}
