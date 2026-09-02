"""
fetch_boundaries.py

Mengambil boundary KPH/PBPH/Kawasan Hutan dari Supabase (view kawasan_kph_simple,
lewat RPC get_kawasan_geojson) dan menyimpannya sebagai data/boundaries.geojson.

Ini TIDAK berjalan tiap hari -- hanya dijalankan manual (workflow_dispatch) saat
boundary di Supabase berubah / di-refresh. Data hotspot harian tidak menyentuh file ini.

Env vars yang dibutuhkan (di-set sebagai GitHub Secrets):
- SUPABASE_URL
- SUPABASE_API_KEY
"""

import json
import os
import sys
from pathlib import Path

import requests

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "boundaries.geojson"


def main() -> None:
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_API_KEY", "")

    if not supabase_url or not supabase_key:
        print("ERROR: SUPABASE_URL dan SUPABASE_API_KEY wajib di-set sebagai env var.")
        sys.exit(1)

    rpc_url = f"{supabase_url}/rest/v1/rpc/get_kawasan_geojson"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }

    print(f"Mengambil boundary dari {rpc_url} ...")
    resp = requests.post(rpc_url, headers=headers, json={}, timeout=60)
    resp.raise_for_status()
    geojson = resp.json()

    if geojson.get("type") != "FeatureCollection":
        print("ERROR: Respons RPC bukan FeatureCollection yang valid. Cek apakah "
              "fungsi get_kawasan_geojson() sudah dibuat di Supabase "
              "(lihat sql/create_rpc_kawasan_geojson.sql).")
        sys.exit(1)

    n_features = len(geojson.get("features", []))
    if n_features == 0:
        print("WARNING: 0 features diterima. Cek grant/RLS pada fungsi RPC.")
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False)

    print(f"OK: {n_features} boundary polygon disimpan ke {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
