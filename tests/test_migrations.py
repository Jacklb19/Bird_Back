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
