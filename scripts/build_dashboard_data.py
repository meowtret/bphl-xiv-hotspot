"""
build_dashboard_data.py

Jalan HARIAN (lewat GitHub Actions, jadwal independen dari FIRMS-Hotspot).
Alur:
  1. Fetch titik hotspot VIIRS (NOAA-20 & NOAA-21) dari NASA FIRMS untuk tanggal target.
  2. Filter confidence: hanya Medium (nominal) & High -- Low dibuang.
  3. Spatial join titik terhadap boundary (data/boundaries.geojson) -> dapat kph, pbph, fungsi.
  4. Buang titik yang fungsi kawasannya APL (konsisten dengan filter di pipeline FIRMS-Hotspot).
  5. Reverse geocode tiap titik (desa/kec/kab/provinsi) via Nominatim OSM, dengan cache.
  6. Tulis data/hotspots.geojson (titik + properti untuk popup) dan data/stats.json (ringkasan).

Env vars (GitHub Secrets):
  - FIRMS_API_KEY
Env vars opsional:
  - TARGET_DATE   (format YYYY-MM-DD, default: hari ini UTC)
  - FIRMS_BBOX    (format "west,south,east,north", default lihat DEFAULT_BBOX di bawah)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point

BASE_DIR = Path(__file__).resolve().parent.parent
BOUNDARIES_PATH = BASE_DIR / "data" / "boundaries.geojson"
HOTSPOTS_OUTPUT = BASE_DIR / "data" / "hotspots.geojson"
STATS_OUTPUT = BASE_DIR / "data" / "stats.json"
GEOCODE_CACHE_PATH = BASE_DIR / "data" / "geocode_cache.json"

# Bbox longgar mencakup Sulteng + Sulut + Gorontalo (termasuk kepulauan Sangihe-Talaud).
# Penyaringan presisi tetap terjadi lewat spatial join ke boundary, jadi bbox longgar aman.
# west, south, east, north
DEFAULT_BBOX = "119.0,-3.6,127.0,4.8"

SATELLITES = {
    "VIIRS_NOAA20_NRT": "NOAA-20",
    "VIIRS_NOAA21_NRT": "NOAA-21",
}

CONFIDENCE_MAP = {
    "h": "High",
    "n": "Medium",
    # "l" (low) sengaja tidak dimasukkan -> dibuang
}

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_HEADERS = {
    "User-Agent": "bphl-xiv-hotspot-dashboard/1.0 (monitoring hotspot BPHL Wilayah XIV)"
}


def get_target_date() -> str:
    override = os.environ.get("TARGET_DATE", "").strip()
    if override:
        return override
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fetch_firms_csv(map_key: str, source: str, bbox: str, date: str, max_retries: int = 3) -> pd.DataFrame:
    url = (
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
        f"{map_key}/{source}/{bbox}/1/{date}"
    )
    print(f"Fetch FIRMS {source} untuk {date} ...")

    resp = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            print(f"  WARNING: percobaan {attempt}/{max_retries} gagal ({e})")
            if attempt == max_retries:
                print(f"  ERROR: fetch {source} gagal setelah {max_retries} percobaan, dilewati.")
                return pd.DataFrame()
            time.sleep(5 * attempt)  # backoff: 5s, 10s, ...

    text = resp.text.strip()
    if not text or text.lower().startswith(("invalid", "error")):
        print(f"  WARNING: respons tidak valid dari FIRMS untuk {source}: {text[:200]}")
        return pd.DataFrame()

    from io import StringIO
    df = pd.read_csv(StringIO(text))
    df["satellite_label"] = SATELLITES[source]
    print(f"  -> {len(df)} titik mentah")
    return df

def load_geocode_cache() -> dict:
    if GEOCODE_CACHE_PATH.exists():
        try:
            with open(GEOCODE_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_geocode_cache(cache: dict) -> None:
    GEOCODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(GEOCODE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)


def geocode_cache_key(lat: float, lon: float) -> str:
    return f"{round(lat, 4)},{round(lon, 4)}"


def reverse_geocode(lat: float, lon: float) -> str:
    params = {
        "format": "jsonv2",
        "lat": lat,
        "lon": lon,
        "zoom": 14,
        "addressdetails": 1,
    }
    try:
        resp = requests.get(
            NOMINATIM_URL, params=params, headers=NOMINATIM_HEADERS, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        addr = data.get("address", {})

        desa = addr.get("village") or addr.get("hamlet") or addr.get("suburb")
        kec = addr.get("suburb") or addr.get("district") or addr.get("city_district")
        kab = (
            addr.get("county")
            or addr.get("regency")
            or addr.get("city")
            or addr.get("state_district")
        )
        prov = addr.get("state")

        parts = []
        if desa:
            parts.append(f"Desa {desa}")
        if kec and kec != desa:
            parts.append(f"Kec. {kec}")
        if kab:
            parts.append(kab)
        if prov:
            parts.append(prov)

        return ", ".join(parts) if parts else "Lokasi tidak diketahui"
    except Exception as e:
        print(f"  WARNING: reverse geocode gagal untuk ({lat},{lon}): {e}")
        return "Lokasi tidak diketahui"


def load_boundaries_geojson(path: Path) -> gpd.GeoDataFrame:
    """
    Load GeoJSON boundary manual pakai shapely.from_geojson (parser native
    Shapely 2.0), bukan gpd.read_file(). gpd.read_file() di beberapa kombinasi
    versi geopandas/shapely memakai parser lama (shapely.geometry.geo.shape())
    yang tidak stabil untuk MultiPolygon dengan ring kompleks/berlubang --
    persis kasus boundary KPH/PBPH/Kawasan Hutan yang jumlahnya ribuan polygon.
    """
    import shapely

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    geoms = []
    records = []
    for feat in raw.get("features", []):
        geom = shapely.from_geojson(json.dumps(feat["geometry"]))
        geoms.append(geom)
        records.append(feat.get("properties", {}) or {})

    return gpd.GeoDataFrame(records, geometry=geoms, crs="EPSG:4326")


def main() -> None:
    map_key = os.environ.get("FIRMS_API_KEY", "")
    if not map_key:
        print("ERROR: FIRMS_API_KEY wajib di-set sebagai env var.")
        sys.exit(1)

    if not BOUNDARIES_PATH.exists():
        print(
            f"ERROR: {BOUNDARIES_PATH} belum ada. Jalankan fetch_boundaries.py "
            "(workflow refresh-boundaries) dulu minimal sekali."
        )
        sys.exit(1)

    bbox = os.environ.get("FIRMS_BBOX", DEFAULT_BBOX)
    target_date = get_target_date()

    frames = [
        fetch_firms_csv(map_key, source, bbox, target_date) for source in SATELLITES
    ]
    frames = [f for f in frames if not f.empty]

    boundaries = load_boundaries_geojson(BOUNDARIES_PATH)

    if not frames:
        print("Tidak ada data hotspot hari ini dari FIRMS.")
        write_outputs(gpd.GeoDataFrame(columns=["geometry"]), target_date)
        return

    raw = pd.concat(frames, ignore_index=True)

    raw["confidence"] = raw["confidence"].astype(str).str.lower()
    raw = raw[raw["confidence"].isin(CONFIDENCE_MAP.keys())].copy()
    raw["confidence_level"] = raw["confidence"].map(CONFIDENCE_MAP)
    print(f"Setelah filter confidence Medium/High: {len(raw)} titik")

    if raw.empty:
        write_outputs(gpd.GeoDataFrame(columns=["geometry"]), target_date)
        return

    points = gpd.GeoDataFrame(
        raw,
        geometry=[Point(xy) for xy in zip(raw["longitude"], raw["latitude"])],
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(points, boundaries, how="inner", predicate="within")
    print(f"Setelah spatial join ke boundary BPHL XIV: {len(joined)} titik")

    if joined.empty:
        write_outputs(gpd.GeoDataFrame(columns=["geometry"]), target_date)
        return

    if "fungsi" in joined.columns:
        before = len(joined)
        joined = joined[joined["fungsi"].astype(str).str.upper() != "APL"].copy()
        print(f"Setelah buang fungsi APL: {len(joined)} titik (dari {before})")

    if joined.empty:
        write_outputs(gpd.GeoDataFrame(columns=["geometry"]), target_date)
        return

    # Reverse geocode -- pakai cache dulu, cuma panggil Nominatim untuk titik
    # yang koordinatnya belum pernah di-geocode sebelumnya. Penting kalau
    # workflow dijadwalkan sering (tiap 10 menit), supaya tidak membanjiri
    # Nominatim dengan request titik yang sama berulang-ulang.
    cache = load_geocode_cache()
    cache_hits = 0
    cache_misses = 0
    lokasi_list = []
    for i, row in enumerate(joined.itertuples(), start=1):
        key = geocode_cache_key(row.latitude, row.longitude)
        if key in cache:
            lokasi_list.append(cache[key])
            cache_hits += 1
        else:
            lokasi = reverse_geocode(row.latitude, row.longitude)
            cache[key] = lokasi
            lokasi_list.append(lokasi)
            cache_misses += 1
            time.sleep(1.1)  # rate limit Nominatim: 1 req/detik
        if i % 10 == 0:
            print(f"  Reverse geocode: {i}/{len(joined)} (cache hit: {cache_hits}, baru: {cache_misses})")
    joined["lokasi"] = lokasi_list
    save_geocode_cache(cache)
    print(f"Cache geocode: {cache_hits} hit, {cache_misses} request baru ke Nominatim")

    write_outputs(joined, target_date)


def format_acq_time(raw_time) -> str:
    """FIRMS menyimpan acq_time sebagai angka HHMM tanpa titik dua (mis. 444 = 04:44 UTC)."""
    if raw_time is None:
        return "-"
    try:
        padded = str(int(raw_time)).zfill(4)
        return f"{padded[:2]}:{padded[2:]} UTC"
    except (ValueError, TypeError):
        return str(raw_time)


def write_outputs(gdf: gpd.GeoDataFrame, target_date: str) -> None:
    features = []
    high_count = 0
    medium_count = 0

    for row in gdf.itertuples():
        conf = getattr(row, "confidence_level", None)
        if conf == "High":
            high_count += 1
        elif conf == "Medium":
            medium_count += 1

        raw_time = getattr(row, "acq_time", None)
        formatted_time = format_acq_time(raw_time)

        lat = getattr(row, "latitude", None)
        lon = getattr(row, "longitude", None)
        gmaps_url = (
            f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
            if lat is not None and lon is not None
            else None
        )

        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "satellite": getattr(row, "satellite_label", None),
                    "acq_date": getattr(row, "acq_date", None),
                    "acq_time": formatted_time,
                    "confidence_level": conf,
                    "confidence_raw": getattr(row, "confidence", None),
                    "lokasi": getattr(row, "lokasi", None),
                    "latitude": lat,
                    "longitude": lon,
                    "kph": getattr(row, "kph", None),
                    "pbph": getattr(row, "pbph", None),
                    "fungsi": getattr(row, "fungsi", None),
                    "gmaps_url": gmaps_url,
                },
            }
        )

    hotspots_geojson = {"type": "FeatureCollection", "features": features}
    HOTSPOTS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(HOTSPOTS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(hotspots_geojson, f, ensure_ascii=False)

    stats = {
        "high": high_count,
        "medium": medium_count,
        "total": high_count + medium_count,
        "target_date": target_date,
        "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(STATS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print(f"OK: {len(features)} titik ditulis. Stats: {stats}")


if __name__ == "__main__":
    main()
