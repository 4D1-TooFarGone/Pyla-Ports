package dev.pyla.app

import android.content.res.AssetManager

class AssetExtractor(
    private val assets: AssetManager,
    private val svc: IShellService
) {
    companion object {
        const val ASSET_ROOT = "pylaai"
        const val TMP_DEST   = "/data/local/tmp/pylaai"
    }

    fun extractAll(onFile: (String) -> Unit = {}): Boolean {
        return try {
            svc.execute("mkdir -p $TMP_DEST")
            copyTree(ASSET_ROOT, TMP_DEST, "", onFile)
            svc.execute("find $TMP_DEST -type d -exec chmod 755 {} +")
            svc.execute("find $TMP_DEST -type f -exec chmod 644 {} +")
            true
        } catch (e: Exception) { false }
    }

    private fun copyTree(assetPath: String, destBase: String, rel: String, onFile: (String) -> Unit) {
        val children = assets.list(assetPath) ?: return
        for (child in children) {
            val childAsset = "$assetPath/$child"
            val childRel   = if (rel.isEmpty()) child else "$rel/$child"
            val childDest  = "$destBase/$childRel"
            if (assets.list(childAsset)?.isNotEmpty() == true) {
                svc.execute("mkdir -p $childDest")
                copyTree(childAsset, destBase, childRel, onFile)
            } else {
                onFile(childRel)
                val data = assets.open(childAsset).readBytes()
                val result = svc.writeFileChunk(childDest, data, true)
                if (result.startsWith("ERROR")) throw RuntimeException("writeFileChunk failed: $result")
            }
        }
    }
}
