package org.acroscope.companion

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Environment
import android.os.storage.StorageManager
import android.provider.DocumentsContract
import org.acroscope.companion.core.Card
import org.acroscope.companion.core.CardFile
import java.io.FileNotFoundException
import java.io.IOException
import java.io.InputStream

/** The DVR's card as the phone has it: none plugged in, one the app may not read yet, or one it may. */
sealed interface CardState {
    data object None : CardState
    /** A card is there; `request` asks the pilot for its folder (once per card). */
    data class NeedsAccess(val label: String, val request: Intent) : CardState
    /** `writable`: the pilot allowed changes too, which deleting the clips the server has needs; `request` asks again. */
    data class Ready(val label: String, val tree: Uri, val writable: Boolean = false, val request: Intent? = null) : CardState
}

interface Cards {
    fun state(): CardState
    /** Keep the access the pilot just gave to a card's folder. */
    fun grant(tree: Uri)
    fun open(tree: Uri): Card
}

/**
 * Cards through Android's storage access framework: the goggles' card sits in a USB reader, Android mounts it as a
 * removable volume, and the pilot allows its folder once (the grant is kept per card and found again by the
 * volume's id). The card is only ever read, but for the clips the server reported done, which the pilot may delete.
 */
class SafCards(private val context: Context) : Cards {
    private val storage = context.getSystemService(StorageManager::class.java)

    override fun state(): CardState {
        val volume = storage.storageVolumes.firstOrNull { it.isRemovable && it.state == Environment.MEDIA_MOUNTED } ?: return CardState.None
        val label = volume.getDescription(context) ?: "card"
        val grants = context.contentResolver.persistedUriPermissions.filter { p ->
            p.isReadPermission && volume.uuid != null && DocumentsContract.getTreeDocumentId(p.uri).startsWith(volume.uuid + ":")
        }
        // the card's own folder (its root or DCIM) before another one picked by mistake, then the grant with changes
        fun ownFolder(p: android.content.UriPermission) = DocumentsContract.getTreeDocumentId(p.uri).substringAfter(':').let { it.isEmpty() || it.equals("DCIM", ignoreCase = true) }
        val granted = grants.sortedWith(compareBy({ !ownFolder(it) }, { !it.isWritePermission })).firstOrNull()
        val ask = volume.createOpenDocumentTreeIntent()
        return if (granted != null) CardState.Ready(label, granted.uri, granted.isWritePermission, ask) else CardState.NeedsAccess(label, ask)
    }

    override fun grant(tree: Uri) {
        val both = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
        try {
            context.contentResolver.takePersistableUriPermission(tree, both)
        } catch (e: SecurityException) {                    // a picker that gave reading only: that much is kept
            context.contentResolver.takePersistableUriPermission(tree, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
    }

    override fun open(tree: Uri): Card = SafCard(context, tree)
}

/** A card's DCIM folder under a granted tree; the tree itself when it has no DCIM (the pilot allowed that folder). */
class SafCard(private val context: Context, private val tree: Uri) : Card {
    override val label: String get() = DocumentsContract.getTreeDocumentId(tree)

    private class Entry(val id: String, val name: String, val dir: Boolean, val size: Long, val modified: Long)

    private fun children(parent: String): List<Entry> {
        val uri = DocumentsContract.buildChildDocumentsUriUsingTree(tree, parent)
        val columns = arrayOf(DocumentsContract.Document.COLUMN_DOCUMENT_ID, DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_MIME_TYPE, DocumentsContract.Document.COLUMN_SIZE, DocumentsContract.Document.COLUMN_LAST_MODIFIED)
        val out = mutableListOf<Entry>()
        context.contentResolver.query(uri, columns, null, null, null)?.use { c ->
            while (c.moveToNext())
                out += Entry(c.getString(0), c.getString(1) ?: "", c.getString(2) == DocumentsContract.Document.MIME_TYPE_DIR, c.getLong(3), c.getLong(4))
        } ?: throw FileNotFoundException("the card does not answer")
        return out
    }

    override fun files(): List<CardFile> {
        val root = children(DocumentsContract.getTreeDocumentId(tree))
        val dcim = root.firstOrNull { it.dir && it.name.equals("DCIM", ignoreCase = true) }
        return (if (dcim != null) children(dcim.id) else root).filter { !it.dir }.map { e ->
            object : CardFile {
                override val name = e.name
                override val size = e.size
                override val modified = e.modified
                override fun open(): InputStream =
                    context.contentResolver.openInputStream(DocumentsContract.buildDocumentUriUsingTree(tree, e.id)) ?: throw FileNotFoundException(e.name)
                override fun delete() {
                    val gone = try {
                        DocumentsContract.deleteDocument(context.contentResolver, DocumentsContract.buildDocumentUriUsingTree(tree, e.id))
                    } catch (x: Exception) {
                        throw IOException(x.message ?: "the card would not let it go", x)
                    }
                    if (!gone) throw IOException("the card would not let it go")
                }
            }
        }
    }
}
