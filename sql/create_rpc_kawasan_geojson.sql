-- Jalankan sekali di Supabase SQL Editor (project: hotspot / meowtret's Project)
-- Fungsi ini membungkus kawasan_kph_simple jadi GeoJSON FeatureCollection
-- supaya bisa diambil langsung lewat REST API tanpa perlu library konversi WKB di Python.
--
-- Tidak mengubah kawasan_kph_simple maupun pipeline FIRMS-Hotspot sama sekali.

CREATE OR REPLACE FUNCTION get_kawasan_geojson()
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
  SELECT jsonb_build_object(
    'type', 'FeatureCollection',
    'features', COALESCE(jsonb_agg(
      jsonb_build_object(
        'type', 'Feature',
        'geometry', ST_AsGeoJSON(geom)::jsonb,
        'properties', jsonb_build_object(
          'id', id,
          'fid', fid,
          'kph', kph,
          'pbph', pbph,
          'provinsi', provinsi,
          'fungsi', fungsi
        )
      )
    ), '[]'::jsonb)
  )
  FROM kawasan_kph_simple
  WHERE geom IS NOT NULL;
$$;

GRANT EXECUTE ON FUNCTION get_kawasan_geojson() TO anon, authenticated, service_role;
