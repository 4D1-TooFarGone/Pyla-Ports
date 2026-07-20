package dev.pyla.app

import android.content.ComponentName
import android.content.Intent
import android.content.res.AssetManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.provider.Settings
import android.util.Log
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import dev.pyla.app.databinding.ActivityMainBinding
import kotlinx.coroutines.*
import rikka.shizuku.Shizuku
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import android.content.pm.PackageManager

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG                  = "PylaAI"
        private const val SHIZUKU_REQUEST_CODE = 1001
        private val FLASK_PORTS = (5185..5210).toList()

        private const val ENV_ROOT   = "/data/local/tmp/pylaenv"
        private const val PYTHON     = "$ENV_ROOT/usr/bin/python3"
        private const val ENV_MARKER = "$ENV_ROOT/.pyla_env_ready"
        private const val BOT_LOG    = "/data/local/tmp/pyla_bot.log"
        private const val CHUNK_SIZE = 512 * 1024
    }

    private val botDir get() = "${getExternalFilesDir(null)?.absolutePath}/cfgs_and_internal"

    private lateinit var binding: ActivityMainBinding
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    private var setupJob:        Job? = null
    private var logTailJob:      Job? = null
    private var pollingJob:      Job? = null
    private var flaskUrl:        String? = null
    private var logLine          = 0
    private var pendingReinstall = false

    private var shellService: IShellService? = null

    private val userServiceArgs by lazy {
        Shizuku.UserServiceArgs(ComponentName(packageName, ShellUserService::class.java.name))
            .daemon(false).processNameSuffix("shell_service").debuggable(false).version(6)
    }

    private val serviceConnection = object : android.content.ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            shellService = IShellService.Stub.asInterface(binder)
            log("Shizuku connected ✓")
            onShizukuReady()
        }
        override fun onServiceDisconnected(name: ComponentName?) {
            shellService = null
            logTailJob?.cancel(); pollingJob?.cancel()
            log("Shizuku disconnected")
            binding.btnConnectShizuku.isEnabled = true
        }
    }

    private val permResultListener = Shizuku.OnRequestPermissionResultListener { _, result ->
        if (result == PackageManager.PERMISSION_GRANTED) bindShizuku()
        else {
            setSetupStatus("Shizuku permission denied.\nGrant it in the Shizuku app then tap Connect.")
            binding.btnConnectShizuku.isEnabled = true
        }
    }
    private val binderReceived = Shizuku.OnBinderReceivedListener { Log.i(TAG, "binder received") }
    private val binderDead     = Shizuku.OnBinderDeadListener {
        pollingJob?.cancel(); logTailJob?.cancel()
        log("Shizuku stopped. Restart it then press Connect again.")
        binding.btnConnectShizuku.isEnabled = true
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        Shizuku.addBinderReceivedListenerSticky(binderReceived)
        Shizuku.addBinderDeadListener(binderDead)
        Shizuku.addRequestPermissionResultListener(permResultListener)
        showSetupMode()
        startInference()

        runCatching {
            if (Shizuku.pingBinder() &&
                Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED) {
                setSetupStatus("Reconnecting…")
                bindShizuku()
            }
        }
    }

    override fun onResume() {
        super.onResume()

        val svc = shellService ?: return
        scope.launch {
            val running = withContext(Dispatchers.IO) { runCatching { isBotRunning(svc) }.getOrDefault(false) }
            if (!running) return@launch
            showNormalMode(); showConsole()
            if (logTailJob?.isActive != true) startLogTailing(BOT_LOG)
            if (flaskUrl != null) {

                binding.tvStatus.text = "Ready ✓"
                binding.btnOpenBrowser.visibility = View.VISIBLE
            } else if (pollingJob?.isActive != true) {
                startFlaskPolling()
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        setupJob?.cancel(); logTailJob?.cancel(); pollingJob?.cancel()
        scope.cancel()
        runCatching { Shizuku.unbindUserService(userServiceArgs, serviceConnection, true) }
        Shizuku.removeBinderReceivedListener(binderReceived)
        Shizuku.removeBinderDeadListener(binderDead)
        Shizuku.removeRequestPermissionResultListener(permResultListener)
    }

    private fun requestShizuku() {
        binding.btnConnectShizuku.isEnabled = false
        setSetupStatus("Connecting to Shizuku…")
        if (!Shizuku.pingBinder()) {
            setSetupStatus(
                "Shizuku is not running.\n\n" +
                "1. Install Shizuku from Play Store\n" +
                "2. Activate via Wireless Debugging\n" +
                "3. Tap Connect again"
            )
            binding.btnConnectShizuku.isEnabled = true; return
        }
        when (Shizuku.checkSelfPermission()) {
            PackageManager.PERMISSION_GRANTED -> bindShizuku()
            else -> Shizuku.requestPermission(SHIZUKU_REQUEST_CODE)
        }
    }

    private fun bindShizuku() {

        if (shellService != null) { onShizukuReady(); return }
        try { Shizuku.bindUserService(userServiceArgs, serviceConnection) }
        catch (e: Exception) {
            log("Shizuku bind failed: ${e.message}")
            binding.btnConnectShizuku.isEnabled = true
        }
    }

    private fun onShizukuReady() {
        val svc = shellService ?: return
        if (pendingReinstall) {
            pendingReinstall = false
            scope.launch { extractAndLaunch(svc) }
            return
        }
        scope.launch {
            val envReady = withContext(Dispatchers.IO) {
                svc.execute("[ -f $ENV_MARKER ] && [ -x $PYTHON ] && echo yes || echo no").trim() == "yes"
            }
            if (envReady) {
                showNormalMode()
                showConsole()
                val alreadyRunning = withContext(Dispatchers.IO) { isBotRunning(svc) }
                if (alreadyRunning) {
                    log("Bot already running — reconnecting…")
                    startLogTailing(BOT_LOG)
                    startFlaskPolling()
                } else {
                    log("Python env ready. Launching bot…")
                    launchBot()
                }
            } else {
                extractAndLaunch(svc)
            }
        }
    }

    private fun showSetupMode() {
        binding.layoutSetup.visibility  = View.VISIBLE
        binding.layoutNormal.visibility = View.GONE
        binding.btnSetupDone.visibility = View.GONE
        binding.btnConnectShizuku.text  = "Connect & Start"
        setSetupStatus("Tap Connect & Start to launch the bot.\nFirst launch will take a minute to set up.")
        binding.btnConnectShizuku.setOnClickListener { requestShizuku() }
    }

    private fun setSetupStatus(msg: String) = runOnUiThread { binding.tvSetupStatus.text = msg }

    private suspend fun extractAndLaunch(svc: IShellService) {
        showConsole()

        log("Checking bot files…")
        withContext(Dispatchers.IO) { runCatching { if (extractBotCodeIfNeeded()) scope.launch(Dispatchers.Main) { log("Extracted bot files from APK") } } }

        log("Setting up Python environment (~2.3 GB, first launch only)…")
        withContext(Dispatchers.IO) {
            runCatching { extractEnv(svc) }.onFailure { e ->
                scope.launch(Dispatchers.Main) { log("Env setup failed: ${e.message}") }
            }
        }

        showNormalMode()
        log("Ready. Launching bot…")
        launchBot()
    }

    private fun extractBotCode() {
        copyAssetDir("pylaai/cfgs_and_internal", File(botDir))
    }

    private fun apkStamp(): String = try {
        packageManager.getPackageInfo(packageName, 0).lastUpdateTime.toString()
    } catch (_: Exception) { "0" }

    private fun extractBotCodeIfNeeded(): Boolean {
        val stampFile = File(botDir, ".bot_stamp")
        val current = apkStamp()
        val upToDate = File("$botDir/main.py").exists() &&
            stampFile.exists() && stampFile.readText().trim() == current
        if (upToDate) return false
        extractBotCode()
        runCatching { stampFile.writeText(current) }
        return true
    }

    private fun copyAssetDir(assetPath: String, dest: File) {
        dest.mkdirs()
        val children = assets.list(assetPath) ?: return
        for (child in children) {
            val childAsset = "$assetPath/$child"
            val childDest  = File(dest, child)
            if (!assets.list(childAsset).isNullOrEmpty()) {
                copyAssetDir(childAsset, childDest)
            } else {
                assets.open(childAsset).use { input ->
                    childDest.outputStream().use { output -> input.copyTo(output) }
                }
            }
        }
    }

    private fun extractEnv(svc: IShellService) {
        // The Python env is bundled INSIDE the APK (assets/env_trim.tar.gz, stored
        // uncompressed via noCompress). Copy it out to external files (shell-readable),
        // then decompress + untar into ENV_ROOT, then delete the temp. Fully self-contained
        // — no separate env download or SD placement needed.
        val extDir = getExternalFilesDir(null)?.absolutePath
            ?: throw IllegalStateException("no external files dir")
        val gzPath = "$extDir/env_trim.tar.gz"
        scope.launch(Dispatchers.Main) { log("Unpacking bundled Python env…") }

        // 1) copy asset out of the APK (0–50% of the progress bar)
        val assetLen = try { assets.openFd("env_trim.dat").use { it.length } } catch (_: Exception) { -1L }
        assets.open("env_trim.dat").use { input ->
            File(gzPath).outputStream().use { output ->
                val buf = ByteArray(1 shl 20)
                var copied = 0L; var lastPct = -1; var n: Int
                while (input.read(buf).also { n = it } > 0) {
                    output.write(buf, 0, n); copied += n
                    if (assetLen > 0) {
                        val pct = ((copied * 50) / assetLen).toInt().coerceIn(0, 50)
                        if (pct != lastPct) {
                            lastPct = pct
                            scope.launch(Dispatchers.Main) { setSetupStatus("Setting up Python environment… $pct%") }
                        }
                    }
                }
            }
        }
        svc.execute("chmod 644 '$gzPath' 2>/dev/null")

        // 2) decompress + untar in the background (50–100%)
        svc.execute("rm -rf $ENV_ROOT && mkdir -p $ENV_ROOT")
        val doneMarker = "$ENV_ROOT/.extract_done"
        svc.execute(
            "rm -f $doneMarker; setsid sh -c \"gzip -dc '$gzPath' | tar x -C $ENV_ROOT; " +
            "chmod 755 $ENV_ROOT/usr/bin/* $ENV_ROOT/usr/lib/*.so* 2>/dev/null; " +
            "rm -f '$gzPath'; touch $doneMarker\" </dev/null >/dev/null 2>&1 &"
        )
        val totalKb = 1_100_000L
        var lastPct = -1
        while (true) {
            val finished = svc.execute("[ -f $doneMarker ] && echo 1 || echo 0").trim() == "1"
            val curKb = svc.execute("du -sk $ENV_ROOT 2>/dev/null | cut -f1").trim().toLongOrNull() ?: 0L
            var pct = 50 + ((curKb * 50) / totalKb).toInt().coerceIn(0, 49)
            if (finished) pct = 100
            if (pct != lastPct) {
                lastPct = pct
                scope.launch(Dispatchers.Main) {
                    setSetupStatus("Setting up Python environment… $pct%")
                    if (pct == 100 || pct % 10 == 0) log("Python env: $pct%")
                }
            }
            if (finished) break
            Thread.sleep(1000)
        }

        if (svc.execute("[ -x $PYTHON ] && echo ok || echo no").trim() == "ok")
            svc.execute("touch $ENV_MARKER")
        else
            throw IllegalStateException("env extraction failed (likely out of internal storage)")
    }

    private fun isBotRunning(svc: IShellService): Boolean =

        svc.execute("ps -A -o ARGS 2>/dev/null | grep main.py | grep -v grep").trim().isNotEmpty()

    private fun showNormalMode() = runOnUiThread {
        binding.layoutSetup.visibility  = View.GONE
        binding.layoutNormal.visibility = View.VISIBLE
        binding.etBotPath.visibility    = View.GONE

        binding.tvStatus.text = "Bot ready to start"

        binding.btnStart.setOnClickListener {
            binding.btnStart.isEnabled = false
            showConsole()
            log("Connecting Shizuku…")
            connectShizuku()
        }
        binding.btnOpenBrowser.setOnClickListener { flaskUrl?.let { openBrowser(it) } }
        binding.btnStopBot.setOnClickListener { stopBot() }

        binding.btnReinstallDeps.setOnClickListener {
            showConsole()
            val svc = shellService
            if (svc != null) {
                scope.launch {
                    svc.execute("rm -rf $ENV_ROOT")
                    extractAndLaunch(svc)
                }
            } else {
                log("Connecting Shizuku for re-extract…")
                pendingReinstall = true
                if (!Shizuku.pingBinder()) {
                    log("Shizuku not running — start it then try again.")
                    pendingReinstall = false; return@setOnClickListener
                }
                when (Shizuku.checkSelfPermission()) {
                    PackageManager.PERMISSION_GRANTED -> bindShizuku()
                    else -> Shizuku.requestPermission(SHIZUKU_REQUEST_CODE)
                }
            }
        }
    }

    private fun connectShizuku() {
        binding.btnStart.isEnabled = false
        if (!Shizuku.pingBinder()) {
            log("Shizuku not running. Start it then press Start again.")
            binding.btnStart.isEnabled = true; return
        }
        when (Shizuku.checkSelfPermission()) {
            PackageManager.PERMISSION_GRANTED -> bindShizuku()
            else -> Shizuku.requestPermission(SHIZUKU_REQUEST_CODE)
        }
    }

    private fun launchBot() {
        scope.launch {
            val dir = botDir
            withContext(Dispatchers.IO) {
                runCatching {
                    if (extractBotCodeIfNeeded())
                        scope.launch(Dispatchers.Main) { log("Bot files updated from APK") }
                }
                val svc = shellService ?: return@withContext
                if (isBotRunning(svc)) {
                    scope.launch(Dispatchers.Main) { log("Bot already running — reattaching, not relaunching.") }
                    return@withContext
                }
                svc.execute("echo '' > $BOT_LOG && chmod 666 $BOT_LOG")

                val inferAddr = awaitInferAddrBlocking(6000)
                val inferEnv = if (inferAddr != null) " PYLA_INFER_ADDR=$inferAddr" else ""
                scope.launch(Dispatchers.Main) {
                    log(if (inferAddr != null) "GPU inference service at $inferAddr ✓" else "Inference service not ready — bot uses CPU (ORT)")
                }

                val env = "HOME=$ENV_ROOT/usr PREFIX=$ENV_ROOT/usr " +
                    "PATH=$ENV_ROOT/usr/bin:$ENV_ROOT/usr/sbin:/system/bin " +
                    "LD_LIBRARY_PATH=$ENV_ROOT/usr/lib " +
                    "PYTHONHOME=$ENV_ROOT/usr " +
                    "SSL_CERT_FILE=$ENV_ROOT/usr/etc/tls/cert.pem " +
                    "SSL_CERT_DIR=$ENV_ROOT/usr/etc/tls/certs " +
                    "TMPDIR=/data/local/tmp PYLA_ROOT=$dir PYTHONUNBUFFERED=1" + inferEnv

                val cmd = "cd $dir && $env setsid $PYTHON main.py >> $BOT_LOG 2>&1 </dev/null &"
                svc.execute(cmd)

                val resetDisp = "/system/bin/wm size reset >/dev/null 2>&1; " +
                    "/system/bin/wm density reset >/dev/null 2>&1; " +
                    "/system/bin/svc power stayon false >/dev/null 2>&1"
                val waitDie = "while ps -A -o COMM,ARGS 2>/dev/null | grep -qE '^python.*main.py'; do sleep 2; done"
                val watchdog = "setsid sh -c \"sleep 8; " +
                    "for i in 1 2 3 4 5 6 7 8 9 10; do " +
                        "$waitDie; sleep 3; $cmd sleep 12; " +
                    "done; " +
                    "$waitDie; $resetDisp\" </dev/null >/dev/null 2>&1 &"
                svc.execute(watchdog)
            }
            log("Bot started. Watching log…")
            startLogTailing(BOT_LOG)
            startFlaskPolling()
        }
    }

    private fun startLogTailing(logFile: String) {
        logTailJob?.cancel()
        logLine = 0
        logTailJob = scope.launch(Dispatchers.IO) {
            delay(2500)
            while (isActive) {
                val svc = shellService ?: break
                val next = logLine + 1
                val out = try { svc.execute("tail -n +$next $logFile 2>/dev/null") }
                          catch (_: Exception) { break }
                if (out.isNotBlank() && !out.startsWith("ERROR") && !out.startsWith("STDERR")) {
                    val lines = out.trimEnd().split("\n").filter { it.isNotBlank() }
                    if (lines.isNotEmpty()) {
                        logLine += lines.size
                        withContext(Dispatchers.Main) { lines.forEach { log(it) } }
                    }
                }
                delay(2000)
            }
        }
    }

    private fun startFlaskPolling() {
        pollingJob?.cancel()
        pollingJob = scope.launch {
            var n = 0
            while (isActive) {
                n++
                val url = withContext(Dispatchers.IO) { findFlask() }
                if (url != null) {
                    flaskUrl = url; log("Flask ready at $url ✓")
                    binding.tvStatus.text = "Ready ✓"
                    binding.btnOpenBrowser.visibility = View.VISIBLE
                    startOverlay()
                    delay(400); openBrowser(url); break
                }
                if (n % 10 == 0) log("Waiting for Flask… ($n)")
                binding.tvStatus.text = "Bot starting… ($n)"
                delay(1500)
            }
        }
    }

    private fun findFlask(): String? {
        for (port in FLASK_PORTS) {
            try {
                val c = URL("http://127.0.0.1:$port/api/health").openConnection() as HttpURLConnection
                c.connectTimeout = 700; c.readTimeout = 700
                val ok = c.responseCode == 200
                c.disconnect()
                if (ok) return "http://127.0.0.1:$port"
            } catch (_: Exception) {}
        }
        return null
    }

    private fun openBrowser(url: String) = try {
        startActivity(Intent(this, WebViewActivity::class.java).putExtra(WebViewActivity.EXTRA_URL, url))
    } catch (e: Exception) {
        log("In-app UI failed (${e.message}); opening external browser")
        runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }
    }

    private fun stopBot() {
        val svc = shellService
        if (svc == null) {
            log("Can't stop: Shizuku not connected. Open Shizuku, then reconnect.")
            return
        }
        binding.btnStopBot.isEnabled = false
        log("Force-killing Python bot…")
        scope.launch {
            withContext(Dispatchers.IO) {
                runCatching {
                    svc.execute("pkill -f 'main.py' 2>/dev/null; sleep 1; pkill -9 -f 'main.py' 2>/dev/null")

                    svc.execute("/system/bin/wm size reset >/dev/null 2>&1; " +
                                "/system/bin/wm density reset >/dev/null 2>&1; " +
                                "/system/bin/svc power stayon false >/dev/null 2>&1")
                }
            }
            pollingJob?.cancel(); logTailJob?.cancel()
            flaskUrl = null
            runCatching { stopService(Intent(this@MainActivity, OverlayService::class.java)) }
            binding.tvStatus.text = "Stopped — press Start to relaunch"
            binding.btnOpenBrowser.visibility = View.GONE
            binding.btnStopBot.isEnabled = true
            log("Bot killed.")
        }
    }

    private fun startInference() {
        val i = Intent(this, InferenceService::class.java)
        runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(i) else startService(i)
            log("Inference service started")
        }.onFailure { log("Inference start failed: ${it.message}") }
    }

    private fun awaitInferAddrBlocking(timeoutMs: Long): String? {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val port = runCatching {
                File(filesDir, InferenceService.PORT_FILE).takeIf { it.exists() }?.readText()?.trim()?.toIntOrNull()
            }.getOrNull()
            if (port != null && inferHealthOk(port)) return "127.0.0.1:$port"
            Thread.sleep(400)
        }
        return null
    }

    private fun inferHealthOk(port: Int): Boolean = try {
        java.net.Socket().use { s ->
            s.connect(java.net.InetSocketAddress("127.0.0.1", port), 400)
            s.soTimeout = 500

            val req = java.nio.ByteBuffer.allocate(8 + 2 + 4).order(java.nio.ByteOrder.LITTLE_ENDIAN)
            req.put('P'.code.toByte()); req.put('Y'.code.toByte()); req.put('L'.code.toByte()); req.put('A'.code.toByte())
            req.put(1.toByte()); req.put(2.toByte()); req.put(0.toByte()); req.put(0.toByte())
            req.putShort(0); req.putInt(0)
            s.getOutputStream().apply { write(req.array()); flush() }
            val resp = ByteArray(9)
            val n = s.getInputStream().read(resp)
            n >= 6 && resp[0] == 'P'.code.toByte() && resp[5].toInt() == 0
        }
    } catch (_: Exception) { false }

    private fun startOverlay() {
        val url = flaskUrl ?: return
        if (!Settings.canDrawOverlays(this)) {
            log("Overlay needs 'Draw over other apps' — grant it, then reopen to enable.")
            runCatching {
                startActivity(
                    Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                )
            }
            return
        }
        val i = Intent(this, OverlayService::class.java).putExtra(OverlayService.EXTRA_URL, "$url/api/stats")
        runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(i) else startService(i)
            log("Overlay started ✓")
        }.onFailure { log("Overlay start failed: ${it.message}") }
    }

    private fun showConsole() = runOnUiThread { binding.consoleScroll.visibility = View.VISIBLE }

    private fun log(msg: String) = runOnUiThread {
        val ts = java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault()).format(java.util.Date())
        binding.tvConsole.append("[$ts] $msg\n")
        binding.consoleScroll.post { binding.consoleScroll.fullScroll(View.FOCUS_DOWN) }
    }
}
