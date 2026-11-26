package org.acroscope.companion.core

/** What an unattended upload does after a run: nothing more, the same again when the phone next allows it, or
 *  nothing until the pilot has done something. */
enum class Next { DONE, LATER, PILOT }

/**
 * Uploading by itself: the decisions of it, apart from the scheduling (which is the phone's). A run is wanted when
 * the pilot switched it on, the app is paired, and a batch waits. What cannot be helped by trying again (a revoked
 * token, a batch id already used, a file gone from the phone) is left for the pilot; everything else is tried again
 * later, and what went up meanwhile stays up.
 */
object Auto {
    fun next(result: UploadResult): Next = when (result) {
        is UploadResult.Done -> Next.DONE
        UploadResult.Stopped, is UploadResult.Unreachable -> Next.LATER
        // busy, a piece that stalled on the way, too many at once, no room (507), a bad moment
        is UploadResult.Refused -> if (result.status in setOf(408, 423, 429) || result.status >= 500) Next.LATER else Next.PILOT
        UploadResult.NotPaired, is UploadResult.Broken -> Next.PILOT
    }

    private fun on(state: State) = state.settings.autoUpload && state.settings.pairing != null

    /** Something waits to go up. */
    fun wanted(state: State): Boolean = on(state) && state.pending.any { it.items.isNotEmpty() }

    /** Something was handed in and the server is not finished with it: the phone's copy waits for its "done",
     *  whether a press of Upload sent it or the app by itself. */
    fun following(state: State): Boolean =
        state.settings.pairing != null && state.batches.any { it.stage == Stage.COMMITTED && it.server?.status != "failed" }
}
