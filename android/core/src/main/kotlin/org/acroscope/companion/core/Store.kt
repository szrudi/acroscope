package org.acroscope.companion.core

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.serialization.json.Json
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * Everything the app remembers, in one JSON file, and the folders the batches' files lie in. A change is written
 * before it is seen: to a new file, synced, then renamed over the old one, so a phone that dies in the middle has
 * either the state before or the state after.
 */
class Store(private val root: File, private val files: File = root) {
    private val file = File(root, "state.json")
    private val json = Json { ignoreUnknownKeys = true; prettyPrint = true }
    private val flow = MutableStateFlow(load())

    val state: StateFlow<State> get() = flow
    val now: State get() = flow.value

    /** The folder a batch's files are kept in until the server has them: under `files`, which may be another
     *  place than the state's (the roomy storage, where the state with its token stays in the app's own). */
    fun dir(batch: String): File {
        require(Names.isBatchId(batch)) { "$batch: not a batch id" }
        return File(File(files, "batches"), batch)
    }

    /** A place for a file on its way into a batch (a log as it comes off the quad). */
    val scratch: File get() = File(files, "tmp")

    /** Bytes free where the batches are kept. */
    val free: Long get() = generateSequence(files) { it.parentFile }.first { it.exists() }.usableSpace

    @Synchronized
    fun update(change: (State) -> State): State {
        val next = change(flow.value)
        if (next != flow.value) {
            root.mkdirs()
            val tmp = File(root, "state.json.new")
            tmp.outputStream().use { out ->
                out.write(json.encodeToString(State.serializer(), next).toByteArray())
                out.fd.sync()
            }
            Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            flow.value = next
        }
        return next
    }

    fun updateBatch(id: String, change: (Batch) -> Batch): State =
        update { s -> s.copy(batches = s.batches.map { if (it.id == id) change(it) else it }) }

    private fun load(): State =
        if (file.isFile) json.decodeFromString(State.serializer(), file.readText()) else State()
}
