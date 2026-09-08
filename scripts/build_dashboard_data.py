"""
build_dashboard_data.py

Jalan berkala (dipicu cron-job.org via workflow_dispatch, independen dari
FIRMS-Hotspot dan dari Supabase -- semua data boundary sekarang statis di
folder data/, format TopoJSON, di-upload manual).

Alur:
  1. Fetch titik hotspot VIIRS (NOAA-20 & NOAA-21) dari NASA FIRMS untuk tanggal target.
  2. Filter confidence: hanya Medium (nominal) & High -- Low dibuang.
     (Fungsi kawasan TIDAK difilter lagi -- APL & lainnya tetap ditampilkan.)
  3. Enrichment (LEFT join, bukan filter): tiap titik dicek masuk KPH mana,
     Fungsi Kawasan apa, dan PBPH siapa -- SEMUA titik tetap ditampilkan
     meski tidak masuk ke boundary manapun (field terkait cukup kosong "-").
  4. Reverse geocode tiap titik (desa/kec/kab/provinsi) via Nominatim OSM, dengan cache.
  5. Tulis data/hotspots.geojson (titik + properti untuk popup) dan data/stats.json (ringkasan).

Env vars (GitHub Secrets):
  - FIRMS_API_KEY
Env vars opsional:
  - TARGET_DATE   (format YYYY-MM-DD, default: hari ini UTC)
  - FIRMS_BBOX    (format "west,south,east,north", default lihat DEFAULT_BBOX di bawah)
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely import make_valid
from shapely.geometry import Point, Polygon, MultiPolygon
from shapely.ops import unary_union

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
HOTSPOTS_OUTPUT = DATA_DIR / "hotspots.geojson"
STATS_OUTPUT = DATA_DIR / "stats.json"
GEOCODE_CACHE_PATH = DATA_DIR / "geocode_cache.json"

TOPOJSON_FILES = {
    "kph": DATA_DIR / "kph_bphl.json",
    "pbph": DATA_DIR / "PBPH_PALU.json",
    "kws_gorontalo": DATA_DIR / "kws_gorontalo.json",
    "kws_sulteng": DATA_DIR / "kws_sulteng.json",
    "kws_sulut": DATA_DIR / "kws_sulut.json",
}

# Bbox longgar mencakup Sulteng + Sulut + Gorontalo (termasuk kepulauan Sangihe-Talaud).
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
            time.sleep(5 * attempt)

    text = resp.text.strip()
    if not text or text.lower().startswith(("invalid", "error")):
        print(f"  WARNING: respons tidak valid dari FIRMS untuk {source}: {text[:200]}")
        return pd.DataFrame()

    from io import StringIO
    df = pd.read_csv(StringIO(text))
    df["satellite_label"] = SATELLITES[source]
    print(f"  -> {len(df)} titik mentah")
    return df


# ---------------------------------------------------------------------------
# TopoJSON decoder (murni manual, tanpa dependency topojson pihak ketiga)
# ---------------------------------------------------------------------------

def _to_polygonal(geom):
    """Repair geometri sedikit-invalid (self-intersection, hole salah tempat --
    umum terjadi di data GIS hasil export/simplifikasi) dan pastikan hasilnya
    murni Polygon/MultiPolygon (buang artefak garis/titik dari proses repair)."""
    geom = make_valid(geom)
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        if not polys:
            return None
        return unary_union(polys)
    return None  # LineString/Point dsb -- dibuang


def load_topojson_layer(path: Path, property_map: dict) -> gpd.GeoDataFrame:
    """
    Decode 1 file TopoJSON (1 object) jadi GeoDataFrame.
    property_map: {"NAMA_KOLOM_ASLI": "nama_kolom_baru"}, mis. {"ORGANISASI": "kph"}
    """
    with open(path, encoding="utf-8") as f:
        topo = json.load(f)

    transform = topo.get("transform")
    scale = transform["scale"] if transform else [1, 1]
    translate = transform["translate"] if transform else [0, 0]

    def decode_arc(arc):
        x, y = 0, 0
        points = []
        for dx, dy in arc:
            x += dx
            y += dy
            points.append((translate[0] + scale[0] * x, translate[1] + scale[1] * y))
        return points

    arcs = [decode_arc(a) for a in topo["arcs"]]

    def resolve_arc(idx):
        return arcs[idx] if idx >= 0 else list(reversed(arcs[~idx]))

    def build_ring(arc_indices):
        coords = []
        for i, idx in enumerate(arc_indices):
            pts = resolve_arc(idx)
            coords.extend(pts if i == 0 else pts[1:])
        return coords

    def build_polygon(rings_idx):
        rings = [build_ring(r) for r in rings_idx]
        return Polygon(rings[0], rings[1:])

    obj = list(topo["objects"].values())[0]
    geometries = obj["geometries"]

    records, geoms = [], []
    for g in geometries:
        gtype = g["type"]
        if gtype == "Polygon":
            geom = build_polygon(g["arcs"])
        elif gtype == "MultiPolygon":
            geom = MultiPolygon([build_polygon(r) for r in g["arcs"]])
        else:
            continue

        geom = _to_polygonal(geom)
        if geom is None or geom.is_empty:
            continue

        props_raw = g.get("properties", {}) or {}
        props = {new: props_raw.get(old) for old, new in property_map.items()}
        records.append(props)
        geoms.append(geom)

    return gpd.GeoDataFrame(records, geometry=geoms, crs="EPSG:4326")


def enrich_with_boundary(points_gdf: gpd.GeoDataFrame, boundary_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """LEFT spatial join -- semua baris di points_gdf TETAP ada di hasil,
    kolom dari boundary_gdf jadi kosong (NaN) kalau titik tidak match ke
    polygon manapun. Kalau satu titik match >1 polygon (jarang, tepi
    berhimpit), ambil match pertama saja supaya jumlah baris tidak dobel."""
    result = gpd.sjoin(points_gdf, boundary_gdf, how="left", predicate="within")
    result = result[~result.index.duplicated(keep="first")]
    if "index_right" in result.columns:
        result = result.drop(columns=["index_right"])
    return result


def clean_value(v):
    """Sanitasi NaN/NaT dari pandas jadi None, supaya valid di-serialize JSON
    (JSON standar tidak mengenal literal NaN)."""
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


# ---------------------------------------------------------------------------
# Reverse geocode + cache
# ---------------------------------------------------------------------------

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


def format_acq_time(raw_time) -> str:
    """FIRMS menyimpan acq_time sebagai angka HHMM tanpa titik dua (mis. 444 = 04:44 UTC)."""
    if raw_time is None:
        return "-"
    try:
        padded = str(int(raw_time)).zfill(4)
        return f"{padded[:2]}:{padded[2:]} UTC"
    except (ValueError, TypeError):
        return str(raw_time)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    map_key = os.environ.get("FIRMS_API_KEY", "")
    if not map_key:
        print("ERROR: FIRMS_API_KEY wajib di-set sebagai env var.")
        sys.exit(1)

    missing = [name for name, path in TOPOJSON_FILES.items() if not path.exists()]
    if missing:
        print(f"ERROR: file topojson berikut belum ada di data/: {missing}")
        sys.exit(1)

    bbox = os.environ.get("FIRMS_BBOX", DEFAULT_BBOX)
    target_date = get_target_date()

    # 1. Fetch semua satelit
    frames = [
        fetch_firms_csv(map_key, source, bbox, target_date) for source in SATELLITES
    ]
    frames = [f for f in frames if not f.empty]

    if not frames:
        print("Tidak ada data hotspot hari ini dari FIRMS.")
        write_outputs(gpd.GeoDataFrame(columns=["geometry"]), target_date)
        return

    raw = pd.concat(frames, ignore_index=True)

    # 2. Filter confidence Medium & High saja (fungsi kawasan TIDAK difilter --
    #    APL dan lainnya tetap ditampilkan)
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
    ).reset_index(drop=True)

    # 3. Load boundary (TopoJSON statis) & enrichment lewat LEFT join
    print("Load boundary KPH, Kawasan Hutan, PBPH (topojson) ...")
    kph_gdf = load_topojson_layer(TOPOJSON_FILES["kph"], {"ORGANISASI": "kph"})
    pbph_gdf = load_topojson_layer(TOPOJSON_FILES["pbph"], {"NAMOBJ": "pbph"})
    kws_frames = [
        load_topojson_layer(TOPOJSON_FILES[key], {"F_KAW": "fungsi"})
        for key in ("kws_gorontalo", "kws_sulteng", "kws_sulut")
    ]
    kawasan_gdf = gpd.GeoDataFrame(
        pd.concat(kws_frames, ignore_index=True), geometry="geometry", crs="EPSG:4326"
    )
    print(f"  KPH: {len(kph_gdf)}, Kawasan Hutan: {len(kawasan_gdf)}, PBPH: {len(pbph_gdf)}")

    joined = enrich_with_boundary(points, kph_gdf[["kph", "geometry"]])
    joined = enrich_with_boundary(joined, kawasan_gdf[["fungsi", "geometry"]])
    joined = enrich_with_boundary(joined, pbph_gdf[["pbph", "geometry"]])
    print(f"Total titik ditampilkan (semua, tanpa filter boundary): {len(joined)}")

    if joined.empty:
        write_outputs(gpd.GeoDataFrame(columns=["geometry"]), target_date)
        return

    # 4. Reverse geocode -- pakai cache dulu, cuma panggil Nominatim untuk
    #    titik yang koordinatnya belum pernah di-geocode sebelumnya.
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
                    "kph": clean_value(getattr(row, "kph", None)),
                    "pbph": clean_value(getattr(row, "pbph", None)),
                    "fungsi": clean_value(getattr(row, "fungsi", None)),
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
