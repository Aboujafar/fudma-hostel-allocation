import base64
import json
import os
import re
import psycopg2
import psycopg2.extras
import threading
import urllib.error
import urllib.request
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
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf.csrf import CSRFProtect


# ==================================================
# APPLICATION CONFIGURATION
# ==================================================

BASE_DIR = Path(__file__).resolve().parent

# Postgres connection string (Neon, Supabase, Render Postgres...).
# Set DATABASE_URL in Render > Environment. Keep it private.
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "Set DATABASE_URL before starting the application."
    )

app = Flask(__name__)

# Render sits behind a proxy/HTTPS terminator. ProxyFix lets Flask
# see the real scheme and host so secure cookies and redirects work.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

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

# Set LOGIN_DEBUG=1 in Render only while troubleshooting login.
LOGIN_DEBUG = os.environ.get("LOGIN_DEBUG", "0") == "1"

csrf = CSRFProtect(app)


# ==================================================
# EMAIL NOTIFICATIONS (Mailjet HTTPS API)
# ==================================================
# Render's free plan blocks SMTP ports, so emails are sent through
# Mailjet's HTTPS API. Set these in Render > Environment:
#   MAILJET_API_KEY     - API key from your Mailjet account
#   MAILJET_SECRET_KEY  - Secret key from your Mailjet account
#   MAIL_SENDER         - sender email verified in Mailjet
#   MAIL_SENDER_NAME    - optional, defaults to "FUDMA Hostels"
# If any required value is missing, emails are skipped and the
# app keeps working normally.

def _send_email_now(to_email, to_name, subject, html_body):
    api_key = os.environ.get("MAILJET_API_KEY", "")
    secret_key = os.environ.get("MAILJET_SECRET_KEY", "")
    sender = os.environ.get("MAIL_SENDER", "")
    sender_name = os.environ.get("MAIL_SENDER_NAME", "FUDMA Hostels")

    if not api_key or not secret_key or not sender or not to_email:
        print("Email skipped: missing configuration or recipient.", flush=True)
        return

    payload = json.dumps({
        "Messages": [
            {
                "From": {"Email": sender, "Name": sender_name},
                "To": [{"Email": to_email, "Name": to_name or to_email}],
                "Subject": subject,
                "HTMLPart": html_body,
            }
        ]
    }).encode("utf-8")

    credentials = base64.b64encode(
        f"{api_key}:{secret_key}".encode("utf-8")
    ).decode("ascii")

    request_obj = urllib.request.Request(
        "https://api.mailjet.com/v3.1/send",
        data=payload,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            print(f"Email sent (status {response.status}).", flush=True)
    except urllib.error.HTTPError as error:
        print(f"Email failed: HTTP {error.code}", flush=True)
    except Exception as error:
        print(f"Email failed: {type(error).__name__}", flush=True)


def send_email(to_email, to_name, subject, html_body):
    """Send in a background thread so pages never wait on email."""
    threading.Thread(
        target=_send_email_now,
        args=(to_email, to_name, subject, html_body),
        daemon=True,
    ).start()


def notify_application_result(db, application_id):
    """Email the student after an admin approves or rejects."""
    row = run(db,
        """
        SELECT users.full_name,
               users.email,
               applications.status,
               hostels.name AS hostel_name,
               allocations.bed_number
        FROM applications
        JOIN users ON users.id = applications.user_id
        JOIN hostels ON hostels.id = applications.hostel_id
        LEFT JOIN allocations
            ON allocations.user_id = applications.user_id
        WHERE applications.id = %s
        """,
        (application_id,),
    ).fetchone()

    if row is None or not row["email"]:
        return

    name = row["full_name"]

    if row["status"] == "approved":
        subject = "Your FUDMA hostel application has been approved"
        body = (
            f"<p>Dear {name},</p>"
            f"<p>Your application has been <b>approved</b>.</p>"
            f"<p>Hostel: <b>{row['hostel_name']}</b><br>"
            f"Bed number: <b>{row['bed_number']}</b></p>"
            f"<p>Log in to your dashboard for details.</p>"
            f"<p>FUDMA Hostels</p>"
        )
    else:
        subject = "Update on your FUDMA hostel application"
        body = (
            f"<p>Dear {name},</p>"
            f"<p>We are sorry to inform you that your application for "
            f"<b>{row['hostel_name']}</b> was not approved.</p>"
            f"<p>Please contact the hostel administration for more "
            f"information.</p><p>FUDMA Hostels</p>"
        )

    send_email(row["email"], name, subject, body)


# ==================================================
# DATABASE CONNECTION (Postgres)
# ==================================================

def connect():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=10,
    )


def run(db, sql, params=()):
    """Execute a query and return the cursor (rows behave like dicts)."""
    cur = db.cursor()
    cur.execute(sql, params if params else None)
    return cur


def get_db():
    if "db" not in g:
        g.db = connect()

    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)

    if db is not None:
        db.close()


# ==================================================
# ADMIN SEEDING
# ==================================================

def seed_admin(db):
    """
    Create an administrator account from environment variables
    (ADMIN_MATRIC and ADMIN_PASSWORD) if it does not exist yet.
    Change the password after the first login.
    """
    matric = os.environ.get("ADMIN_MATRIC", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")

    if not matric or not password:
        print(
            "Admin seed skipped: ADMIN_MATRIC / ADMIN_PASSWORD not set.",
            flush=True,
        )
        return

    exists = run(
        db,
        "SELECT 1 FROM users WHERE matric_number = %s",
        (matric,),
    ).fetchone()

    if exists:
        return

    run(
        db,
        """
        INSERT INTO users (
            full_name, matric_number, department, level,
            email, student_contact, next_of_kin_name,
            next_of_kin_contact, password_hash, role
        )
        VALUES (%s, %s, 'Administration', 'N/A', '', '', '', '', %s, 'admin')
        """,
        (
            "System Administrator",
            matric,
            generate_password_hash(password),
        ),
    )

    print("Admin account created.", flush=True)


# ==================================================
# DATABASE INITIALIZATION
# ==================================================

def init_db():
    """Create missing tables. Existing data is preserved."""
    db = connect()

    try:
        # Stops several workers from creating tables at the same time.
        run(db, "SELECT pg_advisory_xact_lock(748213)")

        run(db, """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                full_name TEXT NOT NULL,
                matric_number TEXT NOT NULL UNIQUE,
                department TEXT NOT NULL,
                level TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                student_contact TEXT NOT NULL DEFAULT '',
                next_of_kin_name TEXT NOT NULL DEFAULT '',
                next_of_kin_contact TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'student'
                    CHECK(role IN ('student', 'admin'))
            );

            CREATE TABLE IF NOT EXISTS hostels (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                capacity INTEGER NOT NULL CHECK(capacity >= 0)
            );

            CREATE TABLE IF NOT EXISTS applications (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
                hostel_id INTEGER NOT NULL REFERENCES hostels(id),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL DEFAULT
                    (to_char(CURRENT_TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS'))
            );

            CREATE TABLE IF NOT EXISTS allocations (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
                hostel_id INTEGER NOT NULL REFERENCES hostels(id),
                bed_number INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT
                    (to_char(CURRENT_TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS')),
                UNIQUE(hostel_id, bed_number)
            );

            CREATE INDEX IF NOT EXISTS idx_users_email
                ON users(email);

            CREATE INDEX IF NOT EXISTS idx_applications_status
                ON applications(status);
        """)

        # Initial options for a new database only.
        # Verify actual accommodation capacities before production use.
        for name in (
            "Darawa Hostel",
            "Hayin Gada Hostel",
            "Isa Kaita Hostel",
        ):
            run(
                db,
                """
                INSERT INTO hostels (name, capacity)
                VALUES (%s, %s)
                ON CONFLICT (name) DO NOTHING
                """,
                (name, 100),
            )

        seed_admin(db)

        db.commit()
        print("Database initialized (postgres).", flush=True)

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

    hostels = run(db,
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
        existing_user = run(db,
            """
            SELECT matric_number, email
            FROM users
            WHERE matric_number = %s
               OR (
                   email <> ''
                   AND lower(email) = lower(%s)
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
            run(db,
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'student')
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

        except psycopg2.IntegrityError:
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

        user = run(db,
            """
            SELECT *
            FROM users
            WHERE matric_number = %s
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

        # Only printed when LOGIN_DEBUG=1. Never logs passwords or hashes.
        if LOGIN_DEBUG:
            print(
                "LOGIN DEBUG:",
                "ID =", repr(matric),
                "USER FOUND =", bool(user),
                "PASSWORD MATCH =", password_matches,
                "DATABASE = postgres",
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

    student = run(db,
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
        WHERE id = %s
        """,
        (session["user_id"],),
    ).fetchone()

    application = run(db,
        """
        SELECT applications.*,
               hostels.name AS hostel_name,
               allocations.bed_number
        FROM applications
        JOIN hostels
            ON hostels.id = applications.hostel_id
        LEFT JOIN allocations
            ON allocations.user_id = applications.user_id
        WHERE applications.user_id = %s
        """,
        (session["user_id"],),
    ).fetchone()

    hostels = run(db,
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

    hostel = run(db,
        "SELECT id, name FROM hostels WHERE id = %s",
        (hostel_id,),
    ).fetchone()

    if not hostel:
        flash("Please select a valid accommodation option.", "error")
        return redirect(url_for("dashboard"))

    try:
        run(db,
            """
            INSERT INTO applications (user_id, hostel_id)
            VALUES (%s, %s)
            """,
            (session["user_id"], hostel_id),
        )

        db.commit()
        flash("Application submitted successfully.", "success")

        applicant = run(db,
            "SELECT full_name, email FROM users WHERE id = %s",
            (session["user_id"],),
        ).fetchone()

        if applicant and applicant["email"]:
            send_email(
                applicant["email"],
                applicant["full_name"],
                "FUDMA hostel application received",
                (
                    f"<p>Dear {applicant['full_name']},</p>"
                    f"<p>We have received your application for "
                    f"<b>{hostel['name']}</b>. Its status is "
                    f"<b>pending</b>. You will get another email once "
                    f"it has been reviewed.</p><p>FUDMA Hostels</p>"
                ),
            )

    except psycopg2.IntegrityError:
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

    applications = run(db,
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

    hostels = run(db,
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
    existing = run(db,
        "SELECT id FROM allocations WHERE user_id = %s",
        (user_id,),
    ).fetchone()

    if existing:
        return

    hostel = run(db,
        "SELECT capacity FROM hostels WHERE id = %s FOR UPDATE",
        (hostel_id,),
    ).fetchone()

    if hostel is None:
        raise ValueError("Accommodation option does not exist.")

    occupied = {
        row["bed_number"]
        for row in run(db,
            """
            SELECT bed_number
            FROM allocations
            WHERE hostel_id = %s
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

    run(db,
        """
        INSERT INTO allocations (user_id, hostel_id, bed_number)
        VALUES (%s, %s, %s)
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
        application = run(db,
            """
            SELECT *
            FROM applications
            WHERE id = %s
            FOR UPDATE
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

            run(db,
                """
                UPDATE applications
                SET status = 'approved'
                WHERE id = %s
                """,
                (application_id,),
            )

        else:
            run(db,
                """
                DELETE FROM allocations
                WHERE user_id = %s
                """,
                (application["user_id"],),
            )

            run(db,
                """
                UPDATE applications
                SET status = 'rejected'
                WHERE id = %s
                """,
                (application_id,),
            )

        db.commit()

        notify_application_result(db, application_id)

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

    except psycopg2.IntegrityError:
        db.rollback()
        flash(
            "Allocation conflict. Please review this application.",
            "error",
        )

    except psycopg2.Error:
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