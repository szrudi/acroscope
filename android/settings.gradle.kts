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
        // Maven Central through its mirror at Google first, Central itself behind it: Central answers 429 Too Many
        // Requests to big files (Robolectric's runtimes, hundreds of MB) from an address that fetches a lot, and a
        // CI runner's address is one of those
        maven("https://maven-central.storage-download.googleapis.com/maven2/") { name = "MavenCentralMirror" }
        mavenCentral()
    }
}
rootProject.name = "acroscope-companion"
include(":core", ":app")
