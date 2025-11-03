// index.js
require('dotenv').config();
const { test, expect, chromium } = require('@playwright/test'); // using core runner-less API
const fs = require('fs');
const path = require('path');
const { XMLParser } = require('fast-xml-parser');
const mysql = require('mysql2/promise');

const FALL_TERM_ID = process.env.FALL_TERM_ID;
const SCRATCH_URL  = process.env.SCRATCH_URL;

const AUTH_STATE = path.resolve(__dirname, 'auth.json'); // created by codegen step

// ---------- Helpers ----------
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function minutesToHHMM(m) {
  const h = Math.floor(m / 60);
  const mm = `${m % 60}`.padStart(2, '0');
  return `${h}:${mm}`;
}

function parseIntegerList(csv) {
  if (!csv) return [];
  return csv.split(',').map(s => parseInt(s.trim(), 10)).filter(n => Number.isFinite(n));
}

// 1=Sun, 2=Mon, ... 7=Sat (matches the XML 'day=' values)
const dayNameMap = {1:'Sun',2:'Mon',3:'Tue',4:'Wed',5:'Thu',6:'Fri',7:'Sat'};

// ---------- DB ----------
async function getDb() {
  return mysql.createPool({
    host: process.env.DB_HOST,
    port: Number(process.env.DB_PORT || 3306),
    user: process.env.DB_USER,
    password: process.env.DB_PASS,
    database: process.env.DB_NAME,
    waitForConnections: true,
    connectionLimit: 5
  });
}

async function upsertCore(db, course, offering, sections, meetings) {
  // course
  const [crows] = await db.execute(
    `INSERT INTO courses (subject, number, title)
     VALUES (?, ?, ?)
     ON DUPLICATE KEY UPDATE title=VALUES(title)`,
    [course.subject, course.number, course.title || null]
  );
  const [ccheck] = await db.execute(`SELECT id FROM courses WHERE subject=? AND number=?`, [course.subject, course.number]);
  const courseId = ccheck[0].id;

  // term
  const [trows] = await db.execute(`SELECT id FROM terms WHERE mt_id=?`, [String(FALL_TERM_ID)]);
  const termId = trows[0].id;

  // offering
  await db.execute(
    `INSERT INTO offerings (course_id, term_id)
     VALUES (?, ?)
     ON DUPLICATE KEY UPDATE term_id=term_id`,
    [courseId, termId]
  );
  const [orows] = await db.execute(`SELECT id FROM offerings WHERE course_id=? AND term_id=?`, [courseId, termId]);
  const offeringId = orows[0].id;

  // sections
  const secIdMap = new Map(); // key: component+sec_code → id
  for (const s of sections) {
    await db.execute(
      `INSERT INTO sections (offering_id, component, sec_code, class_number, delivery, raw_block_key)
       VALUES (?, ?, ?, ?, ?, ?)
       ON DUPLICATE KEY UPDATE
         class_number=VALUES(class_number),
         delivery=VALUES(delivery),
         raw_block_key=VALUES(raw_block_key)`,
      [offeringId, s.component, s.sec_code, s.class_number || null, s.delivery || null, s.raw_block_key || null]
    );
    const [sid] = await db.execute(
      `SELECT id FROM sections WHERE offering_id=? AND component=? AND sec_code=?`,
      [offeringId, s.component, s.sec_code]
    );
    secIdMap.set(`${s.component}:${s.sec_code}`, sid[0].id);
  }

  // meetings
  for (const m of meetings) {
    const key = `${m.component}:${m.sec_code}`;
    const sectionId = secIdMap.get(key);
    if (!sectionId) continue;
    await db.execute(
      `INSERT INTO meetings (section_id, day_of_week, start_minutes, end_minutes)
       VALUES (?, ?, ?, ?)
       ON DUPLICATE KEY UPDATE
         start_minutes=VALUES(start_minutes),
         end_minutes=VALUES(end_minutes)`,
      [sectionId, m.day, m.t1, m.t2]
    );
  }
}

// ---------- XML parsing ----------
function parseClassDataXml(xmlText) {
  const parser = new XMLParser({ ignoreAttributes: false, attributeNamePrefix: '' });
  const root = parser.parse(xmlText);
  // Shape: addcourse -> classdata -> course -> uselection[] -> selection -> block[] and timeblock[]
  const cd = root?.addcourse?.classdata;
  if (!cd) return null;

  const courseNode = cd.course;
  const subject = courseNode?.code || null;
  const number  = courseNode?.number || null;

  // Title is in <offering title="..."> (outside uselection)
  let title = null;
  if (courseNode?.offering && courseNode.offering.title) {
    title = courseNode.offering.title;
  }

  // normalize uselection(s)
  const uselections = Array.isArray(courseNode.uselection)
    ? courseNode.uselection
    : (courseNode.uselection ? [courseNode.uselection] : []);

  // Collect blocks & timeblocks
  const sections = [];
  const meetings = [];

  for (const use of uselections) {
    const sel = use.selection;
    if (!sel) continue;

    // blocks (sections)
    const blocks = Array.isArray(sel.block) ? sel.block : (sel.block ? [sel.block] : []);
    // timeblocks under uselection scope
    const tbs = Array.isArray(use.timeblock) ? use.timeblock : (use.timeblock ? [use.timeblock] : []);

    // index timeblocks by id for quick lookup
    const tbById = new Map();
    for (const tb of tbs) {
      tbById.set(String(tb.id), {
        id: String(tb.id),
        day: Number(tb.day),             // 1=Sun ... 7=Sat
        t1: Number(tb.t1),               // minutes since midnight
        t2: Number(tb.t2)
      });
    }

    for (const b of blocks) {
      const component = b.type;              // e.g., LEC, TUT
      const sec_code  = b.secNo;             // e.g., C01, T01
      const raw_block_key = b.key ?? null;
      const delivery = b.im === 'P' ? 'In Person' : (b.im ? b.im : null);
      // class number often NOT present in this XML — keep null-friendly column
      const class_number = b.me ?? null;     // (if 'me' is actually class number at your school; may be null/meaningless)

      sections.push({
        component,
        sec_code,
        raw_block_key,
        delivery,
        class_number
      });

      // meetings for this block via timeblockids
      const ids = parseIntegerList(b.timeblockids || '');
      for (const id of ids) {
        const tb = tbById.get(String(id));
        if (!tb) continue;
        meetings.push({
          component,
          sec_code,
          day: tb.day,
          t1: tb.t1,
          t2: tb.t2
        });
      }
    }
  }

  return {
    course: { subject, number, title },
    sections,
    meetings
  };
}

// ---------- Main ----------
(async () => {
  // DB pool
  const db = await getDb();

  // Browser
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    timezoneId: 'America/Toronto',
    locale: 'en-CA',
    storageState: fs.existsSync(AUTH_STATE) ? AUTH_STATE : undefined,
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36'
  });
  const page = await context.newPage();

  // Hard safety: deny any risky endpoints (super defensive even in scratch mode)
  await context.route('**/*', async (route) => {
    const url = route.request().url();
    const lower = url.toLowerCase();
    const risky =
      lower.includes('drop') ||
      lower.includes('enrol') || lower.includes('enroll') ||
      lower.includes('register') || lower.includes('shoppingcart') ||
      lower.includes('commit') || lower.includes('save') && !lower.includes('criteria.jsp') ||
      lower.includes('buildschedule') || lower.includes('getthisschedule');
    if (risky) return route.abort();
    return route.continue();
  });

  async function ensureLoggedIn() {
    // If you didn’t save auth.json, do a one-time programmatic login using env vars:
    if (!fs.existsSync(AUTH_STATE)) {
      await page.goto('https://mytimetable.mcmaster.ca/criteria.jsp', { waitUntil: 'domcontentloaded' });
      await page.getByRole('button', { name: 'Sign In' }).click();
      await page.getByRole('textbox', { name: 'Please enter your User ID' }).fill(process.env.MAC_ID || '');
      await page.getByRole('textbox', { name: 'Please enter your password' }).fill(process.env.MAC_PASS || '');
      await page.getByRole('button', { name: 'Sign In' }).click();
      await page.waitForLoadState('networkidle');
      // Save state so we don’t keep credentials in memory
      await context.storageState({ path: AUTH_STATE });
    }
  }

  await ensureLoggedIn();

  // Load courses
  const coursesList = fs.readFileSync(path.resolve(__dirname, 'courses.txt'), 'utf8')
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean);

  console.log(`Processing ${coursesList.length} courses…`);

  for (let i = 0; i < coursesList.length; i++) {
    const courseQuery = coursesList[i];
    console.log(`[${i+1}/${coursesList.length}] ${courseQuery}`);

    // Always start from scratch planner
    await page.goto(SCRATCH_URL, { waitUntil: 'domcontentloaded' });

    // Remove any leftovers on the left list (safety)
    const removeButtons = await page.locator('button[aria-label="Remove course"]').all();
    for (const btn of removeButtons) {
      try { await btn.click({ timeout: 2000 }); } catch {}
      await sleep(150);
    }

    // Prepare to catch the XML
    const xmlPromise = page.waitForResponse((resp) => {
      const url = resp.url();
      return url.includes('/api/class-data') && url.includes('term='); // broad match
    }, { timeout: 5000 }).catch(() => null);

    // Interact with the combobox
    const combo = page.getByRole('combobox', { name: /Select Course/i });
    await combo.click();
    await combo.fill(courseQuery);
    // Wait for suggestions and pick the first one
    const firstOption = page.getByRole('option').first();
    await firstOption.waitFor({ state: 'visible', timeout: 3000 }).catch(() => {});
    // Prefer clicking—it’s more reliable than Enter
    try { await firstOption.click({ timeout: 2000 }); } catch {}

    // Fallback: press Enter if click didn't register
    if (!(await xmlPromise)) {
      await combo.press('Enter').catch(() => {});
    }

    let xmlResp = await xmlPromise;
    // Retry once if needed
    if (!xmlResp) {
      // re-navigate to scratch and try again
      await page.goto(SCRATCH_URL, { waitUntil: 'domcontentloaded' });
      await combo.click();
      await combo.fill(courseQuery);
      const opt = page.getByRole('option').first();
      await opt.waitFor({ state: 'visible', timeout: 3000 }).catch(() => {});
      try { await opt.click({ timeout: 2000 }); } catch {}
      xmlResp = await page.waitForResponse(r => r.url().includes('/api/class-data'), { timeout: 5000 }).catch(() => null);
    }

    if (!xmlResp) {
      console.warn(`  ⚠️  No XML response captured; skipping.`);
      continue;
    }

    const xmlText = await xmlResp.text();

    // Parse XML
    const parsed = parseClassDataXml(xmlText);
    if (!parsed || !parsed.course?.subject || !parsed.course?.number) {
      console.warn(`  ⚠️  Couldn’t parse useful data; skipping.`);
      continue;
    }

    // Write to DB
    try {
      await upsertCore(db, parsed.course, null, parsed.sections, parsed.meetings);
      console.log(`  ✓ Saved ${parsed.sections.length} sections, ${parsed.meetings.length} meetings (${parsed.course.subject} ${parsed.course.number})`);
    } catch (e) {
      console.error(`  ❌ DB error:`, e.message);
    }

    // Small polite pause
    await sleep(200 + Math.floor(Math.random()*250));
  }

  await browser.close();
  await db.end();
  console.log('Done.');
})();
