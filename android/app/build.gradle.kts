plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "org.acroscope.companion"
    compileSdk {
        version = release(37) { minorApiLevel = 1 }      // Compose 1.13 asks for 37.1
    }

    defaultConfig {
        applicationId = "org.acroscope.companion"
        minSdk = 33
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }
    signingConfigs {
        // the debug key, in the repo on purpose: a debug build from any machine or from CI then updates the one on
        // the phone instead of asking for an uninstall, which would take the pending batches with it. Not a secret.
        getByName("debug") {
            storeFile = rootProject.file("debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }
    buildTypes {
        debug {
            applicationIdSuffix = ".debug"               // beside a release build, never over it: their keys differ
            versionNameSuffix = "-debug"
        }
    }
    buildFeatures {
        compose = true
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    testOptions {
        unitTests {
            isIncludeAndroidResources = true
        }
    }
}

// The Android runtime the unit tests run in (Robolectric), fetched by Gradle like any dependency: Robolectric's own
// download of these 220 MB at test time hung for 28 minutes and failed here, and a cached, pinned jar is the better
// arrangement anyway.
val robolectricRuntime = configurations.create("robolectricRuntime") { isTransitive = false }
val robolectricJars = tasks.register<Copy>("robolectricJars") {
    from(robolectricRuntime)
    into(layout.buildDirectory.dir("robolectric"))
}
tasks.withType<Test>().configureEach {
    dependsOn(robolectricJars)
    maxHeapSize = "1g"
    maxParallelForks = 1
    systemProperty("robolectric.offline", "true")
    systemProperty("robolectric.dependency.dir", layout.buildDirectory.dir("robolectric").get().asFile.path)
    testLogging {
        events("failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
}

dependencies {
    robolectricRuntime(libs.robolectric.android)
    implementation(project(":core"))
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.material3)
    implementation(libs.activity.compose)
    implementation(libs.core.ktx)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.work.runtime)
    implementation(libs.coroutines.android)

    testImplementation(libs.junit)
    testImplementation(libs.kotlin.test)
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.core)
    testImplementation(libs.work.testing)
    testImplementation(libs.coroutines.test)
    testImplementation(platform(libs.compose.bom))
    testImplementation(libs.compose.ui.test.junit4)
    debugImplementation(platform(libs.compose.bom))
    debugImplementation(libs.compose.ui.test.manifest)
}
