package org.acroscope.companion.core

import java.util.Locale

/** How the app says sizes, a batch's contents and the server's word on a batch. */
object Format {
    fun bytes(n: Long): String {
        var v = n.toDouble()
        for (unit in listOf("B", "kB", "MB", "GB")) {
            if (v < 999.5 || unit == "GB")
                return if (unit == "B" || v >= 9.95) String.format(Locale.ROOT, "%.0f %s", v, unit) else String.format(Locale.ROOT, "%.1f %s", v, unit)
            v /= 1000
        }
        error("unreachable")
    }

    private fun count(n: Int, one: String) = "$n $one${if (n == 1) "" else "s"}"

    fun contents(items: List<Item>): String {
        if (items.isEmpty()) return "nothing yet"
        val clips = items.count { it.kind == Kind.CLIP }
        val logs = items.count { it.kind == Kind.LOG }
        return (listOfNotNull(if (clips > 0) count(clips, "clip") else null, if (logs > 0) count(logs, "log") else null) + bytes(items.sumOf { it.size })).joinToString(", ")
    }

    fun status(s: ServerStatus?): String = when (s?.status) {
        null -> "handed in"
        "pending" -> "handed in, waiting its turn"
        "running" -> "being ingested"
        "failed" -> "failed: ${s.error ?: "no reason given"}"
        "done" -> {
            val what = listOfNotNull(if (s.clips.isNotEmpty()) count(s.clips.size, "clip") else null, if (s.logs.isNotEmpty()) count(s.logs.size, "log") else null).joinToString(", ")
            if (what.isEmpty()) "done" else "done: $what" + (s.session?.let { " in $it" } ?: "")
        }
        else -> s.status
    }
}
