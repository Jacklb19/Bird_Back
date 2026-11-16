"""Compare map index expressions on synthetic data in a loopback-only temporary table."""
import json
import os
from statistics import median
from urllib.parse import urlsplit

import psycopg

QUERY = """SELECT count(*) FROM map_index_sample WHERE ST_Intersects(location::geometry,
    ST_MakeEnvelope(-74.01, 1.59, -73.99, 1.61, 4326))"""


def main() -> None:
    url = os.environ.get("BIRDNET_TEST_DATABASE_URL", "")
    if urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("A loopback BIRDNET_TEST_DATABASE_URL is required")
    results = {}
    with psycopg.connect(url) as connection:
        connection.execute("""CREATE TEMP TABLE map_index_sample AS
            SELECT ST_SetSRID(ST_MakePoint(-78 + (i % 1000) * 0.01, 1 + (i / 1000) * 0.01), 4326)::geography AS location
            FROM generate_series(1, 100000) i""")
        connection.execute("CREATE INDEX ON map_index_sample USING gist (location)")
        connection.execute("ANALYZE map_index_sample")
        for name in ("geography_index", "matching_geometry_index"):
            if name == "matching_geometry_index":
                connection.execute("CREATE INDEX map_sample_geometry ON map_index_sample USING gist ((location::geometry))")
                connection.execute("ANALYZE map_index_sample")
            plans = [connection.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {QUERY}").fetchone()[0][0] for _ in range(3)]
            results[name] = {"median_ms": round(median(plan["Execution Time"] for plan in plans), 3), "plan": plans[-1]["Plan"]}
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
