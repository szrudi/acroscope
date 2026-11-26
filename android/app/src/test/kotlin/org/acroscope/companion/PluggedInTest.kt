package org.acroscope.companion

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbManager
import androidx.test.core.app.ApplicationProvider
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.xmlpull.v1.XmlPullParser
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * What Android offers the app for when it is plugged in ("Choose an app for the USB device"): the manifest's device
 * filter, read as installed and matched the way Android matches one.
 */
@RunWith(RobolectricTestRunner::class)
class PluggedInTest {
    private val context: Context = ApplicationProvider.getApplicationContext()

    /** A line of the filter; what it leaves out matches anything. */
    private data class Wanted(val vendor: Int?, val product: Int?, val kind: Int?, val subclass: Int?)

    /** A class and a subclass, as a device or one of its interfaces says it. */
    private data class Kind(val kind: Int, val subclass: Int = 0)

    /** A USB device as it names itself: its ids, its own class and the classes of its interfaces. */
    private data class Plugged(val vendor: Int, val product: Int, val kind: Kind, val faces: List<Kind>)

    private fun wanted(): List<Wanted> {
        val main = ComponentName(context, MainActivity::class.java)
        val meta = context.packageManager.getActivityInfo(main, PackageManager.GET_META_DATA).metaData
        val xml = context.resources.getXml(meta.getInt(UsbManager.ACTION_USB_DEVICE_ATTACHED))
        val out = mutableListOf<Wanted>()
        while (xml.next() != XmlPullParser.END_DOCUMENT) {
            if (xml.eventType != XmlPullParser.START_TAG || xml.name != "usb-device") continue
            fun number(name: String) = xml.getAttributeValue(null, name)?.toInt()
            out += Wanted(number("vendor-id"), number("product-id"), number("class"), number("subclass"))
        }
        return out
    }

    /** Android's rule: the ids when the line names them, then the device's class and subclass or any of its interfaces'. */
    private fun offered(d: Plugged): Boolean = wanted().any { f ->
        fun says(k: Kind) = (f.kind == null || f.kind == k.kind) && (f.subclass == null || f.subclass == k.subclass)
        (f.vendor == null || f.vendor == d.vendor) && (f.product == null || f.product == d.product) &&
            (says(d.kind) || d.faces.any(::says))
    }

    private val reader = Plugged(0x1234, 0x5678, Kind(UsbConstants.USB_CLASS_PER_INTERFACE), listOf(Kind(UsbConstants.USB_CLASS_MASS_STORAGE, 6)))
    private val acm = Kind(UsbConstants.USB_CLASS_COMM, 2)
    private val data = Kind(UsbConstants.USB_CLASS_CDC_DATA)
    private val serial = listOf(acm, data)

    @Test
    fun `the app is one of those Android starts for a USB device`() {
        val asked = Intent(UsbManager.ACTION_USB_DEVICE_ATTACHED).setPackage(context.packageName)
        val found = context.packageManager.queryIntentActivities(asked, 0).map { it.activityInfo.name }
        assertEquals(listOf(MainActivity::class.java.name), found)
    }

    @Test
    fun `it is offered when the card's reader is plugged in`() {
        assertTrue(offered(reader))
    }

    @Test
    fun `and when a flight controller is, whatever class the board says it is`() {
        assertTrue(offered(Plugged(0x1234, 0x5678, acm, serial)))
        assertTrue(offered(Plugged(0x1234, 0x5678, Kind(UsbConstants.USB_CLASS_COMM), serial)))   // the device says only the class: an interface says ACM
        assertTrue(offered(Plugged(0x1234, 0x5678, Kind(UsbConstants.USB_CLASS_MISC, 2), serial)))  // a composite device: an interface says it
        val dataOnly = listOf(data)                                                                // nothing says it: known by its ids
        assertTrue(offered(Plugged(0x0483, 0x5740, Kind(UsbConstants.USB_CLASS_MISC, 2), dataOnly)))   // ST's virtual COM port
        assertTrue(offered(Plugged(0x2E3C, 0x5740, Kind(UsbConstants.USB_CLASS_MISC, 2), dataOnly)))   // Artery's
        assertFalse(offered(Plugged(0x0483, 0x1234, Kind(UsbConstants.USB_CLASS_MISC, 2), dataOnly)))  // not everything ST makes
    }

    @Test
    fun `not for a hub's network adapter, though it speaks the communications class`() {
        val ncm = listOf(Kind(UsbConstants.USB_CLASS_COMM, 0x0D), data)                            // ASIX's AX88179A in its NCM mode
        assertFalse(offered(Plugged(0x0B95, 0x1790, Kind(UsbConstants.USB_CLASS_PER_INTERFACE), ncm)))
        assertFalse(offered(Plugged(0x0B95, 0x1790, Kind(UsbConstants.USB_CLASS_COMM), ncm)))
        val ecm = listOf(Kind(UsbConstants.USB_CLASS_COMM, 0x06), data)
        assertFalse(offered(Plugged(0x1234, 0x5678, Kind(UsbConstants.USB_CLASS_PER_INTERFACE), ecm)))
    }

    @Test
    fun `but not for what it has no use for`() {
        assertFalse(offered(Plugged(0x1234, 0x5678, Kind(UsbConstants.USB_CLASS_PER_INTERFACE), listOf(Kind(UsbConstants.USB_CLASS_HID, 1)))))   // a keyboard
        assertFalse(offered(Plugged(0x1234, 0x5678, Kind(UsbConstants.USB_CLASS_HUB), listOf(Kind(UsbConstants.USB_CLASS_HUB)))))
        assertFalse(offered(Plugged(0x1234, 0x5678, Kind(UsbConstants.USB_CLASS_PER_INTERFACE), listOf(Kind(UsbConstants.USB_CLASS_AUDIO, 1)))))  // a headset
    }
}
