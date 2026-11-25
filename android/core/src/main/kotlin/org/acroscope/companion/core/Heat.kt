package org.acroscope.companion.core

/** How warm a quad may get on the bench: on USB it has power and no air over it. */
object Heat {
    /** The core temperature, in °C, from which the pilot is told to unplug the quad. */
    const val LIMIT = 65

    fun hot(temp: Int?): Boolean = temp != null && temp >= LIMIT
}
