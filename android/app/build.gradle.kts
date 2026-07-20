plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.pyla.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "dev.pyla.app"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"

        ndk { abiFilters += "arm64-v8a" }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
        debug { isDebuggable = true }
    }

    buildFeatures {
        viewBinding = true
        aidl = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions { jvmTarget = "17" }

    packaging {
        resources.excludes += "META-INF/DEPENDENCIES"
        resources.excludes += "META-INF/LICENSE*"
        resources.excludes += "META-INF/NOTICE*"
    }

    androidResources {
        noCompress += "tarpart"
        noCompress += "tflite"
        noCompress += "dat"
    }
}

dependencies {
    implementation("dev.rikka.shizuku:api:13.1.5")
    implementation("dev.rikka.shizuku:provider:13.1.5")
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")

    implementation("org.apache.commons:commons-compress:1.27.1")
    implementation("org.tukaani:xz:1.10")
    implementation("com.github.luben:zstd-jni:1.5.6-6")

    implementation("com.google.ai.edge.litert:litert:1.4.0")
    implementation("com.google.ai.edge.litert:litert-gpu:1.4.0")
}

val syncBotAssets by tasks.registering(Copy::class) {
    from(File(rootProject.projectDir.parentFile, "cfgs_and_internal")) {
        exclude("models/easyocr/**")

        exclude("**/__pycache__/**")
        exclude("**/*.pyc", "**/*.pyd")
        exclude("cfg/match_history.csv")
        exclude("**/.DS_Store")

        exclude("**/*.tflite")
    }
    into(layout.projectDirectory.dir("src/main/assets/pylaai/cfgs_and_internal"))
    duplicatesStrategy = DuplicatesStrategy.INCLUDE
}

tasks.named("preBuild") { dependsOn(syncBotAssets) }
