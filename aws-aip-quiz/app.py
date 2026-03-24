"""Flask backend for AWS AIP-C01 Quiz Application."""
import json
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request

app = Flask(__name__)
DB_PATH = "quiz.db"


def _migrate_exam_marked_column():
    """Ensure exam_answers.marked exists (older DBs)."""
    path = Path(__file__).resolve().parent / DB_PATH
    if not path.exists():
        return
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("PRAGMA table_info(exam_answers)")
        cols = {row[1] for row in cur.fetchall()}
        if "marked" not in cols:
            conn.execute(
                "ALTER TABLE exam_answers ADD COLUMN marked INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()
    finally:
        conn.close()


_migrate_exam_marked_column()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


@app.route("/")
def index():
    return render_template("index.html")


# ─── Question APIs ───────────────────────────────────────────────────

@app.route("/api/questions")
def get_questions():
    """Get all questions with study status."""
    db = get_db()
    rows = db.execute(
        """SELECT q.id, q.number, q.topic,
                  sa.is_correct AS study_status,
                  CASE WHEN sq.question_id IS NOT NULL THEN 1 ELSE 0 END AS starred,
                  CASE WHEN n.content IS NOT NULL AND n.content != '' THEN 1 ELSE 0 END AS has_note
           FROM questions q
           LEFT JOIN study_answers sa ON q.id = sa.question_id
           LEFT JOIN starred_questions sq ON q.id = sq.question_id
           LEFT JOIN notes n ON q.id = n.question_id
           ORDER BY q.number"""
    ).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/questions/<int:question_id>")
def get_question(question_id: int):
    """Get a single question with full details."""
    db = get_db()
    row = db.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if not row:
        return jsonify({"error": "Question not found"}), 404

    data = row_to_dict(row)
    data["choices_en"] = json.loads(data["choices_en"])
    data["choices_tw"] = json.loads(data["choices_tw"]) if data["choices_tw"] else {}
    data["answer"] = json.loads(data["answer"])
    data["original_answer"] = json.loads(data["original_answer"]) if data.get("original_answer") else data["answer"]
    data["choice_analysis"] = json.loads(data["choice_analysis"]) if data["choice_analysis"] else {}

    starred = db.execute(
        "SELECT 1 FROM starred_questions WHERE question_id = ?", (question_id,)
    ).fetchone()
    data["starred"] = starred is not None

    note = db.execute(
        "SELECT content FROM notes WHERE question_id = ?", (question_id,)
    ).fetchone()
    data["note"] = note["content"] if note else ""

    override = db.execute(
        "SELECT user_answer, previous_answer FROM answer_overrides WHERE question_id = ?",
        (question_id,),
    ).fetchone()
    if override:
        data["override"] = {
            "answer": json.loads(override["user_answer"]),
            "previous": json.loads(override["previous_answer"]),
        }
        data["answer"] = data["override"]["answer"]
    else:
        data["override"] = None

    return jsonify(data)


# ─── Star APIs ───────────────────────────────────────────────────────

@app.route("/api/starred")
def get_starred():
    """Get all starred question IDs."""
    db = get_db()
    rows = db.execute("SELECT question_id FROM starred_questions").fetchall()
    return jsonify([r["question_id"] for r in rows])


@app.route("/api/questions/<int:question_id>/star", methods=["POST"])
def toggle_star(question_id: int):
    """Toggle star for a question."""
    db = get_db()
    existing = db.execute(
        "SELECT 1 FROM starred_questions WHERE question_id = ?", (question_id,)
    ).fetchone()

    if existing:
        db.execute("DELETE FROM starred_questions WHERE question_id = ?", (question_id,))
        starred = False
    else:
        db.execute(
            "INSERT INTO starred_questions (question_id) VALUES (?)", (question_id,)
        )
        starred = True
    db.commit()
    return jsonify({"starred": starred})


# ─── Notes APIs ──────────────────────────────────────────────────

@app.route("/api/questions/<int:question_id>/note")
def get_note(question_id: int):
    db = get_db()
    row = db.execute(
        "SELECT content FROM notes WHERE question_id = ?", (question_id,)
    ).fetchone()
    return jsonify({"content": row["content"] if row else ""})


@app.route("/api/questions/<int:question_id>/note", methods=["PUT"])
def save_note(question_id: int):
    db = get_db()
    content = request.get_json().get("content", "")
    db.execute(
        """INSERT INTO notes (question_id, content, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(question_id) DO UPDATE SET content = ?, updated_at = CURRENT_TIMESTAMP""",
        (question_id, content, content),
    )
    db.commit()
    return jsonify({"ok": True})


# ─── Answer Override APIs ────────────────────────────────────────

@app.route("/api/questions/<int:question_id>/override", methods=["POST"])
def save_override(question_id: int):
    db = get_db()
    payload = request.get_json()
    new_answer = json.dumps(sorted(payload.get("answer", [])))
    row = db.execute("SELECT answer FROM questions WHERE id = ?", (question_id,)).fetchone()
    if not row:
        return jsonify({"error": "Question not found"}), 404
    current_answer = row["answer"]
    db.execute(
        """INSERT INTO answer_overrides (question_id, user_answer, previous_answer, created_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(question_id) DO UPDATE
           SET user_answer = ?, previous_answer = ?, created_at = CURRENT_TIMESTAMP""",
        (question_id, new_answer, current_answer, new_answer, current_answer),
    )
    db.commit()
    return jsonify({"ok": True, "answer": json.loads(new_answer)})


@app.route("/api/questions/<int:question_id>/override", methods=["DELETE"])
def delete_override(question_id: int):
    db = get_db()
    db.execute("DELETE FROM answer_overrides WHERE question_id = ?", (question_id,))
    db.commit()
    return jsonify({"ok": True})


def get_overrides(db) -> dict:
    rows = db.execute("SELECT question_id, user_answer, previous_answer FROM answer_overrides").fetchall()
    return {r["question_id"]: {"answer": json.loads(r["user_answer"]), "previous": json.loads(r["previous_answer"])} for r in rows}


# ─── Study Answer APIs ───────────────────────────────────────────

@app.route("/api/questions/<int:question_id>/study-answer", methods=["POST"])
def save_study_answer(question_id: int):
    db = get_db()
    payload = request.get_json()
    user_answer = json.dumps(sorted(payload.get("user_answer", [])))
    is_correct = payload.get("is_correct", False)
    db.execute(
        """INSERT INTO study_answers (question_id, user_answer, is_correct, answered_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(question_id) DO UPDATE
           SET user_answer = ?, is_correct = ?, answered_at = CURRENT_TIMESTAMP""",
        (question_id, user_answer, is_correct, user_answer, is_correct),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/study-answers")
def get_study_answers():
    db = get_db()
    rows = db.execute("SELECT question_id, is_correct FROM study_answers").fetchall()
    return jsonify({str(r["question_id"]): r["is_correct"] for r in rows})


@app.route("/api/questions/full")
def get_questions_full():
    """Get all questions with full details for list view."""
    db = get_db()
    rows = db.execute("SELECT * FROM questions ORDER BY number").fetchall()
    starred_rows = db.execute("SELECT question_id FROM starred_questions").fetchall()
    starred_ids = {r["question_id"] for r in starred_rows}
    sa_rows = db.execute("SELECT question_id, user_answer, is_correct FROM study_answers").fetchall()
    sa_map = {r["question_id"]: row_to_dict(r) for r in sa_rows}
    note_rows = db.execute("SELECT question_id, content FROM notes WHERE content != ''").fetchall()
    note_map = {r["question_id"]: r["content"] for r in note_rows}

    overrides = get_overrides(db)

    result = []
    for row in rows:
        d = row_to_dict(row)
        d["choices_en"] = json.loads(d["choices_en"])
        d["choices_tw"] = json.loads(d["choices_tw"]) if d["choices_tw"] else {}
        d["answer"] = json.loads(d["answer"])
        d["original_answer"] = json.loads(d["original_answer"]) if d.get("original_answer") else d["answer"]
        d["choice_analysis"] = json.loads(d["choice_analysis"]) if d["choice_analysis"] else {}
        d["starred"] = d["id"] in starred_ids
        sa = sa_map.get(d["id"])
        d["study_status"] = sa["is_correct"] if sa else None
        d["study_answer"] = json.loads(sa["user_answer"]) if sa else None
        d["note"] = note_map.get(d["id"], "")
        ov = overrides.get(d["id"])
        if ov:
            d["override"] = ov
            d["answer"] = ov["answer"]
        else:
            d["override"] = None
        result.append(d)
    return jsonify(result)


# ─── Exam APIs ───────────────────────────────────────────────────────

@app.route("/api/exam/start", methods=["POST"])
def start_exam():
    """Start a new exam with randomized questions and choices."""
    db = get_db()
    rows = db.execute("SELECT * FROM questions ORDER BY number").fetchall()
    overrides = get_overrides(db)
    questions = []
    for row in rows:
        data = row_to_dict(row)
        data["choices_en"] = json.loads(data["choices_en"])
        data["choices_tw"] = json.loads(data["choices_tw"]) if data["choices_tw"] else {}
        data["answer"] = json.loads(data["answer"])
        data["choice_analysis"] = json.loads(data["choice_analysis"]) if data["choice_analysis"] else {}
        ov = overrides.get(data["id"])
        if ov:
            data["answer"] = ov["answer"]
        questions.append(data)

    random.shuffle(questions)

    for q in questions:
        letters = list(q["choices_en"].keys())
        random.shuffle(letters)

        old_to_new = {old: new for old, new in zip(letters, sorted(q["choices_en"].keys()))}
        new_to_old = {v: k for k, v in old_to_new.items()}

        new_choices_en = {}
        new_choices_tw = {}
        new_analysis = {}
        for new_letter in sorted(old_to_new.values()):
            old_letter = new_to_old[new_letter]
            new_choices_en[new_letter] = q["choices_en"][old_letter]
            if old_letter in q["choices_tw"]:
                new_choices_tw[new_letter] = q["choices_tw"][old_letter]
            if old_letter in q["choice_analysis"]:
                new_analysis[new_letter] = q["choice_analysis"][old_letter]

        new_answer = sorted([old_to_new[a] for a in q["answer"] if a in old_to_new])

        q["choices_en"] = new_choices_en
        q["choices_tw"] = new_choices_tw
        q["choice_analysis"] = new_analysis
        q["answer"] = new_answer

    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        "INSERT INTO exam_records (started_at, total_questions) VALUES (?, ?)",
        (now, len(questions)),
    )
    db.commit()
    exam_id = cursor.lastrowid

    return jsonify({"exam_id": exam_id, "questions": questions, "total": len(questions)})


@app.route("/api/exam/<int:exam_id>/submit", methods=["POST"])
def submit_exam(exam_id: int):
    """Submit exam answers and calculate score."""
    db = get_db()
    payload = request.get_json()
    answers = payload.get("answers", [])
    time_spent = payload.get("time_spent_seconds", 0)

    correct_count = 0
    results = []
    for item in answers:
        qid = item["question_id"]
        user_ans = sorted(item["user_answer"])
        correct_ans = sorted(item["correct_answer"])
        is_correct = user_ans == correct_ans
        choices_shown = json.dumps(item.get("choices_shown", {}), ensure_ascii=False)
        marked = 1 if item.get("marked") else 0

        if is_correct:
            correct_count += 1

        db.execute(
            """INSERT INTO exam_answers
               (exam_id, question_id, user_answer, correct_answer, is_correct, choices_shown, marked)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                exam_id,
                qid,
                json.dumps(user_ans),
                json.dumps(correct_ans),
                is_correct,
                choices_shown,
                marked,
            ),
        )

        results.append({
            "question_id": qid,
            "user_answer": user_ans,
            "correct_answer": correct_ans,
            "is_correct": is_correct,
            "marked": bool(marked),
        })

    total = len(answers)
    score = round((correct_count / total * 100), 1) if total > 0 else 0
    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        """UPDATE exam_records
           SET finished_at = ?, correct_count = ?, score = ?, time_spent_seconds = ?
           WHERE id = ?""",
        (now, correct_count, score, time_spent, exam_id),
    )
    db.commit()

    return jsonify({
        "exam_id": exam_id,
        "total": total,
        "correct_count": correct_count,
        "score": score,
        "time_spent_seconds": time_spent,
        "results": results,
    })


@app.route("/api/exam/history")
def exam_history():
    """Get all past exam records."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM exam_records ORDER BY started_at DESC"
    ).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/exam/<int:exam_id>/details")
def exam_details(exam_id: int):
    """Get detailed results for a specific exam."""
    db = get_db()
    record = db.execute("SELECT * FROM exam_records WHERE id = ?", (exam_id,)).fetchone()
    if not record:
        return jsonify({"error": "Exam not found"}), 404

    answers = db.execute(
        """SELECT ea.*, q.question_en, q.question_tw,
                  q.choices_en AS orig_choices_en, q.choices_tw AS orig_choices_tw,
                  q.number, q.logic, q.choice_analysis, q.link
           FROM exam_answers ea
           JOIN questions q ON ea.question_id = q.id
           WHERE ea.exam_id = ?""",
        (exam_id,),
    ).fetchall()

    starred_rows = db.execute("SELECT question_id FROM starred_questions").fetchall()
    starred_ids = {r["question_id"] for r in starred_rows}

    answer_list = []
    for a in answers:
        d = row_to_dict(a)
        d["user_answer"] = json.loads(d["user_answer"])
        d["correct_answer"] = json.loads(d["correct_answer"])
        d["choices_shown"] = json.loads(d["choices_shown"]) if d.get("choices_shown") else None
        d["orig_choices_en"] = json.loads(d["orig_choices_en"])
        d["orig_choices_tw"] = json.loads(d["orig_choices_tw"]) if d["orig_choices_tw"] else {}
        d["choice_analysis"] = json.loads(d["choice_analysis"]) if d.get("choice_analysis") else {}
        d["starred"] = d["question_id"] in starred_ids
        d["marked"] = bool(d.get("marked"))
        answer_list.append(d)

    return jsonify({"record": row_to_dict(record), "answers": answer_list})


@app.route("/api/dashboard/stats")
def dashboard_stats():
    """Read-only aggregates: exam score trend & wrong-answer frequency.

    Excludes exams with zero correct answers (no progress to compare).
    Does not modify any stored data.
    """
    db = get_db()
    rows = db.execute(
        """SELECT * FROM exam_records
           WHERE finished_at IS NOT NULL AND correct_count > 0
           ORDER BY started_at ASC"""
    ).fetchall()

    exam_trend = []
    prev_score = None
    for r in rows:
        d = row_to_dict(r)
        delta = None
        if prev_score is not None:
            delta = round(float(d["score"]) - float(prev_score), 1)
        exam_trend.append(
            {
                "id": d["id"],
                "started_at": d["started_at"],
                "score": float(d["score"]),
                "correct_count": d["correct_count"],
                "total_questions": d["total_questions"],
                "delta_from_previous": delta,
            }
        )
        prev_score = float(d["score"])

    exam_ids = [r["id"] for r in rows]
    wrong_frequency = []
    if exam_ids:
        placeholders = ",".join("?" * len(exam_ids))
        wrong_rows = db.execute(
            f"""SELECT ea.question_id, q.number, COUNT(*) AS cnt
                FROM exam_answers ea
                JOIN questions q ON ea.question_id = q.id
                WHERE ea.exam_id IN ({placeholders}) AND ea.is_correct = 0
                GROUP BY ea.question_id
                ORDER BY cnt DESC, q.number ASC""",
            exam_ids,
        ).fetchall()
        wrong_frequency = [
            {
                "question_id": wr["question_id"],
                "number": wr["number"],
                "wrong_count": wr["cnt"],
            }
            for wr in wrong_rows
        ]

    return jsonify({"exam_trend": exam_trend, "wrong_frequency": wrong_frequency})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
