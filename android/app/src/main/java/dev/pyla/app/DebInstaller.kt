package dev.pyla.app

import android.content.res.AssetManager
import org.apache.commons.compress.archivers.ar.ArArchiveInputStream
import org.apache.commons.compress.archivers.tar.TarArchiveInputStream
import org.apache.commons.compress.compressors.gzip.GzipCompressorInputStream
import org.apache.commons.compress.compressors.xz.XZCompressorInputStream
import org.apache.commons.compress.compressors.zstandard.ZstdCompressorInputStream
import org.json.JSONObject
import java.io.InputStream

class DebInstaller(private val svc: IShellService, private val assets: AssetManager) {

    companion object {
        const val INSTALL_ROOT = "/data/local/tmp/pylaenv"
        const val MARKER       = "$INSTALL_ROOT/.pyla_installed"
        const val PIP_MARKER   = "$INSTALL_ROOT/.pyla_pip_installed"

        private const val ASSET_DIR      = "termux_debs"
        private const val CHUNK_SIZE     = 512 * 1024
        private const val TERMUX_PREFIX  = "/data/data/com.termux/files/"

        val PIP_PACKAGES = listOf(

            "opencv-python-headless",
            "aiohttp", "multidict", "yarl", "frozenlist", "aiosignal",
            "pycryptodome", "pandas",
            "python-Levenshtein", "rapidfuzz",

            "Flask", "requests", "toml", "packaging",
            "discord.py", "adbutils", "easyocr", "scrcpy-client",

            "Werkzeug", "Jinja2", "itsdangerous", "click", "blinker", "MarkupSafe",

            "urllib3", "charset-normalizer", "idna", "certifi",

            "typing-extensions", "async-timeout",
            "whichcraft", "retry", "decorator", "deprecation", "setuptools",
            "PyYAML", "python-bidi", "six",
            "imageio", "tqdm", "prettytable", "ninja",
        )

        fun buildEnv(): String =
            "HOME=$INSTALL_ROOT/usr " +
            "PREFIX=$INSTALL_ROOT/usr " +
            "PATH=$INSTALL_ROOT/usr/bin:$INSTALL_ROOT/usr/sbin:/system/bin " +
            "LD_LIBRARY_PATH=$INSTALL_ROOT/usr/lib " +
            "PYTHONHOME=$INSTALL_ROOT/usr " +
            "SSL_CERT_FILE=$INSTALL_ROOT/usr/etc/tls/cert.pem " +
            "SSL_CERT_DIR=$INSTALL_ROOT/usr/etc/tls/certs " +
            "OPENSSL_DIR=$INSTALL_ROOT/usr " +
            "TMPDIR=/data/local/tmp"
    }

    fun isInstalled(): Boolean =
        svc.execute("[ -f $MARKER ] && echo yes || echo no").trim() == "yes"

    fun isPipInstalled(): Boolean {

        if (svc.execute("[ -f $PIP_MARKER ] && echo yes || echo no").trim() != "yes") return false
        val check = svc.execute(
            "${buildEnv()} $INSTALL_ROOT/usr/bin/python3 -c " +
            "'import pkg_resources, flask, adbutils; print(\"ok\")' 2>&1"
        ).trim()
        return check == "ok"
    }

    fun install(onProgress: (String) -> Unit) {
        val manifest = JSONObject(
            assets.open("$ASSET_DIR/manifest.json").use { it.readBytes().decodeToString() }
        )
        val installOrder = manifest.getJSONArray("install_order")
        val packages     = manifest.getJSONObject("packages")
        val total        = installOrder.length()

        onProgress("Extracting $total packages from APK assets…")
        svc.execute("mkdir -p $INSTALL_ROOT/usr")

        for (i in 0 until total) {
            val pkgName = installOrder.getString(i)
            val debName = packages.optString(pkgName, "")
            if (debName.isEmpty()) { onProgress("  [skip] $pkgName"); continue }

            onProgress("  [${i + 1}/$total] $pkgName")
            try {
                extractDeb(debName, pkgName, onProgress)
            } catch (e: Exception) {
                onProgress("  [fail] $pkgName: ${e.message}")
            }
        }

        onProgress("Fixing permissions on executables…")
        svc.execute("chmod 755 $INSTALL_ROOT/usr/bin/* 2>/dev/null; " +
                    "chmod 755 $INSTALL_ROOT/usr/sbin/* 2>/dev/null; " +
                    "chmod 755 $INSTALL_ROOT/usr/lib/*.so* 2>/dev/null; " +
                    "chmod 755 $INSTALL_ROOT/usr/lib/python3.12/lib-dynload/*.so 2>/dev/null")

        val pyVer = svc.execute("${buildEnv()} $INSTALL_ROOT/usr/bin/python3 --version 2>&1").trim()
        onProgress("Python check: $pyVer")
        if (!pyVer.startsWith("Python")) {
            onProgress("[ERROR] Python not working — check extraction logs above")
        }

        val sslVer = svc.execute(
            "${buildEnv()} $INSTALL_ROOT/usr/bin/python3 -c " +
            "'import ssl; print(\"SSL OK:\", ssl.OPENSSL_VERSION)' 2>&1"
        ).trim()
        onProgress("SSL check: $sslVer")

        val whlList = assets.list("$ASSET_DIR/pip_wheels") ?: emptyArray()
        if (whlList.isNotEmpty()) {
            extractWheelsToSitePackages(whlList, onProgress)
        } else {
            onProgress("[warn] No pre-built wheels in assets — skipping")
        }

        svc.execute("echo done > $MARKER")
        onProgress("All done ✓")
    }

    fun installPipOnly(onProgress: (String) -> Unit) {
        val whlList = assets.list("$ASSET_DIR/pip_wheels") ?: emptyArray()
        if (whlList.isEmpty()) {
            onProgress("[warn] No pre-built wheels in assets"); return
        }
        extractWheelsToSitePackages(whlList, onProgress)
    }

    private fun extractWheelsToSitePackages(whlList: Array<String>, onProgress: (String) -> Unit) {
        val whlDir = "/data/local/tmp/pyla_wheels"
        svc.execute("rm -rf $whlDir && mkdir -p $whlDir")

        onProgress("Copying ${whlList.size} wheels to device…")
        for (whlName in whlList) {
            assets.open("$ASSET_DIR/pip_wheels/$whlName", AssetManager.ACCESS_STREAMING)
                .use { s -> streamToShizuku(s, "$whlDir/$whlName") }
        }

        onProgress("Installing packages via Python zipfile…")
        val pyScript = """
import zipfile, os, sys
print('sys.path:', sys.path)
sp = next((p for p in sys.path if 'site-packages' in p and os.path.isdir(p)), None)
if not sp:
    import site; sp = site.getsitepackages()[0]
print('site-packages:', sp)
os.makedirs(sp, exist_ok=True)
d = '$whlDir'
wheels = sorted(f for f in os.listdir(d) if f.endswith('.whl'))
for w in wheels:
    try:
        zipfile.ZipFile(os.path.join(d, w)).extractall(sp)
        print('ok:', w)
    except Exception as e:
        print('fail:', w, e)
pr = os.path.join(sp, 'pkg_resources')
if not os.path.isdir(pr):
    print('WARNING: pkg_resources dir missing after extraction')
else:
    print('pkg_resources dir OK')
print('done')
""".trimIndent()

        val scriptPath = "/data/local/tmp/pyla_install.py"
        streamToShizuku(pyScript.byteInputStream(), scriptPath)

        val out = svc.execute("${buildEnv()} $INSTALL_ROOT/usr/bin/python3 $scriptPath 2>&1")
        out.lines().forEach { onProgress("  $it") }

        val shimContent = assets.open("pkg_resources_shim.py").use { it.readBytes().decodeToString() }
        val siteDirsResult = svc.execute(
            "${buildEnv()} $INSTALL_ROOT/usr/bin/python3 -c " +
            "'import sys; [print(p) for p in sys.path if \"site-packages\" in p]' 2>/dev/null"
        )
        for (siteDir in siteDirsResult.lines().map { it.trim() }.filter { it.startsWith("/") }) {
            val pkgResDir = "$siteDir/pkg_resources"
            val pkgResInit = "$pkgResDir/__init__.py"
            svc.execute("mkdir -p $pkgResDir")

            val exists = svc.execute("[ -f $pkgResInit ] && echo yes || echo no").trim()
            if (exists != "yes") {
                onProgress("  Installing pkg_resources shim → $siteDir")
                streamToShizuku(shimContent.byteInputStream(), pkgResInit)
            }
        }

        svc.execute("rm -f $scriptPath && rm -rf $whlDir")
        svc.execute("echo done > $PIP_MARKER")
        onProgress("Packages installed ✓")
    }

    private fun extractDeb(debName: String, @Suppress("UNUSED_PARAMETER") pkgName: String, onProgress: (String) -> Unit) {
        var files = 0; var links = 0

        assets.open("$ASSET_DIR/$debName", AssetManager.ACCESS_STREAMING)
            .buffered(128 * 1024)
            .use { debStream ->
                val ar = ArArchiveInputStream(debStream)
                var found = false

                while (true) {
                    @Suppress("DEPRECATION")
                    val arEntry = ar.nextArEntry ?: break
                    if (!arEntry.name.startsWith("data.tar")) { drainStream(ar); continue }

                    found = true
                    val tarRaw: InputStream = when {
                        arEntry.name.endsWith(".xz")  -> XZCompressorInputStream(ar)
                        arEntry.name.endsWith(".gz")  -> GzipCompressorInputStream(ar)
                        arEntry.name.endsWith(".zst") -> ZstdCompressorInputStream(ar)
                        else                          -> ar
                    }
                    val tar = TarArchiveInputStream(tarRaw)
                    val createdDirs = mutableSetOf<String>()

                    while (true) {
                        @Suppress("DEPRECATION")
                        val te = tar.nextTarEntry ?: break

                        val name = te.name
                        val idx  = name.indexOf(TERMUX_PREFIX)
                        if (idx == -1) continue
                        val relPath = name.substring(idx + TERMUX_PREFIX.length)
                        if (relPath.isEmpty()) continue

                        val dest = "$INSTALL_ROOT/$relPath"

                        when {
                            te.isDirectory -> {
                                if (dest !in createdDirs) {
                                    svc.execute("mkdir -p $dest")
                                    createdDirs.add(dest)
                                }
                            }
                            te.isSymbolicLink -> {

                                val target = te.linkName.replace(
                                    "/data/data/com.termux/files", INSTALL_ROOT
                                )
                                ensureParent(dest, createdDirs)
                                svc.execute("ln -sf '$target' '$dest' 2>/dev/null || true")
                                links++
                            }
                            te.isLink -> {

                                val linkSrc = te.linkName.let { t ->
                                    val i = t.indexOf(TERMUX_PREFIX)
                                    if (i != -1) "$INSTALL_ROOT/${t.substring(i + TERMUX_PREFIX.length)}" else t
                                }
                                ensureParent(dest, createdDirs)
                                svc.execute("ln '$linkSrc' '$dest' 2>/dev/null || cp '$linkSrc' '$dest' 2>/dev/null || true")
                                links++
                            }
                            else -> {
                                ensureParent(dest, createdDirs)
                                streamToShizuku(tar, dest)
                                files++
                            }
                        }
                    }
                    break
                }
                if (!found) throw RuntimeException("No data.tar.* in $debName")
            }

        onProgress("    $files files, $links symlinks/hardlinks")
    }

    private fun ensureParent(destPath: String, createdDirs: MutableSet<String>) {
        val parent = destPath.substringBeforeLast("/")
        if (parent.isNotEmpty() && parent !in createdDirs) {
            svc.execute("mkdir -p $parent")
            createdDirs.add(parent)
        }
    }

    private fun streamToShizuku(input: InputStream, destPath: String) {
        val buf   = ByteArray(CHUNK_SIZE)
        var first = true
        while (true) {
            var read = 0
            while (read < CHUNK_SIZE) {
                val n = input.read(buf, read, CHUNK_SIZE - read)
                if (n == -1) break
                read += n
            }
            if (read == 0) break
            svc.writeFileChunk(destPath, if (read == CHUNK_SIZE) buf.clone() else buf.copyOf(read), first)
            first = false
            if (read < CHUNK_SIZE) break
        }
    }

    private fun drainStream(stream: InputStream) {
        val buf = ByteArray(8192)
        @Suppress("ControlFlowWithEmptyBody")
        while (stream.read(buf) != -1) {}
    }
}
