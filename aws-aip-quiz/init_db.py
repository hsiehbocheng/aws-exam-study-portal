"""Parse the xlsx file and import data into SQLite database."""
import json
import re
import sqlite3
import openpyxl

XLSX_PATH = "../AIP-C01-questions-explain.xlsx"
DB_PATH = "quiz.db"


def parse_choices(choice_text: str) -> dict[str, str]:
    """Parse choice text like 'A.xxx\\nB.xxx' into {'A': 'xxx', 'B': 'xxx'}."""
    if not choice_text:
        return {}
    parts = re.split(r"\n(?=[A-G][\.\s])", choice_text.strip())
    choices = {}
    for part in parts:
        match = re.match(r"^([A-G])[\.\s]\s*(.*)", part.strip(), re.DOTALL)
        if match:
            letter = match.group(1)
            text = match.group(2).strip()
            choices[letter] = text
    return choices


def parse_choice_analysis(text: str) -> dict[str, str]:
    """Parse choice analysis like 'A: explanation\\nB: explanation'."""
    if not text:
        return {}
    parts = re.split(r"\n(?=[A-G]:)", text.strip())
    analysis = {}
    for part in parts:
        match = re.match(r"^([A-G]):\s*(.*)", part.strip(), re.DOTALL)
        if match:
            letter = match.group(1)
            explanation = match.group(2).strip()
            analysis[letter] = explanation
    return analysis


def parse_answer(answer_text: str) -> list[str]:
    """Parse answer like 'ADF' or 'C' into ['A', 'D', 'F'] or ['C']."""
    if not answer_text:
        return []
    cleaned = answer_text.strip().replace(",", "").replace(" ", "")
    return list(cleaned.upper())


def create_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY,
            number INTEGER NOT NULL,
            question_en TEXT NOT NULL,
            question_tw TEXT,
            choices_en TEXT NOT NULL,   -- JSON: {"A": "text", "B": "text", ...}
            choices_tw TEXT,            -- JSON: {"A": "text", "B": "text", ...}
            answer TEXT NOT NULL,       -- JSON array: final_answer
            original_answer TEXT,       -- JSON array: community answer (ANSWER column)
            topic INTEGER,
            link TEXT,
            logic TEXT,
            choice_analysis TEXT,       -- JSON: {"A": "explanation", ...}
            uid INTEGER
        );

        CREATE TABLE IF NOT EXISTS starred_questions (
            question_id INTEGER PRIMARY KEY,
            starred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );

        CREATE TABLE IF NOT EXISTS exam_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TIMESTAMP NOT NULL,
            finished_at TIMESTAMP,
            total_questions INTEGER NOT NULL,
            correct_count INTEGER DEFAULT 0,
            score REAL DEFAULT 0,
            time_spent_seconds INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS exam_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            user_answer TEXT NOT NULL,   -- JSON array
            correct_answer TEXT NOT NULL, -- JSON array
            is_correct BOOLEAN NOT NULL,
            choices_shown TEXT,          -- JSON: shuffled choices as shown during exam
            marked INTEGER NOT NULL DEFAULT 0,  -- 1 = user flagged for later review
            FOREIGN KEY (exam_id) REFERENCES exam_records(id),
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );

        CREATE TABLE IF NOT EXISTS notes (
            question_id INTEGER PRIMARY KEY,
            content TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );

        CREATE TABLE IF NOT EXISTS study_answers (
            question_id INTEGER PRIMARY KEY,
            user_answer TEXT NOT NULL,   -- JSON array
            is_correct BOOLEAN NOT NULL,
            answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );

        CREATE TABLE IF NOT EXISTS answer_overrides (
            question_id INTEGER PRIMARY KEY,
            user_answer TEXT NOT NULL,    -- JSON array: user-corrected answer
            previous_answer TEXT NOT NULL, -- JSON array: answer before correction
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );
    """)


def import_data(conn: sqlite3.Connection):
    wb = openpyxl.load_workbook(XLSX_PATH, read_only=True)
    ws = wb.active

    rows_imported = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        num = row[0]
        if num is None:
            continue

        question_en = row[1] or ""
        choices_raw_en = row[2] or ""
        community_answer_raw = row[3] or ""
        final_answer_raw = row[15] or ""
        answer_raw = community_answer_raw if community_answer_raw else final_answer_raw
        uid = int(row[4]) if row[4] else None
        topic = int(row[6]) if row[6] else None
        link = row[7] or ""
        question_tw = row[8] or ""
        choices_raw_tw = row[9] or ""
        logic = row[13] or ""
        choice_analysis_raw = row[14] or ""

        choices_en = parse_choices(choices_raw_en)
        choices_tw = parse_choices(choices_raw_tw)
        answer = parse_answer(str(answer_raw))
        original_answer = parse_answer(str(final_answer_raw))
        choice_analysis = parse_choice_analysis(choice_analysis_raw)

        conn.execute(
            """INSERT INTO questions
               (number, question_en, question_tw, choices_en, choices_tw,
                answer, original_answer, topic, link, logic, choice_analysis, uid)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(num),
                question_en.strip(),
                question_tw.strip(),
                json.dumps(choices_en, ensure_ascii=False),
                json.dumps(choices_tw, ensure_ascii=False),
                json.dumps(answer),
                json.dumps(original_answer),
                topic,
                link.strip(),
                logic.strip(),
                json.dumps(choice_analysis, ensure_ascii=False),
                uid,
            ),
        )
        rows_imported += 1

    conn.commit()
    wb.close()
    print(f"Imported {rows_imported} questions into {DB_PATH}")


def migrate(conn: sqlite3.Connection):
    """Add columns that may not exist in older databases."""
    cursor = conn.execute("PRAGMA table_info(questions)")
    existing = {row[1] for row in cursor.fetchall()}
    if "original_answer" not in existing:
        conn.execute("ALTER TABLE questions ADD COLUMN original_answer TEXT")
        conn.commit()
        print("Migrated: added original_answer column")

    cursor = conn.execute("PRAGMA table_info(exam_answers)")
    exam_cols = {row[1] for row in cursor.fetchall()}
    if "marked" not in exam_cols:
        conn.execute(
            "ALTER TABLE exam_answers ADD COLUMN marked INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        print("Migrated: added exam_answers.marked column")


def main():
    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    migrate(conn)
    conn.execute("DELETE FROM questions")
    conn.commit()
    import_data(conn)
    conn.close()


if __name__ == "__main__":
    main()
