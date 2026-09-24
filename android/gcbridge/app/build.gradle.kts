plugins {
    id("com.android.application")
}

android {
    namespace = "com.controllerlog.gcbridge"
    compileSdk = 37
    buildToolsVersion = "37.0.0"

    defaultConfig {
        applicationId = "com.controllerlog.gcbridge"
        minSdk = 29
        targetSdk = 36
        versionCode = 1
        versionName = "0.1"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    testOptions {
        unitTests.all {
            // Lets the unit test cross-check the init bytes against the Python implementation
            // (skipped when the file isn't there).
            val switch2Py = rootProject.file("../../controllerlog/input/switch2_usb.py")
            it.systemProperty("switch2.py", switch2Py.absolutePath)
            // Declared as an input, so editing the Python file re-runs the tests instead of
            // reusing an up-to-date or cached result (files() also tolerates a missing file).
            it.inputs.files(switch2Py)
                .withPropertyName("switch2Py")
                .withPathSensitivity(PathSensitivity.NONE)
        }
    }

    lint {
        abortOnError = true
        // Debug tool app: no store listing, no translations. targetSdk 36 is deliberate
        // (compileSdk 37 for the newest APIs, behaviour of the release the phone runs).
        disable += setOf("MissingTranslation", "SetTextI18n", "OldTargetApi")
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
}

tasks.withType<JavaCompile>().configureEach {
    options.compilerArgs.addAll(listOf("-Xlint:all", "-Xlint:-options", "-Xlint:-classfile"))
}
