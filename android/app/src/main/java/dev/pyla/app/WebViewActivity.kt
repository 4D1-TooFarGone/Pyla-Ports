package dev.pyla.app

import android.annotation.SuppressLint
import android.os.Bundle
import android.view.ViewGroup
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import androidx.appcompat.app.AppCompatActivity

class WebViewActivity : AppCompatActivity() {

    private lateinit var web: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val url = intent.getStringExtra(EXTRA_URL)
        if (url.isNullOrEmpty()) { finish(); return }

        web = WebView(this)
        web.layoutParams = FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
        setContentView(web)

        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            useWideViewPort = true
            loadWithOverviewMode = true
            builtInZoomControls = true
            displayZoomControls = false
            setSupportZoom(true)
            userAgentString = DESKTOP_UA
            @Suppress("DEPRECATION")
            allowFileAccess = true
        }

        web.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                view?.evaluateJavascript(VIEWPORT_JS, null)
            }
            override fun onPageFinished(view: WebView?, url: String?) {
                view?.evaluateJavascript(VIEWPORT_JS, null)
            }
        }
        web.webChromeClient = WebChromeClient()
        web.loadUrl(url)
    }

    @Deprecated("kept for minSdk 26 back navigation")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    override fun onDestroy() {
        runCatching { web.destroy() }
        super.onDestroy()
    }

    companion object {
        const val EXTRA_URL = "url"

        private const val DESKTOP_UA =
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

        private const val VIEWPORT_JS =
            "(function(){var v=document.querySelector('meta[name=viewport]');" +
            "if(!v){v=document.createElement('meta');v.setAttribute('name','viewport');" +
            "(document.head||document.getElementsByTagName('head')[0]).appendChild(v);}" +
            "v.setAttribute('content','width=1280');})();"
    }
}
