package dev.pyla.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.IBinder
import android.util.TypedValue
import android.view.Gravity
import android.view.MotionEvent
import android.view.WindowManager
import android.widget.TextView
import kotlinx.coroutines.*
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class OverlayService : Service() {

    private lateinit var wm: WindowManager
    private var view: TextView? = null
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private var statsUrl: String? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        wm = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        startForeground(NOTIF_ID, buildNotification())
        addOverlay()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        intent?.getStringExtra(EXTRA_URL)?.let { statsUrl = it }
        return START_STICKY
    }

    private fun addOverlay() {
        if (view != null) return
        val bg = GradientDrawable().apply {
            cornerRadius = 26f
            setColor(Color.parseColor("#E6101018"))
            setStroke(2, Color.parseColor("#40FFFFFF"))
        }
        val tv = TextView(this).apply {
            text = "PylaAI\nconnecting…"
            setTextColor(Color.WHITE)
            background = bg
            setPadding(30, 20, 30, 20)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            setLineSpacing(4f, 1f)
        }
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE
        val lp = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT
        )
        lp.gravity = Gravity.TOP or Gravity.START
        lp.x = 24; lp.y = 140

        var initX = 0; var initY = 0; var touchX = 0f; var touchY = 0f
        tv.setOnTouchListener { _, e ->
            when (e.action) {
                MotionEvent.ACTION_DOWN -> { initX = lp.x; initY = lp.y; touchX = e.rawX; touchY = e.rawY; true }
                MotionEvent.ACTION_MOVE -> {
                    lp.x = initX + (e.rawX - touchX).toInt()
                    lp.y = initY + (e.rawY - touchY).toInt()
                    runCatching { wm.updateViewLayout(tv, lp) }
                    true
                }
                else -> false
            }
        }
        runCatching { wm.addView(tv, lp) }
        view = tv
        startPolling()
    }

    private fun startPolling() {
        scope.launch {
            while (isActive) {
                val txt = withContext(Dispatchers.IO) { fetchStats() }
                view?.text = txt
                delay(1000)
            }
        }
    }

    private fun fetchStats(): String {
        val url = statsUrl ?: return "PylaAI\nconnecting…"
        return try {
            val c = URL(url).openConnection() as HttpURLConnection
            c.connectTimeout = 900; c.readTimeout = 900
            val body = c.inputStream.bufferedReader().use { it.readText() }
            c.disconnect()
            val j = JSONObject(body)
            val ips = j.optDouble("ips", 0.0)
            val running = j.optBoolean("is_running", false)
            var state = j.optString("state", "")
            if (state.isEmpty() || state == "null") state = "—"
            val brawler = j.optString("brawler", "")
            val line2 = if (running) state else "stopped"
            val line3 = if (running && brawler.isNotEmpty() && brawler != "null") "\n🤖 $brawler" else ""
            "⚡ ${"%.2f".format(ips)} IPS\n▸ $line2$line3"
        } catch (e: Exception) {
            "PylaAI\n(no data)"
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        scope.cancel()
        view?.let { v -> runCatching { wm.removeView(v) } }
        view = null
    }

    private fun buildNotification(): Notification {
        val chId = "pyla_overlay"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(chId, "Pyla Overlay", NotificationManager.IMPORTANCE_LOW)
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(ch)
        }
        val b = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            Notification.Builder(this, chId) else @Suppress("DEPRECATION") Notification.Builder(this)
        return b.setContentTitle("PylaAI overlay")
            .setContentText("Showing live bot stats")
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .build()
    }

    companion object {
        const val EXTRA_URL = "stats_url"
        private const val NOTIF_ID = 42
    }
}
