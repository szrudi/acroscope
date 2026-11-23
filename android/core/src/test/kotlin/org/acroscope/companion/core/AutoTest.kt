package org.acroscope.companion.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class AutoTest {
    private val paired = Settings(url = "https://drop.example.org", token = "t".repeat(43), autoUpload = true)
    private fun batch(stage: Stage, status: String? = null, items: Int = 1) =
        Batch("20261212-090507-ab12cd", "park", "2026-12-12", "", List(items) { Item("VID0000$it.mov", 10, Kind.CLIP, "") }, stage, status?.let { ServerStatus(it) })

    @Test
    fun `what an unattended run does next`() {
        val cases = listOf(
            UploadResult.Done(2) to Next.DONE,
            UploadResult.Done(0) to Next.DONE,
            UploadResult.Stopped to Next.LATER,                                 // the phone ended the run: what went up stays, the rest goes next time
            UploadResult.Unreachable("no route to host") to Next.LATER,         // not at home
            UploadResult.Refused(507, "the drop is full") to Next.LATER,        // no room there right now
            UploadResult.Refused(423, "another upload is writing it") to Next.LATER,
            UploadResult.Refused(500, "boom") to Next.LATER,                    // a server on a bad day
            UploadResult.Refused(502, "Bad Gateway") to Next.LATER,
            UploadResult.Refused(401, "this door takes a paired device's token") to Next.PILOT,   // revoked: nothing an unattended run can do
            UploadResult.Refused(409, "a batch by that id was already handed in") to Next.PILOT,
            UploadResult.Refused(400, "not a plain file name") to Next.PILOT,
            UploadResult.NotPaired to Next.PILOT,
            UploadResult.Broken("VID00001.mov is not on the phone as it was collected") to Next.PILOT,
        )
        for ((result, next) in cases) assertEquals(next, Auto.next(result), result.toString())
    }

    @Test
    fun `an unattended run is wanted when it is switched on, paired, and something waits`() {
        assertTrue(Auto.wanted(State(paired, listOf(batch(Stage.PENDING)))))
        assertFalse(Auto.wanted(State(paired.copy(autoUpload = false), listOf(batch(Stage.PENDING)))))
        assertFalse(Auto.wanted(State(paired.copy(token = null), listOf(batch(Stage.PENDING)))))
        assertFalse(Auto.wanted(State(paired, emptyList())))
        assertFalse(Auto.wanted(State(paired, listOf(batch(Stage.PENDING, items = 0)))))    // nothing in it: nothing to send
        assertFalse(Auto.wanted(State(paired, listOf(batch(Stage.DONE, "done")))))
        // handed in and not finished on the server: there is something to follow (the phone's copy waits for "done")
        assertFalse(Auto.wanted(State(paired, listOf(batch(Stage.COMMITTED, "pending")))))
        assertTrue(Auto.following(State(paired, listOf(batch(Stage.COMMITTED, "pending")))))
        assertTrue(Auto.following(State(paired, listOf(batch(Stage.COMMITTED, "running")))))
        assertTrue(Auto.following(State(paired, listOf(batch(Stage.COMMITTED, null)))))
        assertFalse(Auto.following(State(paired, listOf(batch(Stage.COMMITTED, "failed")))))    // that one waits for the pilot
        assertFalse(Auto.following(State(paired, listOf(batch(Stage.DONE, "done")))))
        assertFalse(Auto.following(State(paired.copy(autoUpload = false), listOf(batch(Stage.COMMITTED, "pending")))))
        assertFalse(Auto.following(State(paired, listOf(batch(Stage.PENDING)))))
    }
}
