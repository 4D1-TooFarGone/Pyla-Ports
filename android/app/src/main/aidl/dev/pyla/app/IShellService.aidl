package dev.pyla.app;

interface IShellService {
    String execute(String command);
    String download(String url, String destPath);
    String writeFileChunk(String destPath, in byte[] data, boolean overwrite);
    void destroy();
}
