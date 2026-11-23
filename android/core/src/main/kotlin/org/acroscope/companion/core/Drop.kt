package org.acroscope.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.net.URLEncoder
import java.util.Base64
import java.util.concurrent.TimeUnit

/** The server refused: its HTTP status and what it said. 401: this device is not paired (any more). 507: no room
 *  there right now. 409: it does not fit what the server has (a batch id already handed in, another file under
 *  the name). */
class DropException(val status: Int, message: String) : IOException(message)

/** The upload was told to stop between two pieces; what went up stays up. */
class DropStopped : IOException("stopped")

/** Who the server takes this device for, and the room at the drop. */
data class Device(val id: String, val name: String, val quota: Long, val staged: Long, val maxFile: Long)

/** The server's upload door as the app uses it (`/drop/` in the server's README). */
interface Drop {
    fun device(): Device
    fun events(): List<String>
    /** This device's batches as the server has them, by id. */
    fun batches(): Map<String, ServerStatus>
    /** A file of a batch, from wherever the server has it to its end. `progress` hears the bytes that are up. */
    fun upload(batch: String, name: String, file: File, progress: (Long) -> Unit = {}, stop: () -> Boolean = { false })
    /** A small file sent whole, in place of whatever is staged under its name. */
    fun put(batch: String, name: String, bytes: ByteArray)
    fun commit(batch: String, files: Map<String, Long>): ServerStatus
    fun retry(batch: String): ServerStatus
}

/**
 * The drop over HTTP: tus 1.0.0 for the files (create, ask the offset, send the next piece), then a commit. An
 * upload cut off anywhere goes on from the last byte the server has: every call asks the server where the file
 * ends before it sends, so nothing about an upload needs remembering on the phone.
 */
class DropClient(
    private val pairing: Pairing,
    private val http: OkHttpClient = client(),
    /** Bytes in one request. What arrived of a piece cut off is kept by the server, so this only bounds a request. */
    private val piece: Long = 32L shl 20,
    private val pause: (Long) -> Unit = Thread::sleep,
) : Drop {
    companion object {
        private val OCTETS = "application/offset+octet-stream".toMediaType()
        private val JSON = "application/json".toMediaType()
        /** The server lets a silent sender go after 60 s: a file busy for longer than this is held by something else. */
        private const val BUSY_TRIES = 45

        fun client(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS).writeTimeout(60, TimeUnit.SECONDS).readTimeout(90, TimeUnit.SECONDS)
            .retryOnConnectionFailure(false).build()
    }

    private class Answer(val status: Int, val headers: Map<String, String>, val body: String)

    private fun call(method: String, path: String, body: RequestBody? = null, headers: Map<String, String> = emptyMap()): Answer {
        val request = Request.Builder().url(pairing.url + path).method(method, body)
            .header("Authorization", "Bearer ${pairing.token}").header("Tus-Resumable", "1.0.0")
        headers.forEach { (k, v) -> request.header(k, v) }
        http.newCall(request.build()).execute().use { r ->
            return Answer(r.code, r.headers.names().associate { it.lowercase() to r.header(it).orEmpty() }, r.body.string())
        }
    }

    private fun refuse(what: String, a: Answer): Nothing {
        val said = try { Json.parseToJsonElement(a.body).jsonObject["error"]?.jsonPrimitive?.contentOrNull } catch (e: Exception) { null }
        throw DropException(a.status, said ?: "$what: the server answered ${a.status}")
    }

    private fun json(method: String, path: String, body: JsonObject? = null): JsonObject {
        val a = call(method, path, body?.toString()?.toRequestBody(JSON) ?: if (method == "POST") ByteArray(0).toRequestBody(null) else null)
        if (a.status != 200) refuse("$method $path", a)
        return Json.parseToJsonElement(a.body).let { if (it is JsonArray) buildJsonObject { put("list", it) } else it.jsonObject }
    }

    private fun status(o: JsonObject): ServerStatus {
        val result = o["result"] as? JsonObject
        fun names(key: String) = (result?.get(key) as? JsonArray)?.map { it.jsonPrimitive.content } ?: emptyList()
        return ServerStatus(o["status"]!!.jsonPrimitive.content, o["session"]?.jsonPrimitive?.contentOrNull, o["error"]?.jsonPrimitive?.contentOrNull, names("clips"), names("logs"))
    }

    private fun path(batch: String) = "/drop/batches/$batch"

    private fun enc(name: String) = URLEncoder.encode(name, "UTF-8").replace("+", "%20")

    override fun device(): Device = json("GET", "/drop/device").let {
        Device(it["id"]!!.jsonPrimitive.content, it["name"]!!.jsonPrimitive.content, it["quota"]!!.jsonPrimitive.long, it["staged"]!!.jsonPrimitive.long, it["max_file"]!!.jsonPrimitive.long)
    }

    override fun events(): List<String> = json("GET", "/drop/events")["events"]!!.jsonArray.map { it.jsonPrimitive.content }

    override fun batches(): Map<String, ServerStatus> =
        json("GET", "/drop/batches")["list"]!!.jsonArray.associate { it.jsonObject["id"]!!.jsonPrimitive.content to status(it.jsonObject) }

    private fun create(batch: String, name: String, length: Long): String {
        val meta = "filename " + Base64.getEncoder().encodeToString(name.toByteArray())
        val a = call("POST", "${path(batch)}/files", ByteArray(0).toRequestBody(null), mapOf("Upload-Length" to length.toString(), "Upload-Metadata" to meta))
        if (a.status != 201) refuse("declaring $name", a)
        return a.headers["location"] ?: throw DropException(a.status, "declaring $name: the server named no upload")
    }

    private fun offset(upload: String): Long {
        val a = call("HEAD", upload)
        if (a.status != 200) refuse("asking where $upload ends", a)
        return a.headers["upload-offset"]!!.toLong()
    }

    private fun slice(file: File, at: Long, count: Long, sent: (Long) -> Unit) = object : RequestBody() {
        override fun contentType() = OCTETS
        override fun contentLength() = count
        override fun writeTo(sink: BufferedSink) {
            RandomAccessFile(file, "r").use { f ->
                f.seek(at)
                val buf = ByteArray(64 shl 10)
                var left = count
                while (left > 0) {
                    val n = f.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
                    if (n < 0) throw IOException("${file.name} ends before byte ${at + count}")
                    sink.write(buf, 0, n)
                    left -= n
                    sent(at + count - left)
                }
            }
        }
    }

    override fun upload(batch: String, name: String, file: File, progress: (Long) -> Unit, stop: () -> Boolean) {
        val length = file.length()
        val upload = try {
            create(batch, name, length)
        } catch (e: DropException) {
            if (e.status != 409) throw e
            // another file is staged under this name (it was collected again, or changed): that one is void. When the
            // conflict is something else (the batch's id is spent), declaring again says so.
            call("DELETE", "${path(batch)}/files/${enc(name)}")
            create(batch, name, length)
        }
        var at = offset(upload)
        var busy = 0
        progress(at)
        while (at < length) {
            if (stop()) throw DropStopped()
            val a = call("PATCH", upload, slice(file, at, minOf(piece, length - at), progress), mapOf("Upload-Offset" to at.toString()))
            when (a.status) {
                204 -> { at = a.headers["upload-offset"]!!.toLong(); busy = 0 }
                409 -> at = offset(upload)                                    // the server has more or less than was thought: go on from where it is
                423 -> { if (++busy > BUSY_TRIES) refuse("sending $name", a); pause(2000) }   // an earlier piece of ours is still being let go
                else -> refuse("sending $name", a)
            }
            progress(at)
        }
    }

    override fun put(batch: String, name: String, bytes: ByteArray) {
        val gone = call("DELETE", "${path(batch)}/files/${enc(name)}")
        if (gone.status != 204 && gone.status != 404) refuse("replacing $name", gone)
        val upload = create(batch, name, bytes.size.toLong())
        val a = call("PATCH", upload, bytes.toRequestBody(OCTETS), mapOf("Upload-Offset" to "0"))
        if (a.status != 204) refuse("sending $name", a)
    }

    override fun commit(batch: String, files: Map<String, Long>): ServerStatus =
        status(json("POST", "${path(batch)}/commit", buildJsonObject { putJsonObject("files") { files.forEach { (n, s) -> put(n, s) } } }))

    override fun retry(batch: String): ServerStatus = status(json("POST", "${path(batch)}/retry"))
}
