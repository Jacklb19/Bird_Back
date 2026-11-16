-- The map filters the geometry projection, so its GiST index must match that expression.
-- Retain the geography index used by distance queries.
CREATE INDEX IF NOT EXISTS detections_location_geometry_gist
ON public.detections USING gist ((ubicacion::geometry));
