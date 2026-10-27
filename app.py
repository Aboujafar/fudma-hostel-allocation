
import os
import sqlite3
from functools import wraps
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, g, abort
)

from werkzeug.security import generate_password_hash, check_password_hash

from flask_wtf.csrf import CSRFProtect

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "hostel.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY")

if not app.config["SECRET_KEY"]:
    raise RuntimeError(
        "Set FLASK_SECRET_KEY before starting the application."
    
    )
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("FLASK_HTTPS_ONLY", "0") == "1"
)
csrf = CSRFProtect(app)
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA foreign_keys = ON")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            matric_number TEXT NOT NULL UNIQUE,
            department TEXT NOT NULL,
            level TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'student'
                CHECK(role IN ('student', 'admin'))
        );

        CREATE TABLE IF NOT EXISTS hostels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            capacity INTEGER NOT NULL CHECK(capacity >= 0)
        );

        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            hostel_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'approved', 'rejected')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(hostel_id) REFERENCES hostels(id)
        );

        CREATE TABLE IF NOT EXISTS allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            hostel_id INTEGER NOT NULL,
            bed_number INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(hostel_id, bed_number),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(hostel_id) REFERENCES hostels(id)
        );
    """)

    # Demonstration capacities only. Replace with verified
    # hostel capacities before using real university data.
    for name in (
        "Darawa Hostel",
        "Hayin Gada Hostel",
        "Isa Kaita Hostel"
    ):
        db.execute(
            "INSERT OR IGNORE INTO hostels (name, capacity) VALUES (?, ?)",
            (name, 100)
        )

    db.commit()
    db.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.route("/")
def index():
    db = get_db()
    hostels = db.execute(
        "SELECT * FROM hostels ORDER BY name"
    ).fetchall()
    return render_template("index.html", hostels=hostels)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        matric = request.form.get("matric_number", "").strip()
        department = request.form.get("department", "").strip()
        level = request.form.get("level", "").strip()
        password = request.form.get("password", "")

        if not all([full_name, matric, department, level, password]):
            flash("Please complete all fields.", "error")
            return render_template("register.html")

        if len(password) < 8:
            flash("Password must contain at least 8 characters.", "error")
            return render_template("register.html")

        db = get_db()

        try:
            db.execute("""
                INSERT INTO users
                (full_name, matric_number, department, level, password_hash)
                VALUES (?, ?, ?, ?, ?)
            """, (
                full_name, matric, department, level,
                generate_password_hash(password)
            ))
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            flash("That matriculation number is already registered.", "error")
            return render_template("register.html")

        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        matric = request.form.get("matric_number", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE matric_number = ?",
            (matric,)
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]

            if user["role"] == "admin":
                return redirect(url_for("admin"))

            return redirect(url_for("dashboard"))

        flash("Invalid matriculation number or password.", "error")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have logged out.", "success")
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()

    application = db.execute("""
        SELECT applications.*, hostels.name AS hostel_name,
               allocations.bed_number
        FROM applications
        JOIN hostels ON hostels.id = applications.hostel_id
        LEFT JOIN allocations ON allocations.user_id = applications.user_id
        WHERE applications.user_id = ?
    """, (session["user_id"],)).fetchone()

    hostels = db.execute(
        "SELECT * FROM hostels ORDER BY name"
    ).fetchall()

    return render_template(
        "dashboard.html",
        application=application,
        hostels=hostels
    )


@app.route("/apply", methods=["POST"])
@login_required
def apply():
    hostel_id = request.form.get("hostel_id", type=int)

    db = get_db()
    hostel = db.execute(
        "SELECT id FROM hostels WHERE id = ?", (hostel_id,)
    ).fetchone()

    if not hostel:
        flash("Please select a valid hostel.", "error")
        return redirect(url_for("dashboard"))

    try:
        db.execute("""
            INSERT INTO applications (user_id, hostel_id)
            VALUES (?, ?)
        """, (session["user_id"], hostel_id))
        db.commit()
        flash("Application submitted successfully.", "success")
    except sqlite3.IntegrityError:
        db.rollback()
        flash("You already have an application on record.", "error")

    return redirect(url_for("dashboard"))


@app.route("/admin")
@login_required
@admin_required
def admin():
    db = get_db()

    applications = db.execute("""
        SELECT applications.id, applications.status,
               applications.created_at, users.full_name,
               users.matric_number, users.department, users.level,
               hostels.name AS hostel_name
        FROM applications
        JOIN users ON users.id = applications.user_id
        JOIN hostels ON hostels.id = applications.hostel_id
        ORDER BY applications.created_at DESC
    """).fetchall()

    hostels = db.execute("""
        SELECT hostels.*,
            (SELECT COUNT(*) FROM allocations
             WHERE allocations.hostel_id = hostels.id) AS occupied
        FROM hostels ORDER BY hostels.name
    """).fetchall()

    return render_template(
        "admin.html",
        applications=applications,
        hostels=hostels
    )


def allocate_bed(db, user_id, hostel_id):
    """Allocate the first available bed, within a write transaction."""
    existing = db.execute(
        "SELECT id FROM allocations WHERE user_id = ?", (user_id,)
    ).fetchone()

    if existing:
        return

    hostel = db.execute(
        "SELECT capacity FROM hostels WHERE id = ?", (hostel_id,)
    ).fetchone()

    if hostel is None:
        raise ValueError("Hostel does not exist.")

    occupied = {
        row["bed_number"]
        for row in db.execute(
            "SELECT bed_number FROM allocations WHERE hostel_id = ?",
            (hostel_id,)
        ).fetchall()
    }

    bed_number = next(
        (number for number in range(1, hostel["capacity"] + 1)
         if number not in occupied),
        None
    )

    if bed_number is None:
        raise ValueError("No beds are available in this hostel.")

    db.execute("""
        INSERT INTO allocations (user_id, hostel_id, bed_number)
        VALUES (?, ?, ?)
    """, (user_id, hostel_id, bed_number))


@app.route("/admin/application/<int:application_id>/<action>",
           methods=["POST"])
@login_required
@admin_required
def update_application(application_id, action):
    if action not in ("approve", "reject"):
        abort(400)

    db = get_db()

    try:
        # Prevent two concurrent approvals from taking the same bed.
        db.execute("BEGIN IMMEDIATE")

        application = db.execute("""
            SELECT * FROM applications WHERE id = ?
        """, (application_id,)).fetchone()

        if application is None:
            db.rollback()
            abort(404)

        if action == "approve":
            allocate_bed(
                db, application["user_id"], application["hostel_id"]
            )
            db.execute(
                "UPDATE applications SET status = 'approved' WHERE id = ?",
                (application_id,)
            )
        else:
            db.execute(
                "DELETE FROM allocations WHERE user_id = ?",
                (application["user_id"],)
            )
            db.execute(
                "UPDATE applications SET status = 'rejected' WHERE id = ?",
                (application_id,)
            )

        db.commit()
        flash("Application status updated.", "success")

    except ValueError as error:
        db.rollback()
        flash(str(error), "error")
    except sqlite3.IntegrityError:
        db.rollback()
        flash("Allocation conflict. Please review this application.", "error")

    return redirect(url_for("admin"))


if __name__ == "__main__":
    init_db()
    app.run(debug=False)