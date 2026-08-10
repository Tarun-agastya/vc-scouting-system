#!/bin/bash
# Drives scripts/rc_footprints.py to completion despite Overpass's ~1-in-3
# 504 rate: rc_footprints.py is already incremental (only queries rows with
# footprint_m2 IS NULL), so simply re-running it until nothing is left
# unmeasured converges — each pass picks up wherever the last one stopped.
set -e
cd "$(dirname "$0")/.."

for i in $(seq 1 12); do
  echo "=== footprint pass $i ==="
  python3 -u scripts/rc_footprints.py --apply --batch 40
  remaining=$(python3 -c "
from database.connection import SessionLocal
from database.models import RegionalCompany
db=SessionLocal()
n=db.query(RegionalCompany).filter(RegionalCompany.in_radius==True,
    RegionalCompany.source=='osm', RegionalCompany.footprint_m2.is_(None)).count()
print(n)
db.close()")
  echo "  unmeasured OSM rows remaining: $remaining"
  [ "$remaining" -eq 0 ] && break
  sleep 5
done

echo
echo "=== promoting large footprints, then enriching everything now in the queue ==="
python3 -u scripts/rc_enrich.py --limit 1000 --apply
