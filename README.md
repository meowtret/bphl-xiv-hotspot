# bphl-xiv-hotspot

Dashboard publik monitoring hotspot untuk wilayah kerja **BPHL Wilayah XIV**
(Sulawesi Tengah, Sulawesi Utara, Gorontalo), breakdown per **KPH**, **PBPH**,
dan **fungsi Kawasan Hutan**.

Repo ini **berdiri sendiri** — tidak menyentuh atau bergantung pada repo pipeline
`FIRMS-Hotspot`. Sumber datanya: NASA FIRMS (langsung) + Supabase (boundary saja).

## Cara kerja

1. **Boundary** (`data/boundaries.geojson`) diambil dari Supabase (`kawasan_kph_simple`)
   lewat RPC `get_kawasan_geojson()`. Hanya di-refresh manual (jarang berubah).
2. **Hotspot harian** (`data/hotspots.geojson`, `data/stats.json`) diambil langsung dari
   NASA FIRMS setiap hari, di-spatial-join ke boundary di atas, lalu di-reverse-geocode.
3. **Dashboard** (`index.html`) adalah halaman statis (Leaflet) yang baca kedua file
   GeoJSON itu — cocok untuk GitHub Pages.

## Setup awal (sekali saja)

1. **Buat repo baru** di GitHub bernama `bphl-xiv-hotspot`, push semua isi folder ini.

2. **Buat RPC di Supabase** — buka SQL Editor project `hotspot` (meowtret's Project),
   jalankan isi `sql/create_rpc_kawasan_geojson.sql`.

3. **Tambahkan GitHub Secrets** di repo baru (Settings → Secrets and variables → Actions):
   - `FIRMS_API_KEY` — API key NASA FIRMS
   - `SUPABASE_URL` — URL project Supabase (mis. `https://wzgecqmqrchavjeypnhh.supabase.co`)
   - `SUPABASE_API_KEY` — API key Supabase (anon atau service_role, tergantung yang dipakai
     di `FIRMS-Hotspot`; kalau service_role, sudah otomatis bisa akses tanpa perlu grant RLS)

4. **Jalankan workflow "Refresh Boundaries dari Supabase" secara manual** (tab Actions →
   pilih workflow → Run workflow). Ini mengisi `data/boundaries.geojson` untuk pertama kali.

5. **Jalankan workflow "Update Hotspot Dashboard" secara manual** sekali untuk tes
   (tab Actions → Run workflow, boleh isi `target_date` kalau mau tes tanggal tertentu
   yang ada datanya). Setelah itu workflow ini otomatis jalan tiap hari sesuai cron.

6. **Aktifkan GitHub Pages** — Settings → Pages → Source: `Deploy from a branch` →
   branch `main`, folder `/ (root)`. Dashboard akan tersedia di
   `https://<username>.github.io/bphl-xiv-hotspot/`.

## Struktur

```
.github/workflows/
  update-dashboard.yml       # harian: fetch FIRMS -> proses -> commit
  refresh-boundaries.yml     # manual: ambil ulang boundary dari Supabase
scripts/
  fetch_boundaries.py        # ambil boundary dari Supabase (RPC)
  build_dashboard_data.py    # ambil FIRMS, spatial join, reverse geocode
  requirements.txt
sql/
  create_rpc_kawasan_geojson.sql
data/
  boundaries.geojson         # digenerate, jangan edit manual
  hotspots.geojson           # digenerate, jangan edit manual
  stats.json                 # digenerate, jangan edit manual
index.html                  # dashboard
```

## Yang mungkin perlu disesuaikan

- **Jadwal cron** di `update-dashboard.yml` (default 01:00 UTC / 08:00 WITA) — geser
  kalau mau selaras persis dengan jadwal `FIRMS-Hotspot`.
- **Bbox FIRMS** (`DEFAULT_BBOX` di `build_dashboard_data.py`) — sudah longgar mencakup
  3 provinsi, tapi bisa dipersempit kalau mau mempercepat fetch.
- **Reverse geocoding** pakai Nominatim (OpenStreetMap), gratis tapi rate-limited
  1 request/detik — kalau jumlah titik harian sangat banyak (>300), runtime workflow
  bisa beberapa menit. Bisa diganti provider lain kalau perlu lebih cepat.
