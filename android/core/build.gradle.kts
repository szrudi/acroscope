// The app's logic without Android: what is tested on any machine with a JDK.
plugins {
    `java-test-fixtures`                        // the stand-in flight controller, for the app's tests too
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

kotlin {
    jvmToolchain(21)
}

dependencies {
    implementation(libs.coroutines.core)
    implementation(libs.serialization.json)
    implementation(libs.okhttp)
    testImplementation(libs.kotlin.test)
    testImplementation(libs.junit)
    testImplementation(libs.coroutines.test)
}

tasks.test {
    // DropContractTest runs the server from this checkout: its code and the Python named to run it are inputs, so a
    // change on the server's side runs the tests again instead of finding them up to date or in the build cache
    inputs.files(fileTree("../../acroscope") { exclude("**/__pycache__/**") }).withPropertyName("server").withPathSensitivity(PathSensitivity.RELATIVE)
    inputs.property("python", providers.environmentVariable("ACROSCOPE_PYTHON").orElse(""))
    testLogging {
        events("failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
}
