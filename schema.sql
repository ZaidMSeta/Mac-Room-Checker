CREATE DATABASE IF NOT EXISTS mactimetable CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE mactimetable;

CREATE TABLE IF NOT EXISTS terms (
  id INT AUTO_INCREMENT PRIMARY KEY,
  mt_id VARCHAR(32) NOT NULL UNIQUE,   -- MyTimetable 'term' (e.g. 3202530)
  name VARCHAR(64) NULL
);

CREATE TABLE IF NOT EXISTS courses (
  id INT AUTO_INCREMENT PRIMARY KEY,
  subject VARCHAR(32) NOT NULL,
  number  VARCHAR(32) NOT NULL,
  title   VARCHAR(255) NULL,
  UNIQUE KEY uq_course (subject, number)
);

CREATE TABLE IF NOT EXISTS offerings (
  id INT AUTO_INCREMENT PRIMARY KEY,
  course_id INT NOT NULL,
  term_id   INT NOT NULL,
  FOREIGN KEY (course_id) REFERENCES courses(id),
  FOREIGN KEY (term_id) REFERENCES terms(id),
  UNIQUE KEY uq_offering (course_id, term_id)
);

CREATE TABLE IF NOT EXISTS sections (
  id INT AUTO_INCREMENT PRIMARY KEY,
  offering_id INT NOT NULL,
  component   VARCHAR(12) NOT NULL,   -- LEC/TUT/LAB/SEM, etc.
  sec_code    VARCHAR(16) NOT NULL,   -- e.g. C01, T02
  class_number VARCHAR(32) NULL,      -- often not present in XML; keep for future
  delivery    VARCHAR(16) NULL,       -- from im="P" (In Person), etc.
  raw_block_key VARCHAR(32) NULL,     -- block key (e.g., "2469") to help map later
  UNIQUE KEY uq_section (offering_id, component, sec_code),
  INDEX idx_blockkey (raw_block_key),
  FOREIGN KEY (offering_id) REFERENCES offerings(id)
);

CREATE TABLE IF NOT EXISTS meetings (
  id INT AUTO_INCREMENT PRIMARY KEY,
  section_id INT NOT NULL,
  day_of_week TINYINT NOT NULL,       -- 1=Sun, 2=Mon, ... 7=Sat (site uses this)
  start_minutes INT NOT NULL,         -- minutes since midnight (t1)
  end_minutes   INT NOT NULL,         -- minutes since midnight (t2)
  -- no building/room by design (XML lacks it)
  UNIQUE KEY uq_meeting (section_id, day_of_week, start_minutes, end_minutes),
  FOREIGN KEY (section_id) REFERENCES sections(id)
);

INSERT IGNORE INTO terms (mt_id, name) VALUES ('${FALL_TERM_ID}', '2025 Fall');
