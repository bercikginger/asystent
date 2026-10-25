plugins {
    id("com.android.application")
}

android {
    namespace = "pl.asystent"
    compileSdk = 35

    defaultConfig {
        applicationId = "pl.asystent"
        minSdk = 26
        targetSdk = 35
        versionCode = 3
        versionName = "1.2"
    }

    // Interfejs (ten sam co na serwerze) jest wbudowany w apkę.
    sourceSets {
        getByName("main") {
            assets.srcDirs("../../server/static")
        }
    }

    buildTypes {
        release { isMinifyEnabled = false }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("androidx.webkit:webkit:1.12.1")
}
