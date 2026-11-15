"""Build the Fire Connect phone app (mavbac-connect, Expo) as an installable web app served at /app.

    python build_app.py

Runs `npx expo export --platform web` into ./app, then makes it installable on a phone home screen
(app manifest, icon, iPhone settings). server.py serves the result at https://<site>/app/.
"""
import json, os, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "mavbac-connect")
OUT = os.path.join(ROOT, "app")

HEAD = """
    <link rel="manifest" href="/app/manifest.webmanifest">
    <link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-title" content="Fire Connect">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="theme-color" content="#0d1717">
    <style>html,body{background:#0d1717}</style>
"""

MANIFEST = {
    "name": "Fire Connect by MavBAC",
    "short_name": "Fire Connect",
    "description": "Report fires, ask questions and get official alerts from forecasters and first responders.",
    "id": "/app/",
    "start_url": "/app/",
    "scope": "/app/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0d1717",
    "theme_color": "#0d1717",
    "icons": [
        {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    npx = "npx.cmd" if os.name == "nt" else "npx"
    subprocess.run([npx, "expo", "export", "--platform", "web", "--output-dir", OUT, "--clear"], cwd=SRC, check=True)

    p = os.path.join(OUT, "index.html")
    html = open(p, encoding="utf-8").read()
    html = html.replace('content="width=device-width, initial-scale=1, shrink-to-fit=no"',
                        'content="width=device-width, initial-scale=1, viewport-fit=cover"')
    assert "</head>" in html
    html = html.replace("</head>", HEAD + "</head>", 1)
    open(p, "w", encoding="utf-8", newline="\n").write(html)
    with open(os.path.join(OUT, "manifest.webmanifest"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(MANIFEST, f, indent=2)
    print("Built", OUT)


if __name__ == "__main__":
    sys.exit(main())
