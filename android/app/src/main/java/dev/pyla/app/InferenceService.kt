package dev.pyla.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.res.AssetFileDescriptor
import android.os.Build
import android.os.IBinder
import android.util.Log
import org.tensorflow.lite.Delegate
import org.tensorflow.lite.Interpreter
import org.tensorflow.lite.gpu.CompatibilityList
import org.tensorflow.lite.gpu.GpuDelegate
import java.io.File
import java.io.FileInputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.MappedByteBuffer
import java.nio.channels.FileChannel
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import kotlin.concurrent.thread

class InferenceService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    @Volatile private var running = true
    private var server: ServerSocket? = null
    private var port = -1
    private val slots = ConcurrentHashMap<String, ModelSlot>()
    private val compat by lazy { CompatibilityList() }

    private class ModelSlot(val key: String) {
        val exec = Executors.newSingleThreadExecutor()
        @Volatile var state = 0
        @Volatile var backend = BK_NA
        var interp: Interpreter? = null
        var delegate: Delegate? = null
        var outBytes = 0
        var outDims = IntArray(0)
        var inBuf: ByteBuffer? = null
        var outBuf: ByteBuffer? = null
        var nCalls = 0
        var sumMs = 0.0
    }

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIF_ID, buildNotification())
        thread(name = "pyla-infer-accept", isDaemon = true) { serve() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        super.onDestroy()
        running = false
        runCatching { server?.close() }
        for (slot in slots.values) {
            slot.exec.submit {
                runCatching { slot.interp?.close() }
                runCatching { slot.delegate?.close() }
            }
            slot.exec.shutdown()
        }
    }

    private fun serve() {
        val loopback = InetAddress.getByName("127.0.0.1")
        var chosen = -1
        for (p in PORT_RANGE) {
            try { server = ServerSocket(p, 8, loopback); chosen = p; break }
            catch (_: Exception) {  }
        }
        if (chosen < 0) { Log.e(T, "no free port in $PORT_RANGE"); return }
        port = chosen
        runCatching { File(filesDir, PORT_FILE).writeText(port.toString()) }
        Log.i(T, "inference server listening on 127.0.0.1:$port (pid=${android.os.Process.myPid()})")
        while (running) {
            val sock = try { server!!.accept() } catch (e: Exception) { if (running) Log.w(T, "accept: ${e.message}"); break }
            thread(isDaemon = true) { handleConn(sock) }
        }
    }

    private fun handleConn(sock: Socket) {
        try {
            sock.tcpNoDelay = true
            val ins = sock.getInputStream(); val outs = sock.getOutputStream()
            while (running) {
                val head = readExact(ins, REQ_HEAD) ?: break
                if (!(head[0]=='P'.code.toByte() && head[1]=='Y'.code.toByte() && head[2]=='L'.code.toByte() && head[3]=='A'.code.toByte())) {
                    Log.w(T, "bad magic"); break
                }
                val opcode = head[5].toInt() and 0xff
                val ndim = head[7].toInt() and 0xff
                val dims = IntArray(ndim)
                if (ndim > 0) {
                    val db = readExact(ins, 4 * ndim) ?: break
                    val bb = ByteBuffer.wrap(db).order(ByteOrder.LITTLE_ENDIAN)
                    for (i in 0 until ndim) dims[i] = bb.int
                }
                val keyLenB = readExact(ins, 2) ?: break
                val keyLen = ByteBuffer.wrap(keyLenB).order(ByteOrder.LITTLE_ENDIAN).short.toInt() and 0xffff
                val key = if (keyLen > 0) String(readExact(ins, keyLen) ?: break, Charsets.UTF_8) else ""
                val plenB = readExact(ins, 4) ?: break
                val plen = ByteBuffer.wrap(plenB).order(ByteOrder.LITTLE_ENDIAN).int
                val payload = if (plen > 0) (readExact(ins, plen) ?: break) else ByteArray(0)

                when (opcode) {
                    OP_HEALTH -> writeResp(outs, ST_OK, BK_NA, IntArray(0), healthJson().toByteArray())
                    OP_INFO   -> writeResp(outs, ST_OK, slots[key]?.backend ?: BK_NA, IntArray(0), (slots[key]?.backendName() ?: "n/a").toByteArray())
                    OP_INFER  -> handleInfer(outs, key, payload)
                    else      -> writeResp(outs, ST_ERR, BK_NA, IntArray(0), "bad opcode".toByteArray())
                }
            }
        } catch (e: Exception) {
            if (running) Log.w(T, "conn: ${e.message}")
        } finally { runCatching { sock.close() } }
    }

    private fun handleInfer(outs: java.io.OutputStream, key: String, payload: ByteArray) {
        val path = assetPathFor(key)
        if (path == null) { writeResp(outs, ST_ERR, BK_NA, IntArray(0), "unknown model $key".toByteArray()); return }
        val slot = slots.getOrPut(key) { ModelSlot(key) }
        when (slot.state) {
            2 -> {
                try {
                    val t0 = System.nanoTime()
                    val res = slot.exec.submit<ByteArray> { runInfer(slot, payload) }.get()
                    val ms = (System.nanoTime() - t0) / 1e6
                    slot.nCalls++; slot.sumMs += ms
                    if (slot.nCalls % 20 == 0)
                        Log.i(T, "${slot.key} ${slot.backendName()} avg=${"%.0f".format(slot.sumMs / slot.nCalls)}ms last=${"%.0f".format(ms)}ms (${slot.nCalls} calls)")
                    writeResp(outs, ST_OK, slot.backend, slot.outDims, res)
                } catch (e: Exception) {
                    Log.w(T, "$key infer failed: ${e.message}")
                    writeResp(outs, ST_ERR, slot.backend, IntArray(0), "infer error".toByteArray())
                }
            }
            0 -> {
                slot.state = 1
                slot.exec.submit { loadModel(slot, path) }
                writeResp(outs, ST_WARMING, BK_NA, IntArray(0), "loading".toByteArray())
            }
            1 -> writeResp(outs, ST_WARMING, BK_NA, IntArray(0), "loading".toByteArray())
            else -> writeResp(outs, ST_ERR, BK_NA, IntArray(0), "model failed to load".toByteArray())
        }
    }

    private fun loadModel(slot: ModelSlot, assetPath: String) {
        try {
            val model = mapAsset(assetPath)

            val cpuRef = Interpreter(model, Interpreter.Options().apply { setUseXNNPACK(false); numThreads = 1 })
            val inBytes = cpuRef.getInputTensor(0).numBytes()
            slot.outBytes = cpuRef.getOutputTensor(0).numBytes()
            slot.outDims = cpuRef.getOutputTensor(0).shape()

            slot.inBuf = ByteBuffer.allocateDirect(inBytes).order(ByteOrder.nativeOrder())
            slot.outBuf = ByteBuffer.allocateDirect(slot.outBytes).order(ByteOrder.nativeOrder())
            val refIn = deterministicInput(inBytes)
            val refOut = FloatArray(slot.outBytes / 4)
            runRaw(cpuRef, refIn, slot.outBytes, refOut)

            try {
                val d = if (compat.isDelegateSupportedOnThisDevice) GpuDelegate(compat.bestOptionsForThisDevice) else GpuDelegate()
                val gi = Interpreter(model, Interpreter.Options().apply { addDelegate(d) })
                val gOut = FloatArray(slot.outBytes / 4)
                runRaw(gi, refIn, slot.outBytes, gOut)
                val diff = maxAbsDiff(gOut, refOut)
                if (diff < GPU_TOL) {
                    slot.interp = gi; slot.delegate = d; slot.backend = BK_GPU; slot.state = 2
                    runCatching { cpuRef.close() }
                    Log.i(T, "${slot.key}: GPU ready (diff=$diff)"); return
                }
                Log.w(T, "${slot.key}: GPU output diverges (diff=$diff) -> demote"); gi.close(); d.close()
            } catch (t: Throwable) { Log.w(T, "${slot.key}: GPU unavailable: ${t.message}") }

            try {
                val xi = Interpreter(model, Interpreter.Options().apply { setUseXNNPACK(true); numThreads = 2 })
                val xOut = FloatArray(slot.outBytes / 4); runRaw(xi, refIn, slot.outBytes, xOut)
                slot.interp = xi; slot.backend = BK_XNNPACK; slot.state = 2
                runCatching { cpuRef.close() }
                Log.i(T, "${slot.key}: XNNPACK ready"); return
            } catch (t: Throwable) { Log.w(T, "${slot.key}: XNNPACK failed: ${t.message}") }

            slot.interp = cpuRef; slot.backend = BK_CPU; slot.state = 2
            Log.i(T, "${slot.key}: CPU ready")
        } catch (t: Throwable) {
            Log.e(T, "${slot.key}: load FAILED: ${t.message}", t); slot.state = 3
        }
    }

    private fun runInfer(slot: ModelSlot, payload: ByteArray): ByteArray {
        val interp = slot.interp!!
        val inBuf = slot.inBuf!!; val outBuf = slot.outBuf!!
        inBuf.rewind(); inBuf.put(payload); inBuf.rewind()
        outBuf.rewind()
        interp.run(inBuf, outBuf)
        outBuf.rewind()
        val out = ByteArray(slot.outBytes); outBuf.get(out); return out
    }

    private fun runRaw(interp: Interpreter, input: ByteBuffer, outBytes: Int, out: FloatArray) {
        val outBuf = ByteBuffer.allocateDirect(outBytes).order(ByteOrder.nativeOrder())
        repeat(2) { input.rewind(); outBuf.rewind(); interp.run(input, outBuf) }
        outBuf.rewind(); outBuf.asFloatBuffer().get(out)
    }

    private fun assetPathFor(key: String): String? {

        val at = key.indexOf('@'); if (at <= 0) return null
        val name = key.substring(0, at); val size = key.substring(at + 1)
        if (name != "mainInGameModel" && name != "tileDetector") return null
        if (size !in listOf("320", "480", "640")) return null
        val path = "models/${name}_${size}.tflite"
        return try { assets.openFd(path).close(); path } catch (_: Exception) { null }
    }

    private fun deterministicInput(nBytes: Int): ByteBuffer {
        val b = ByteBuffer.allocateDirect(nBytes).order(ByteOrder.nativeOrder())
        val f = b.asFloatBuffer(); var s = 12345
        for (i in 0 until nBytes / 4) { s = (s * 1103515245 + 12345) and 0x7fffffff; f.put((s % 1000) / 1000f) }
        return b
    }

    private fun mapAsset(path: String): MappedByteBuffer {
        val afd: AssetFileDescriptor = assets.openFd(path)
        FileInputStream(afd.fileDescriptor).use { return it.channel.map(FileChannel.MapMode.READ_ONLY, afd.startOffset, afd.declaredLength) }
    }

    private fun maxAbsDiff(a: FloatArray, b: FloatArray): Float {
        var m = 0f; for (i in a.indices) { val d = kotlin.math.abs(a[i] - b[i]); if (d > m) m = d }; return m
    }

    private fun ModelSlot.backendName() = when (backend) {
        BK_GPU -> "gpu"; BK_NNAPI -> "nnapi"; BK_XNNPACK -> "xnnpack"; BK_CPU -> "cpu"; else -> "n/a"
    }

    private fun healthJson(): String {
        val sb = StringBuilder("{\"port\":$port,\"models\":{")
        var first = true
        for ((k, s) in slots) { if (!first) sb.append(","); sb.append("\"$k\":\"${s.backendName()}\""); first = false }
        sb.append("}}"); return sb.toString()
    }

    private fun readExact(ins: java.io.InputStream, n: Int): ByteArray? {
        val buf = ByteArray(n); var off = 0
        while (off < n) { val r = ins.read(buf, off, n - off); if (r < 0) return null; off += r }
        return buf
    }

    private fun writeResp(outs: java.io.OutputStream, status: Int, backend: Int, dims: IntArray, payload: ByteArray) {

        val header = ByteBuffer.allocate(9 + 4 * dims.size + 4).order(ByteOrder.LITTLE_ENDIAN)
        header.put('P'.code.toByte()); header.put('Y'.code.toByte()); header.put('L'.code.toByte()); header.put('A'.code.toByte())
        header.put(VERSION.toByte()); header.put(status.toByte()); header.put(backend.toByte()); header.put(0.toByte())
        header.put(dims.size.toByte())
        for (d in dims) header.putInt(d)
        header.putInt(payload.size)
        synchronized(outs) { outs.write(header.array()); if (payload.isNotEmpty()) outs.write(payload); outs.flush() }
    }

    private fun buildNotification(): Notification {
        val chId = "pyla_inference"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(chId, "Pyla Inference", NotificationManager.IMPORTANCE_LOW)
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).createNotificationChannel(ch)
        }
        val b = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) Notification.Builder(this, chId)
                else @Suppress("DEPRECATION") Notification.Builder(this)
        return b.setContentTitle("PylaAI inference").setContentText("GPU inference service")
            .setSmallIcon(android.R.drawable.ic_menu_manage).build()
    }

    companion object {
        private const val T = "PylaInfer"
        private const val NOTIF_ID = 43
        const val PORT_FILE = "infer_port.txt"
        private val PORT_RANGE = 5300..5320
        private const val VERSION = 1
        private const val REQ_HEAD = 8
        private const val GPU_TOL = 0.1f

        private const val OP_INFER = 1; private const val OP_HEALTH = 2; private const val OP_INFO = 3

        private const val ST_OK = 0; private const val ST_ERR = 1; private const val ST_WARMING = 2

        private const val BK_GPU = 0; private const val BK_NNAPI = 1; private const val BK_XNNPACK = 2; private const val BK_CPU = 3; private const val BK_NA = 255
    }
}
