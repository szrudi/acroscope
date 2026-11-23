pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode = RepositoriesMode.FAIL_ON_PROJECT_REPOS
    repositories {
        google()
        // Robolectric's Android runtime (220 MB) from Maven Central's mirror at Google: Central itself answers
        // 429 Too Many Requests to that one file after a few fetches from an address
        maven("https://maven-central.storage-download.googleapis.com/maven2/") {
            content { includeModule("org.robolectric", "android-all-instrumented") }
        }
        mavenCentral {
            content { excludeModule("org.robolectric", "android-all-instrumented") }
        }
    }
}
rootProject.name = "acroscope-companion"
include(":core", ":app")
