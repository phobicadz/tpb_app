from flask import Flask, render_template, request, jsonify
from urllib.parse import quote_plus
from datetime import datetime, timezone
import os
import threading
import requests

app = Flask(__name__)

API_URL = "https://apibay.org/q.php"
DETAILS_URL = "https://thepiratebay.org/description.php"
TORRENT_URL = "https://thepiratebay.org/torrent.php"

# qBittorrent defaults (override with env vars QB_URL / QB_USERNAME / QB_PASSWORD)
QB_URL = os.environ.get("QB_URL", "http://desktop.mancave:8080")
QB_USERNAME = os.environ.get("QB_USERNAME", "admin")
QB_PASSWORD = os.environ.get("QB_PASSWORD", "weasel")

# apibay category filter options (numeric IDs are the only ones the API honors)
QB_CATEGORIES = [
    ("", "All categories"),
    ("100", "Audio"),
    ("101", "Music"),
    ("102", "Audiobooks"),
    ("104", "Lossless Music"),
    ("200", "Video"),
    ("201", "Movies"),
    ("207", "Movies HD"),
    ("208", "TV HD"),
    ("300", "Applications"),
    ("301", "Apps - Windows"),
    ("302", "Apps - Mac"),
    ("303", "Apps - Linux"),
    ("400", "Games"),
    ("401", "Games - PC"),
    ("600", "Books"),
    ("601", "EBooks"),
    ("602", "Comics"),
]

# Human-readable names for category tags; falls back to the top-level group.
CATEGORY_NAMES = {
    "101": "Music", "102": "Audiobooks", "104": "Lossless Music", "199": "Other (Audio)",
    "201": "Movies", "207": "Movies HD", "208": "TV HD", "299": "Other (Video)",
    "301": "Windows Apps", "302": "Mac Apps", "303": "Linux Apps", "399": "Other Apps",
    "401": "PC Games", "499": "Other Games",
    "501": "XXX", "599": "Other",
    "601": "EBooks", "602": "Comics",
}
CATEGORY_GROUPS = {"1": "Audio", "2": "Video", "3": "Applications",
                   "4": "Games", "5": "Other", "6": "Books"}


def category_label(cat_id):
    cat_id = str(cat_id)
    if cat_id in CATEGORY_NAMES:
        return CATEGORY_NAMES[cat_id]
    return CATEGORY_GROUPS.get(cat_id[:1], cat_id)


class QBittorrentClient:
    """qBittorrent Web API client with cookie session and CSRF token handling.

    qBittorrent >= 4.6 requires an X-CSRFTOKEN header on POST requests, and
    /api/v2/torrents/add returns HTTP 200 with an empty body on success.
    """

    def __init__(self):
        self.session = None
        self.csrf_token = None
        self.credentials = None  # (base_url, username, password) currently logged in
        # Serializes requests on this client so a 403 re-login or session reset
        # in one request cannot corrupt another request in flight.
        self._lock = threading.Lock()

    def login(self, base_url, username, password):
        """Authenticate and cache the CSRF token. Returns True on success."""
        session = requests.Session()
        try:
            resp = session.post(
                f"{base_url}/api/v2/auth/login",
                data={"username": username, "password": password},
                timeout=10,
            )
        except requests.RequestException:
            return False
        # qBittorrent 4.6.x replies "Ok." (and "Fails." for bad credentials);
        # 5.x replies 200 with an empty body on success.
        body = resp.text.strip()
        if resp.status_code != 200 or body not in ("", "Ok."):
            return False

        # qBittorrent >= 4.6 exposes the CSRF token via authInfo; older
        # versions use the sid cookie value as the token.
        csrf_token = None
        try:
            info = session.get(f"{base_url}/api/v2/auth/authInfo", timeout=10)
            if info.status_code == 200:
                csrf_token = info.json().get("csrf_token")
        except (requests.RequestException, ValueError):
            pass
        if not csrf_token:
            csrf_token = session.cookies.get("sid")

        self.session = session
        self.csrf_token = csrf_token
        self.credentials = (base_url, username, password)
        return True

    def _ensure_login(self, base_url, username, password):
        if self.credentials != (base_url, username, password) or self.session is None:
            return self.login(base_url, username, password)
        return True

    def _post(self, path, base_url, username, password, data):
        """POST with CSRF header; re-login and retry once on 403 (expired session)."""
        if not self._ensure_login(base_url, username, password):
            return None, "login failed"
        headers = {"X-CSRFTOKEN": self.csrf_token} if self.csrf_token else {}
        try:
            resp = self.session.post(
                f"{base_url}{path}", data=data, headers=headers, timeout=10
            )
            if resp.status_code == 403:
                # Session or CSRF expired: re-login once and retry.
                if not self.login(base_url, username, password):
                    return None, "login failed"
                headers = {"X-CSRFTOKEN": self.csrf_token} if self.csrf_token else {}
                resp = self.session.post(
                    f"{base_url}{path}", data=data, headers=headers, timeout=10
                )
        except requests.RequestException as e:
            self.session = None
            self.credentials = None
            return None, f"connection error: {e}"
        if resp.status_code == 200:
            return resp, None
        self.session = None
        self.credentials = None
        return None, f"HTTP {resp.status_code}: {resp.text[:200] or 'no response body'}"

    def add_torrent(self, base_url, username, password, magnet):
        with self._lock:
            resp, error = self._post("/api/v2/torrents/add", base_url, username, password,
                                     {"urls": magnet})
            if error:
                return False, error
            return True, None

    def check_connection(self, base_url, username, password):
        """Login and read the qBittorrent version to confirm the API works."""
        with self._lock:
            if not self._ensure_login(base_url, username, password):
                return False, "login failed"
            try:
                resp = self.session.get(f"{base_url}/api/v2/app/version", timeout=10)
            except requests.RequestException as e:
                self.session = None
                self.credentials = None
                return False, f"connection error: {e}"
            if resp.status_code != 200:
                self.session = None
                self.credentials = None
                return False, f"HTTP {resp.status_code}"
            return True, resp.text.strip()


# One client per credential set, so concurrent requests from different browsers
# cannot clobber each other's session/CSRF state. Same credentials reuse the
# same logged-in client (no re-login per request).
_qb_clients = {}
_qb_clients_lock = threading.Lock()


def get_qb_client(base_url, username, password):
    key = (base_url, username, password)
    with _qb_clients_lock:
        client = _qb_clients.get(key)
        if client is None:
            client = QBittorrentClient()
            _qb_clients[key] = client
        return client


def resolve_qb_target(data):
    """Resolve url/credentials for a request, preventing credential leaks.

    Server-side default credentials (QB_USERNAME/QB_PASSWORD) are only ever
    sent to QB_URL. A caller-supplied url requires explicit credentials,
    otherwise an unauthenticated caller could leak the secrets to any host.
    Returns (base_url, username, password, error); error is None on success.
    """
    url = (data.get("url") or "").strip()
    username = data.get("username") or ""
    password = data.get("password") or ""
    if not username or not password:
        if url and url != QB_URL:
            return None, None, None, (
                "a custom qBittorrent URL requires an explicit username and password"
            )
        return QB_URL, QB_USERNAME, QB_PASSWORD, None
    return url or QB_URL, username, password, None


def format_size(size_bytes):
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = float(size_bytes)
    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def search_torrents(title, category="", top_n=40):
    params = {"q": quote_plus(title), "cat": category}
    try:
        resp = requests.get(API_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data[:top_n]:
            timestamp = int(item.get("added", 0) or 0)
            results.append({
                "id": item["id"],
                "name": item["name"],
                "size": format_size(int(item["size"])),
                "size_bytes": int(item["size"]),
                "seeders": int(item["seeders"]),
                "leechers": int(item["leechers"]),
                "uploaded": datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d") if timestamp else "",
                "uploaded_ts": timestamp,
                "category": category_label(item.get("category", "")),
                "info_hash": item.get("info_hash", ""),
                "url": f"{DETAILS_URL}?id={item['id']}",
                "download": f"{TORRENT_URL}?id={item['id']}",
                "magnet": f"magnet:?xt=urn:btih:{item.get('info_hash', '')}" if item.get("info_hash") else None,
            })
        return results
    except Exception:
        return []


@app.route("/")
def index():
    return render_template("index.html", categories=QB_CATEGORIES,
                          qb_url=QB_URL, qb_username=QB_USERNAME)


@app.route("/search", methods=["GET"])
def search():
    query = request.args.get("q", "")
    category = request.args.get("cat", "")
    top_n = request.args.get("n", 40, type=int)
    results = search_torrents(query, category, top_n) if query else []
    return render_template("results.html", query=query, category=category,
                          category_name=category_label(category) if category else "",
                          categories=QB_CATEGORIES, results=results, count=len(results),
                          qb_url=QB_URL, qb_username=QB_USERNAME)


@app.route("/api/qbittorrent/config")
def qb_config():
    """Defaults for the UI settings panel (password intentionally omitted)."""
    return jsonify({"url": QB_URL, "username": QB_USERNAME})


@app.route("/api/qbittorrent/test", methods=["POST"])
def qb_test_api():
    """Check that the UI can reach and authenticate with qBittorrent."""
    data = request.get_json(silent=True) or {}
    base_url, username, password, error = resolve_qb_target(data)
    if error:
        return jsonify({"success": False, "error": error}), 400
    ok, info = get_qb_client(base_url, username, password).check_connection(base_url, username, password)
    if not ok:
        return jsonify({"success": False, "error": info}), 502
    return jsonify({"success": True, "version": info})


@app.route("/api/qbittorrent/add", methods=["POST"])
def qb_add_api():
    """Proxy endpoint to add a torrent (or several) to qBittorrent."""
    data = request.get_json(silent=True) or {}
    base_url, username, password, error = resolve_qb_target(data)
    if error:
        return jsonify({"success": False, "error": error}), 400
    magnets = data.get("magnets") or ([data["magnet"]] if data.get("magnet") else [])
    if not magnets:
        return jsonify({"success": False, "error": "no magnet provided"}), 400

    errors = []
    sent = 0
    client = get_qb_client(base_url, username, password)
    for magnet in magnets:
        ok, error = client.add_torrent(base_url, username, password, magnet)
        if ok:
            sent += 1
        else:
            errors.append(error)
    if sent == 0:
        status = 502 if any("connection error" in e or "login failed" in e for e in errors) else 500
        return jsonify({"success": False, "error": errors[0]}), status
    return jsonify({"success": True, "sent": sent, "errors": errors})


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "")
    category = request.args.get("cat", "")
    results = search_torrents(query, category) if query else []
    return jsonify(results)


@app.route("/download/<int:torrent_id>")
def download(torrent_id):
    """Redirect to torrent download page."""
    return render_template("download.html", torrent_id=torrent_id, url=f"{TORRENT_URL}?id={torrent_id}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
