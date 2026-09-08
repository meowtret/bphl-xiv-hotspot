# bphl-xiv-hotspot

Dashboard publik monitoring hotspot untuk wilayah kerja **BPHL Wilayah XIV**
(Sulawesi Tengah, Sulawesi Utara, Gorontalo), breakdown per **KPH**, **PBPH**,
dan **Fungsi Kawasan Hutan**.

Repo ini **berdiri sendiri** — tidak menyentuh atau bergantung pada repo pipeline
`FIRMS-Hotspot`, dan **tidak lagi menggunakan Supabase** (semua data boundary
statis, format TopoJSON, di-upload langsung ke repo).

## Cara kerja

1. **Boundary** (`data/kph_bphl.json`, `data/PBPH_PALU.json`, `data/kws_*.json`)
   adalah file TopoJSON statis — tidak pernah berubah otomatis, cuma diganti
   manual (upload ulang) kalau ada update data dari sumbernya.
2. **Hotspot** (`data/hotspots.geojson`, `data/stats.json`) diambil langsung dari
   NASA FIRMS secara berkala (dipicu lewat cron-job.org, bukan cron GitHub --
   lihat catatan di bawah), lalu **semua titik ditampilkan** (tidak difilter
   berdasarkan boundary atau fungsi kawasan sama sekali, termasuk APL) dan
   diperkaya info KPH/PBPH/Fungsi Kawasan lewat spatial join (kalau match) dan
   reverse-geocode lokasi.
3. **Dashboard** (`index.html`) adalah halaman statis (Leaflet + topojson-client)
   yang baca semua file di atas -- cocok untuk GitHub Pages.

## Struktur

```
.github/workflows/
  update-dashboard.yml       # ambil FIRMS, enrich boundary, reverse geocode, commit
scripts/
  build_dashboard_data.py    # semua logic: fetch FIRMS, decode topojson, spatial join, geocode
  requirements.txt
data/
  kph_bphl.json               # boundary 25 unit KPH (statis, upload manual)
  PBPH_PALU.json               # boundary 10 PBPH (statis, upload manual)
  kws_gorontalo.json           # fungsi kawasan hutan per provinsi (statis, upload manual)
  kws_sulteng.json
  kws_sulut.json
  hotspots.geojson             # digenerate otomatis, jangan edit manual
  stats.json                   # digenerate otomatis, jangan edit manual
  geocode_cache.json           # digenerate otomatis (cache reverse geocode)
index.html                    # dashboard
```

## Setup awal (sekali saja)

1. Push semua isi folder ini (termasuk 5 file topojson di `data/`) ke repo.
2. Tambahkan GitHub Secret: `FIRMS_API_KEY` (API key NASA FIRMS).
3. Trigger workflow "Update Hotspot Dashboard" manual sekali untuk tes
   (tab Actions -> Run workflow).
4. Aktifkan GitHub Pages (Settings -> Pages -> branch main, folder root).

## Update otomatis berkala

Cron bawaan GitHub Actions tidak reliable untuk interval pendek (di bawah
~1 jam). Solusinya: pakai layanan eksternal gratis seperti cron-job.org
yang memanggil GitHub API (workflow_dispatch) tiap beberapa menit.

## Kalau boundary perlu diperbarui

File-file di data/*.json (topojson) itu statis -- kalau sumber datanya
berubah, tinggal generate ulang file topojson dari sumbernya lalu upload
ulang (timpa file lama) ke folder data/. Tidak ada proses otomatis untuk ini.
