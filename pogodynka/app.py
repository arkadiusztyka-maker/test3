#!/usr/bin/env python3
"""Paczkomat jako prywatna pogodynka.

Mały serwer (tylko biblioteka standardowa Pythona 3.8+), który:
  * znajduje paczkomaty InPost z czujnikami (temperatura, wilgotność, ciśnienie, PM),
  * pobiera z nich odczyty przez nieoficjalny endpoint inpost.pl,
  * wystawia je jako prosty JSON (np. dla Home Assistant) i jako stronę WWW na telefon.

Uruchomienie:
    python3 app.py                 # http://localhost:8080
    PORT=9000 python3 app.py

Jak to działa:
  1. ShipX API (publiczne, bez klucza) -> dane paczkomatu i lista najbliższych;
     czujnik ma ten, który ma ustawione pole air_index_level.
  2. Sitemapa inpost.pl (/sitemap/points/N.xml) -> adres strony paczkomatu.
  3. Strona paczkomatu -> atrybut data-shipx-url z wewnętrznym numerycznym ID.
  4. POST inpost.pl/shipx-point-data/<ID>/<KOD>/air_index_level
     z nagłówkiem X-Requested-With: XMLHttpRequest -> JSON z odczytami.
     Bez POST i nagłówka dostaje się tylko przekierowanie na mapę paczkomatów.

To nie jest oficjalne API - odczyty są cache'owane (domyślnie 30 min),
żeby nie męczyć serwerów InPostu. Czujniki i tak nie raportują częściej.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHIPX = os.environ.get("SHIPX_URL", "https://api-shipx-pl.easypack24.net/v1/points")
INPOST = os.environ.get("INPOST_URL", "https://inpost.pl")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
READING_TTL = int(os.environ.get("READING_TTL", "1800"))
SITEMAP_MAX_AGE = 7 * 24 * 3600
CACHE_DIR = os.environ.get(
    "CACHE_DIR",
    os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "paczkomat-pogodynka"),
)
SITEMAP_CACHE = os.path.join(CACHE_DIR, "sitemap-urls.txt")
IDS_CACHE = os.path.join(CACHE_DIR, "ids.json")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
UA = "Mozilla/5.0 (paczkomat-pogodynka)"

LEVELS = {
    "VERY_GOOD": "bardzo dobra",
    "GOOD": "dobra",
    "SATISFACTORY": "umiarkowanie dobra",
    "MODERATE": "umiarkowana",
    "BAD": "zła",
    "VERY_BAD": "bardzo zła",
}
# Nazwy czujników z inpost.pl -> klucze w naszym JSON-ie.
SENSOR_KEYS = {
    "TEMPERATURE": "temperature",
    "HUMIDITY": "humidity",
    "PRESSURE": "pressure",
    "PM1": "pm1",
    "PM25": "pm25",
    "PM4": "pm4",
    "PM10": "pm10",
}
CODE_RE = re.compile(r"^[A-Z0-9]{3,16}$")


class NotFound(Exception):
    pass


def http(url, method="GET", headers=None, timeout=20):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


# --- ShipX -----------------------------------------------------------------

def _point_summary(p):
    addr = p.get("address") or {}
    return {
        "code": p.get("name"),
        "address": ", ".join(x for x in (addr.get("line1"), addr.get("line2")) if x),
        "description": p.get("location_description") or "",
        "distance": p.get("distance"),
        "has_sensor": bool(p.get("air_index_level")),
        "air_index_level": p.get("air_index_level"),
        "location": p.get("location"),
    }


def shipx_point(code):
    data = json.loads(http(f"{SHIPX}/{urllib.parse.quote(code)}"))
    if "location" not in data:  # ShipX zwraca błąd w treści, czasem z kodem 200
        raise NotFound(f"Nie znaleziono paczkomatu {code}")
    return data


def shipx_nearby(lat, lon, limit=500):
    query = urllib.parse.urlencode({
        "relative_point": f"{lat},{lon}",
        "limit": limit,
        "type": "parcel_locker",
        "fields": "name,air_index_level,distance,address,location_description,location",
    })
    return json.loads(http(f"{SHIPX}?{query}")).get("items") or []


# --- numeryczne ID punktu ----------------------------------------------------

_lock = threading.Lock()
_page_index = None
_ids = None


def _load_ids():
    global _ids
    if _ids is None:
        try:
            with open(IDS_CACHE) as f:
                _ids = json.load(f)
        except (OSError, ValueError):
            _ids = {}
    return _ids


def _save_ids():
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(IDS_CACHE, "w") as f:
        json.dump(_ids, f, indent=1, sort_keys=True)


def _sitemap_urls():
    fresh = os.path.exists(SITEMAP_CACHE) and time.time() - os.path.getmtime(SITEMAP_CACHE) < SITEMAP_MAX_AGE
    if not fresh:
        print("Pobieram sitemapę paczkomatów z inpost.pl (raz na tydzień)...", file=sys.stderr)
        urls = []
        for i in range(1, 200):
            try:
                xml = http(f"{INPOST}/sitemap/points/{i}.xml")
            except urllib.error.HTTPError:
                break
            found = re.findall(r"<loc>([^<]+)</loc>", xml)
            if not found:
                break
            urls += found
        if not urls:
            raise RuntimeError("Nie udało się pobrać sitemapy inpost.pl")
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(SITEMAP_CACHE, "w") as f:
            f.write("\n".join(urls))
    with open(SITEMAP_CACHE) as f:
        return f.read().split()


def _page_for(code):
    """Kod paczkomatu jest jednym z członów sluga strony, np. paczkomat-wroclaw-wro358m-..."""
    global _page_index
    if _page_index is None:
        idx = {}
        for u in _sitemap_urls():
            for token in u.rstrip("/").rsplit("/", 1)[-1].split("-"):
                if token:
                    idx.setdefault(token, u)
        _page_index = idx
    return _page_index.get(code.lower())


def resolve_id(code):
    with _lock:
        ids = _load_ids()
        if code in ids:
            return ids[code]
        url = _page_for(code)
    if not url:
        raise NotFound(f"Brak strony paczkomatu {code} na inpost.pl")
    m = None
    for attempt in range(3):
        try:
            m = re.search(r'data-shipx-url="/shipx-point-data/(\d+)/', http(url))
            break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1 + attempt)
    if not m:
        raise NotFound(f"Strona paczkomatu {code} nie zawiera ID (prawdopodobnie brak czujników)")
    with _lock:
        _ids[code] = int(m.group(1))
        _save_ids()
    return _ids[code]


def remember_id(code, point_id):
    with _lock:
        _load_ids()[code] = int(point_id)
        _save_ids()


# --- odczyty -------------------------------------------------------------------

def _num(s):
    try:
        return float(str(s).replace(",", "."))
    except ValueError:
        return None


def parse_reading(data):
    """JSON z inpost.pl -> płaski słownik. air_sensors to lista "NAZWA:wartość:%normy"."""
    sensors = {}
    for item in data.get("air_sensors") or []:
        if isinstance(item, dict):
            name, value, pct = item.get("name"), item.get("value"), item.get("percent")
        else:
            name, value, pct = (str(item).split(":") + ["", ""])[:3]
        name = re.sub(r"[^A-Z0-9]", "", str(name).upper())
        if name:
            sensors[name] = {"value": _num(value), "percent": _num(pct)}
    level = data.get("air_index_level")
    out = {
        "air_index_level": level,
        "air_quality": LEVELS.get(level, level),
        "sensors": sensors,
    }
    for raw, key in SENSOR_KEYS.items():
        out[key] = sensors.get(raw, {}).get("value")
    return out


_readings = {}


def reading(code, point_id=None):
    cached = _readings.get(code)
    if cached and time.time() - cached["_ts"] < READING_TTL:
        return {k: v for k, v in cached.items() if k != "_ts"}
    if point_id:
        remember_id(code, point_id)
    pid = resolve_id(code)
    try:
        raw = http(f"{INPOST}/shipx-point-data/{pid}/{code}/air_index_level", method="POST",
                   headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    except urllib.error.HTTPError as e:
        if e.code == 404:  # {"message":"Air sensors are not available."}
            raise NotFound(f"Paczkomat {code} nie udostępnia odczytów")
        raise
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError("inpost.pl zwrócił coś innego niż JSON (może zmienili endpoint?)")
    out = parse_reading(data)
    if not out["sensors"]:
        raise NotFound(f"Paczkomat {code} nie ma odczytów z czujników")
    now = time.time()
    out.update(code=code, id=pid, updated=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)))
    _readings[code] = {**out, "_ts": now}
    return out


def nearby(code=None, lat=None, lon=None, n=15):
    origin = None
    if code:
        p = shipx_point(code)
        origin = _point_summary(p)
        lat, lon = p["location"]["latitude"], p["location"]["longitude"]
    items = [_point_summary(p) for p in shipx_nearby(lat, lon)]
    return {"origin": origin, "items": [p for p in items if p["has_sensor"]][:n]}


# --- HTTP ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PaczkomatPogodynka/1.0"

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        parts = [p for p in url.path.split("/") if p]
        try:
            if not parts:
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if parts == ["api", "nearby"]:
                code = q.get("code", "").strip().upper()
                if code:
                    if not CODE_RE.match(code):
                        return self._send(400, {"error": "Niepoprawny kod paczkomatu"})
                    return self._send(200, nearby(code=code, n=int(q.get("n", 15))))
                lat, lon = _num(q.get("lat", "")), _num(q.get("lon", ""))
                if lat is None or lon is None:
                    return self._send(400, {"error": "Podaj code albo lat i lon"})
                return self._send(200, nearby(lat=lat, lon=lon, n=int(q.get("n", 15))))
            if len(parts) == 3 and parts[:2] == ["api", "reading"]:
                code = parts[2].upper()
                if not CODE_RE.match(code):
                    return self._send(400, {"error": "Niepoprawny kod paczkomatu"})
                pid = q.get("id")
                if pid and not pid.isdigit():
                    return self._send(400, {"error": "ID musi być liczbą"})
                return self._send(200, reading(code, pid))
            return self._send(404, {"error": "Nie ma takiej strony"})
        except NotFound as e:
            return self._send(404, {"error": str(e)})
        except Exception as e:  # błędy sieci, zmiany po stronie InPostu itp.
            return self._send(502, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    print(f"Pogodynka działa na http://{HOST}:{PORT}", file=sys.stderr)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
