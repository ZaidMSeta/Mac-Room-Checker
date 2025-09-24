# extract_codes.py
import csv, re

# Example matches: "MATH 1ZB3", "COMPENG 2AA3", "CIVENG 3G03"
# - Subject: 2+ uppercase letters (allow & for e.g., weird joint subjects)
# - Catalog: digit + 2 alnum + digit (McMaster style like 1ZB3, 2AA3, 3G03)
CODE_LINE_RE = re.compile(r'^\s*([A-Z&]{2,})\s+(\d[A-Z0-9]{2}\d)\b')

codes = set()
rejected = []

with open('course_names.csv', newline='', encoding='utf-8') as f:
    reader = csv.reader(f)
    # Skip header if present
    first = next(reader, None)
    if first and first[0].strip().lower() != 'course name':
        # process this row too
        reader = ([first] for _ in [0]) if first else reader  # put it back
        # (quick trick to re-iterate first row)
        reader = (row for rows in (reader, csv.reader(open('course_names.csv', newline='', encoding='utf-8'))) for row in rows)  # fallback if above feels weird

# Simpler: reopen cleanly and iterate all rows
with open('course_names.csv', newline='', encoding='utf-8') as f:
    reader = csv.reader(f)
    header_seen = False
    for row in reader:
        if not row:
            continue
        text = (row[0] or '')
        # Normalize
        text = text.replace('–', '-').replace('—', '-').replace('•', '').strip().upper()

        # Skip header row if encountered
        if not header_seen and text == 'COURSE NAME':
            header_seen = True
            continue

        m = CODE_LINE_RE.match(text)
        if m:
            subj, catalog = m.group(1), m.group(2)
            codes.add(f'{subj} {catalog}')
        else:
            rejected.append(text)

# Write unique, sorted codes
with open('course_codes.txt', 'w', encoding='utf-8') as out:
    for code in sorted(codes):
        out.write(code + '\n')

# (Optional) see what got filtered out
with open('rejected_lines.txt', 'w', encoding='utf-8') as rej:
    for line in rejected:
        rej.write(line + '\n')

print(f'wrote {len(codes)} codes to course_codes.txt (and {len(rejected)} rejects to rejected_lines.txt)')
