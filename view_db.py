# view_db.py
import sqlite3, csv
from pathlib import Path

DB = Path("mytimetable.db")
con = sqlite3.connect(DB)
cur = con.cursor()

print("\nCourses saved (sample 10):")
for row in cur.execute("""
  SELECT code||'-'||number, title, term_label
  FROM courses
  ORDER BY rowid DESC
  LIMIT 10
"""):
    print("  ", row)

print("\nBlocks (sample 10):")
for row in cur.execute("""
  SELECT c.code||'-'||c.number AS course,
         b.block_type, b.sec_no, COALESCE(b.room, ''), b.day_name, b.start_time, b.end_time
  FROM blocks b
  JOIN selections s ON s.selection_key = b.selection_key
  JOIN courses    c ON c.course_key    = s.course_key
  ORDER BY b.id DESC
  LIMIT 10
"""):
    print("  ", row)

# optional: export to CSV
with open("blocks_export.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["course","block_type","sec_no","room","day","start","end"])
    for row in cur.execute("""
      SELECT c.code||'-'||c.number, b.block_type, b.sec_no, b.room, b.day_name, b.start_time, b.end_time
      FROM blocks b
      JOIN selections s ON s.selection_key = b.selection_key
      JOIN courses    c ON c.course_key    = s.course_key
      ORDER BY c.code, c.number, b.day_num, b.start_min
    """):
        w.writerow(row)

con.close()
print("\nWrote CSV -> blocks_export.csv")
