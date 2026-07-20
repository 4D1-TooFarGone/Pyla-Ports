package dev.pyla.app

import java.io.File
import java.io.FileOutputStream
import java.net.URL

class ShellUserService : IShellService.Stub() {

    override fun execute(command: String): String {
        return try {
            val proc = Runtime.getRuntime().exec(arrayOf("sh", "-c", command))
            val out = proc.inputStream.readBytes()
            val err = proc.errorStream.readBytes()
            proc.waitFor()
            buildString {
                if (out.isNotEmpty()) append(out.decodeToString())
                if (err.isNotEmpty()) append("STDERR: ").append(err.decodeToString())
            }.trimEnd()
        } catch (e: Exception) {
            "ERROR: ${e.message}"
        }
    }

    override fun download(url: String, destPath: String): String {
        return try {
            val file = File(destPath)
            file.parentFile?.mkdirs()
            URL(url).openStream().use { input ->
                FileOutputStream(file).use { output -> input.copyTo(output) }
            }
            "OK"
        } catch (e: Exception) {
            "ERROR: ${e.message}"
        }
    }

    override fun writeFileChunk(destPath: String, data: ByteArray, overwrite: Boolean): String {
        return try {
            val file = File(destPath)
            file.parentFile?.mkdirs()
            FileOutputStream(file, !overwrite).use { it.write(data) }
            "OK"
        } catch (e: Exception) {
            "ERROR: ${e.message}"
        }
    }

    override fun destroy() {
        System.exit(0)
    }
}
