"""The anonymous key is only safe if every table enforces RLS; check it statically on each migration."""
import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
CREATE = re.compile(r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?(\w+)\s*\(", re.I)
ENABLE = re.compile(r"alter\s+table\s+(?:public\.)?(\w+)\s+enable\s+row\s+level\s+security\s*;", re.I)


def test_every_created_table_enables_row_level_security():
    sql = "\n".join(path.read_text(encoding="utf-8-sig") for path in sorted(MIGRATIONS.glob("*.sql")))
    created = set(CREATE.findall(sql))
    assert created, "No migrations found"
    assert created <= set(ENABLE.findall(sql))


BUCKET = re.compile(r"insert\s+into\s+storage\.buckets\s*\(([^)]*)\)\s*values\s*\(([^)]*)\)", re.I)


def test_every_created_bucket_is_private():
    # Objects are reached only through URLs the API signs for their owner; a public bucket would bypass that.
    sql = "\n".join(path.read_text(encoding="utf-8-sig") for path in sorted(MIGRATIONS.glob("*.sql")))
    buckets = [dict(zip((c.strip() for c in columns.split(",")), (v.strip() for v in values.split(",")))) for columns, values in BUCKET.findall(sql)]
    assert buckets, "No buckets found"
    assert all(bucket["public"].lower() == "false" for bucket in buckets)
