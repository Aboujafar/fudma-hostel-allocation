
import os
import re
import sqlite3
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    g,
    abort,
)
from werkzeug.security import (
    generate_password_hash,
    check_password_hash,
)
from flask_wtf.csrf import CSRFProtect


# ==================================================
# APPLICATION CONFIGURATION
# ==================================================

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


# ==================================================
# DATABASE CONNECTION
# ==================================================

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


# ==================================================
# DATABASE INITIALIZATION AND SAFE MIGRATION
# ==================================================

def init_db():
    """
    Create missing tables and add missing profile columns.
    Existing users, applications, and allocations are preserved.
    """
    db = sqlite3.connect(DATABASE, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    try:
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

        # Add new columns to older databases without deleting records.
        existing_columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        }

        new_columns = {
            "email": "TEXT NOT NULL DEFAULT ''",
            "student_contact": "TEXT NOT NULL DEFAULT ''",
            "next_of_kin_name": "TEXT NOT NULL DEFAULT ''",
            "next_of_kin_contact": "TEXT NOT NULL DEFAULT ''",
        }

        for column_name, column_type in new_columns.items():
            if column_name not in existing_columns:
                db.execute(
                    f"ALTER TABLE users ADD COLUMN "
                    f"{column_name} {column_type}"
                )

        # Indexes help with common lookups. Email is not made UNIQUE
        # here because older records may have blank email addresses.
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_email
            ON users(email)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_applications_status
            ON applications(status)
        """)

        # Initial options for a new database only.
        # Verify actual accommodation capacities before production use.
        for name in (
            "Darawa Hostel",
            "Hayin Gada Hostel",
            "Isa Kaita Hostel",
        ):
            db.execute(
                """
                INSERT OR IGNORE INTO hostels (name, capacity)
                VALUES (?, ?)
                """,
                (name, 100),
            )

        db.commit()
        print(f"Database initialized: {DATABASE}", flush=True)

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


# ==================================================
# ACCESS CONTROL
# ==================================================

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


# ==================================================
# VALIDATION HELPERS
# ==================================================

def valid_email(email):
    """Basic server-side email format check."""
    if len(email) > 254:
        return False

    pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    return bool(re.fullmatch(pattern, email))


def valid_phone(phone):
    """Accept common phone-number characters and lengths."""
    pattern = r"[0-9+()\- ]{7,20}"
    return bool(re.fullmatch(pattern, phone))


# ==================================================
# HOME PAGE
# ==================================================

@app.route("/")
def index():
    db = get_db()

    hostels = db.execute(
        "SELECT * FROM hostels ORDER BY name"
    ).fetchall()

    return render_template("index.html", hostels=hostels)


# ==================================================
# STUDENT REGISTRATION
# ==================================================

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        matric = request.form.get("matric_number", "").strip()
        email = request.form.get("email", "").strip().lower()
        department = request.form.get("department", "").strip()
        level = request.form.get("level", "").strip()

        student_contact = request.form.get(
            "student_contact", ""
        ).strip()

        next_of_kin_name = request.form.get(
            "next_of_kin_name", ""
        ).strip()

        next_of_kin_contact = request.form.get(
            "next_of_kin_contact", ""
        ).strip()

        password = request.form.get("password", "")
        confirm_password = request.form.get(
            "confirm_password", ""
        )

        required_fields = [
            full_name,
            matric,
            email,
            department,
            level,
            student_contact,
            next_of_kin_name,
            next_of_kin_contact,
            password,
            confirm_password,
        ]

        if not all(required_fields):
            flash("Please complete all required fields.", "error")
            return render_template("register.html")

        if len(full_name) > 100 or len(matric) > 50:
            flash(
                "Full name or matriculation number is too long.",
                "error",
            )
            return render_template("register.html")

        if len(department) > 100:
            flash("Department name is too long.", "error")
            return render_template("register.html")

        if len(next_of_kin_name) > 100:
            flash("Next-of-kin name is too long.", "error")
            return render_template("register.html")

        if not valid_email(email):
            flash("Please enter a valid email address.", "error")
            return render_template("register.html")

        if not valid_phone(student_contact):
            flash(
                "Please enter a valid student contact number.",
                "error",
            )
            return render_template("register.html")

        if not valid_phone(next_of_kin_contact):
            flash(
                "Please enter a valid next-of-kin contact number.",
                "error",
            )
            return render_template("register.html")

        if level not in {"100", "200", "300", "400", "500"}:
            flash("Please select a valid level.", "error")
            return render_template("register.html")

        if len(password) < 8:
            flash(
                "Password must contain at least 8 characters.",
                "error",
            )
            return render_template("register.html")

        if password != confirm_password:
            flash("The passwords do not match.", "error")
            return render_template("register.html")

        db = get_db()

        # Check the matriculation number and email.
        existing_user = db.execute(
            """
            SELECT matric_number, email
            FROM users
            WHERE matric_number = ?
               OR (
                   email <> ''
                   AND lower(email) = lower(?)
               )
            """,
            (matric, email),
        ).fetchone()

        if existing_user:
            if existing_user["matric_number"] == matric:
                flash(
                    "That matriculation number is already registered.",
                    "error",
                )
            else:
                flash(
                    "That email address is already registered.",
                    "error",
                )

            return render_template("register.html")

        try:
            db.execute(
                """
                INSERT INTO users (
                    full_name,
                    matric_number,
                    department,
                    level,
                    email,
                    student_contact,
                    next_of_kin_name,
                    next_of_kin_contact,
                    password_hash,
                    role
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'student')
                """,
                (
                    full_name,
                    matric,
                    department,
                    level,
                    email,
                    student_contact,
                    next_of_kin_name,
                    next_of_kin_contact,
                    generate_password_hash(password),
                ),
            )

            db.commit()

        except sqlite3.IntegrityError:
            db.rollback()
            flash(
                "Registration could not be completed. "
                "Check whether the matriculation number or email "
                "is already in use.",
                "error",
            )
            return render_template("register.html")

        flash(
            "Registration successful. Please log in.",
            "success",
        )
        return redirect(url_for("login"))

    return render_template("register.html")


# ==================================================
# LOGIN
# ==================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        matric = request.form.get("matric_number", "").strip()
        password = request.form.get("password", "")

        db = get_db()

        user = db.execute(
            """
            SELECT *
            FROM users
            WHERE matric_number = ?
            """,
            (matric,),
        ).fetchone()

        password_matches = False

        if user:
            try:
                password_matches = check_password_hash(
                    user["password_hash"], password
                )
            except (ValueError, TypeError):
                password_matches = False

        # Diagnostic information; never log passwords or hashes.
        print(
            "LOGIN DEBUG:",
            "ID =", repr(matric),
            "USER FOUND =", bool(user),
            "PASSWORD MATCH =", password_matches,
            "DATABASE =", str(DATABASE),
            flush=True,
        )

        if password_matches:
            session.clear()
            session["user_id"] = user["id"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]

            if user["role"] == "admin":
                return redirect(url_for("admin"))

            return redirect(url_for("dashboard"))

        flash(
            "Invalid matriculation number or password.",
            "error",
        )

    return render_template("login.html")


# ==================================================
# LOGOUT
# ==================================================

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have logged out.", "success")
    return redirect(url_for("index"))


# ==================================================
# STUDENT DASHBOARD
# ==================================================

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()

    student = db.execute(
        """
        SELECT full_name,
               matric_number,
               department,
               level,
               email,
               student_contact,
               next_of_kin_name,
               next_of_kin_contact
        FROM users
        WHERE id = ?
        """,
        (session["user_id"],),
    ).fetchone()

    application = db.execute(
        """
        SELECT applications.*,
               hostels.name AS hostel_name,
               allocations.bed_number
        FROM applications
        JOIN hostels
            ON hostels.id = applications.hostel_id
        LEFT JOIN allocations
            ON allocations.user_id = applications.user_id
        WHERE applications.user_id = ?
        """,
        (session["user_id"],),
    ).fetchone()

    hostels = db.execute(
        "SELECT * FROM hostels ORDER BY name"
    ).fetchall()

    return render_template(
        "dashboard.html",
        student=student,
        application=application,
        hostels=hostels,
    )


# ==================================================
# STUDENT ACCOMMODATION APPLICATION
# ==================================================

@app.route("/apply", methods=["POST"])
@login_required
def apply():
    hostel_id = request.form.get("hostel_id", type=int)

    if hostel_id is None:
        flash("Please select a valid accommodation option.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()

    hostel = db.execute(
        "SELECT id FROM hostels WHERE id = ?",
        (hostel_id,),
    ).fetchone()

    if not hostel:
        flash("Please select a valid accommodation option.", "error")
        return redirect(url_for("dashboard"))

    try:
        db.execute(
            """
            INSERT INTO applications (user_id, hostel_id)
            VALUES (?, ?)
            """,
            (session["user_id"], hostel_id),
        )

        db.commit()
        flash("Application submitted successfully.", "success")

    except sqlite3.IntegrityError:
        db.rollback()
        flash(
            "You already have an application on record.",
            "error",
        )

    return redirect(url_for("dashboard"))


# ==================================================
# ADMIN DASHBOARD
# ==================================================

@app.route("/admin")
@login_required
@admin_required
def admin():
    db = get_db()

    applications = db.execute(
        """
        SELECT applications.id,
               applications.status,
               applications.created_at,
               users.id AS user_id,
               users.full_name,
               users.matric_number,
               users.department,
               users.level,
               users.email,
               users.student_contact,
               users.next_of_kin_name,
               users.next_of_kin_contact,
               hostels.name AS hostel_name,
               allocations.bed_number
        FROM applications
        JOIN users
            ON users.id = applications.user_id
        JOIN hostels
            ON hostels.id = applications.hostel_id
        LEFT JOIN allocations
            ON allocations.user_id = applications.user_id
        ORDER BY applications.created_at DESC
        """
    ).fetchall()

    hostels = db.execute(
        """
        SELECT hostels.*,
               (
                   SELECT COUNT(*)
                   FROM allocations
                   WHERE allocations.hostel_id = hostels.id
               ) AS occupied
        FROM hostels
        ORDER BY hostels.name
        """
    ).fetchall()

    return render_template(
        "admin.html",
        applications=applications,
        hostels=hostels,
    )


# ==================================================
# AUTOMATIC BED ALLOCATION
# ==================================================

def allocate_bed(db, user_id, hostel_id):
    """
    Allocate the first available bed in the selected accommodation.
    This function is called during administrator approval.
    """
    existing = db.execute(
        "SELECT id FROM allocations WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if existing:
        return

    hostel = db.execute(
        "SELECT capacity FROM hostels WHERE id = ?",
        (hostel_id,),
    ).fetchone()

    if hostel is None:
        raise ValueError("Accommodation option does not exist.")

    occupied = {
        row["bed_number"]
        for row in db.execute(
            """
            SELECT bed_number
            FROM allocations
            WHERE hostel_id = ?
            """,
            (hostel_id,),
        ).fetchall()
    }

    bed_number = next(
        (
            number
            for number in range(1, hostel["capacity"] + 1)
            if number not in occupied
        ),
        None,
    )

    if bed_number is None:
        raise ValueError(
            "No beds are available in the selected accommodation."
        )

    db.execute(
        """
        INSERT INTO allocations (user_id, hostel_id, bed_number)
        VALUES (?, ?, ?)
        """,
        (user_id, hostel_id, bed_number),
    )


# ==================================================
# ADMIN APPROVAL AND REJECTION
# ==================================================

@app.route(
    "/admin/application/<int:application_id>/<action>",
    methods=["POST"],
)
@login_required
@admin_required
def update_application(application_id, action):
    if action not in ("approve", "reject"):
        abort(400)

    db = get_db()

    try:
        # Prevent concurrent approval requests from selecting
        # the same available bed.
        db.execute("BEGIN IMMEDIATE")

        application = db.execute(
            """
            SELECT *
            FROM applications
            WHERE id = ?
            """,
            (application_id,),
        ).fetchone()

        if application is None:
            db.rollback()
            abort(404)

        if action == "approve":
            allocate_bed(
                db,
                application["user_id"],
                application["hostel_id"],
            )

            db.execute(
                """
                UPDATE applications
                SET status = 'approved'
                WHERE id = ?
                """,
                (application_id,),
            )

        else:
            db.execute(
                """
                DELETE FROM allocations
                WHERE user_id = ?
                """,
                (application["user_id"],),
            )

            db.execute(
                """
                UPDATE applications
                SET status = 'rejected'
                WHERE id = ?
                """,
                (application_id,),
            )

        db.commit()

        if action == "approve":
            flash(
                "Application approved and bed allocation processed.",
                "success",
            )
        else:
            flash("Application rejected.", "success")

    except ValueError as error:
        db.rollback()
        flash(str(error), "error")

    except sqlite3.IntegrityError:
        db.rollback()
        flash(
            "Allocation conflict. Please review this application.",
            "error",
        )

    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Database error updating application.")
        flash(
            "A database error occurred. Please try again.",
            "error",
        )

    return redirect(url_for("admin"))


# ==================================================
# START APPLICATION
# ==================================================

init_db()

if __name__ == "__main__":
    app.run(debug=False)
