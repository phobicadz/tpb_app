from flask import Flask, render_template, request, jsonify
from urllib.parse import quote_plus
import requests

app = Flask(__name__)

API_URL = "https://apibay.org/q.php"
DETAILS_URL = "https://thepiratebay.org/description.php"
TORRENT_URL = "https://thepiratebay.org/torrent.php"

# qBittorrent Web API client
qb_session = requests.Session()


def qb_login(base_url, username, password):
    """Login to qBittorrent Web API."""
    global qb_session
    try:
        resp = qb_session.post(f"{base_url}/api/v2/auth/login",
                              data={"username": username, "password": password},
                              timeout=5)
        return resp.text == "Ok."
    except Exception:
        return False


def qb_add_torrent(base_url, magnet):
    """Add torrent to qBittorrent via Web API."""
    try:
        resp = qb_session.post(f"{base_url}/api/v2/torrents/add",
                              data={"urls": magnet},
                              timeout=5)
        return resp.text == "Ok."
    except Exception as e:
        return str(e)


def format_size(size_bytes):
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = float(size_bytes)
    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def search_torrents(title, top_n=20):
    params = {"q": quote_plus(title), "cat": ""}
    try:
        resp = requests.get(API_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data[:top_n]:
            results.append({
                "id": item["id"],
                "name": item["name"],
                "size": format_size(int(item["size"])),
                "size_bytes": int(item["size"]),
                "seeders": int(item["seeders"]),
                "leechers": int(item["leechers"]),
                "info_hash": item.get("info_hash", ""),
                "url": f"{DETAILS_URL}?id={item['id']}",
                "download": f"{TORRENT_URL}?id={item['id']}",
                "magnet": f"magnet:?xt=urn:btih:{item.get('info_hash', '')}" if item.get("info_hash") else None,
            })
        return results
    except Exception as e:
        return []


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search", methods=["GET"])
def search():
    query = request.args.get("q", "")
    top_n = request.args.get("n", 20, type=int)
    results = search_torrents(query, top_n) if query else []
    return render_template("results.html", query=query, results=results, count=len(results))


@app.route("/api/qbittorrent/add", methods=["POST"])
def qb_add_api():
    """Proxy endpoint to add torrent to qBittorrent."""
    data = request.get_json()
    base_url = data.get("url", "http://desktop.mancave:8080")
    username = data.get("username", "admin")
    password = data.get("password", "weasel")
    magnet = data.get("magnet", "")

    # Login first
    if not qb_login(base_url, username, password):
        return jsonify({"success": False, "error": "qBittorrent login failed"}), 500

    result = qb_add_torrent(base_url, magnet)
    if isinstance(result, str):
        return jsonify({"success": False, "error": result}), 500

    return jsonify({"success": True})


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "")
    results = search_torrents(query) if query else []
    return jsonify(results)


@app.route("/download/<int:torrent_id>")
def download(torrent_id):
    """Redirect to torrent download page."""
    return render_template("download.html", torrent_id=torrent_id, url=f"{TORRENT_URL}?id={torrent_id}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
