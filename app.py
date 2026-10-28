from flask import Flask, render_template, request, redirect, url_for, flash, session
import sqlite3
from pathlib import Path
from datetime import datetime
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "local-development-only")
DB_PATH = Path(__file__).with_name("rural_power.db")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS villages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            district TEXT NOT NULL,
            households_total INTEGER NOT NULL DEFAULT 0,
            households_electrified INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS residents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            address TEXT NOT NULL,
            village_id INTEGER NOT NULL,
            consumer_number TEXT UNIQUE,
            FOREIGN KEY(village_id) REFERENCES villages(id)
        );
        CREATE TABLE IF NOT EXISTS technicians (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            specialization TEXT NOT NULL DEFAULT 'General'
        );
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resident_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'Normal',
            status TEXT NOT NULL DEFAULT 'Submitted',
            technician_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolution_note TEXT DEFAULT '',
            FOREIGN KEY(resident_id) REFERENCES residents(id),
            FOREIGN KEY(technician_id) REFERENCES technicians(id)
        );
        CREATE TABLE IF NOT EXISTS outages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            village_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            started_at TEXT NOT NULL,
            restored_at TEXT,
            affected_households INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(village_id) REFERENCES villages(id)
        );
        CREATE TABLE IF NOT EXISTS connection_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resident_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            village_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending',
            submitted_at TEXT NOT NULL,
            FOREIGN KEY(village_id) REFERENCES villages(id)
        );
        """)

        if con.execute("SELECT COUNT(*) FROM villages").fetchone()[0] == 0:
            con.executemany(
                "INSERT INTO villages(name,district,households_total,households_electrified) VALUES(?,?,?,?)",
                [
                    ("Kallipalayam", "Karur", 420, 385),
                    ("Velliyanai", "Karur", 560, 510),
                    ("Puthur", "Tiruchirappalli", 310, 250),
                ],
            )
            con.executemany(
                "INSERT INTO residents(name,phone,address,village_id,consumer_number) VALUES(?,?,?,?,?)",
                [
                    ("Kannan", "9000000001", "North Street", 1, "KR00125"),
                    ("Meena", "9000000002", "Lake Road", 2, "KR00241"),
                    ("Arun", "9000000003", "Main Road", 3, "TR00309"),
                ],
            )
            con.executemany(
                "INSERT INTO technicians(name,phone,specialization) VALUES(?,?,?)",
                [
                    ("R. Kumar", "9111111111", "Line maintenance"),
                    ("S. Priya", "9222222222", "Meter and connection"),
                ],
            )
            now = datetime.now().isoformat(timespec="minutes")
            con.execute("""INSERT INTO complaints
                (resident_id,category,description,priority,status,technician_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (1, "Power outage", "No electricity since evening.", "High", "Assigned", 1, now, now))
            con.execute("""INSERT INTO outages
                (village_id,reason,started_at,affected_households) VALUES(?,?,?,?)""",
                (1, "Feeder maintenance", now, 65))

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            flash("Please sign in as administrator to access that page.", "warning")
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper

@app.route("/")
def index():
    with db() as con:
        stats = {
            "complaints": con.execute("SELECT COUNT(*) FROM complaints").fetchone()[0],
            "pending": con.execute("SELECT COUNT(*) FROM complaints WHERE status NOT IN ('Resolved','Closed')").fetchone()[0],
            "outages": con.execute("SELECT COUNT(*) FROM outages WHERE restored_at IS NULL").fetchone()[0],
            "villages": con.execute("SELECT COUNT(*) FROM villages").fetchone()[0],
        }
        recent = con.execute("""SELECT c.*, r.name AS resident_name, v.name AS village_name
            FROM complaints c JOIN residents r ON r.id=c.resident_id
            JOIN villages v ON v.id=r.village_id ORDER BY c.id DESC LIMIT 6""").fetchall()
        villages = con.execute("SELECT * FROM villages ORDER BY name").fetchall()
    return render_template("index.html", stats=stats, recent=recent, villages=villages)

@app.route("/complaints/new", methods=["GET", "POST"])
def new_complaint():
    with db() as con:
        residents = con.execute("""SELECT r.*, v.name AS village_name FROM residents r
            JOIN villages v ON v.id=r.village_id ORDER BY r.name""").fetchall()
    if request.method == "POST":
        resident_id = request.form.get("resident_id", type=int)
        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()
        priority = request.form.get("priority", "Normal")
        if not resident_id or not category or not description:
            flash("Please complete all required fields.", "danger")
            return render_template("new_complaint.html", residents=residents)
        now = datetime.now().isoformat(timespec="minutes")
        with db() as con:
            con.execute("""INSERT INTO complaints
                (resident_id,category,description,priority,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                (resident_id, category, description, priority, "Submitted", now, now))
            complaint_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        flash(f"Complaint submitted successfully. Your complaint ID is #{complaint_id}.", "success")
        return redirect(url_for("track_complaint", complaint_id=complaint_id))
    return render_template("new_complaint.html", residents=residents)

@app.route("/track", methods=["GET", "POST"])
def track():
    if request.method == "POST":
        complaint_id = request.form.get("complaint_id", "").strip()
        return redirect(url_for("track_complaint", complaint_id=complaint_id))
    return render_template("track.html", complaint=None)

@app.route("/track/<complaint_id>")
def track_complaint(complaint_id):
    complaint = None
    if complaint_id.isdigit():
        with db() as con:
            complaint = con.execute("""SELECT c.*, r.name AS resident_name, r.consumer_number,
                v.name AS village_name, t.name AS technician_name
                FROM complaints c JOIN residents r ON r.id=c.resident_id
                JOIN villages v ON v.id=r.village_id
                LEFT JOIN technicians t ON t.id=c.technician_id WHERE c.id=?""",
                (int(complaint_id),)).fetchone()
    if not complaint:
        flash("No complaint found with that ID.", "warning")
    return render_template("track.html", complaint=complaint)

@app.route("/connections/new", methods=["GET", "POST"])
def connection_request():
    with db() as con:
        villages = con.execute("SELECT * FROM villages ORDER BY name").fetchall()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        village_id = request.form.get("village_id", type=int)
        address = request.form.get("address", "").strip()
        if not all([name, phone, village_id, address]):
            flash("Please complete all fields.", "danger")
        else:
            with db() as con:
                con.execute("""INSERT INTO connection_requests
                    (resident_name,phone,village_id,address,submitted_at)
                    VALUES(?,?,?,?,?)""",
                    (name, phone, village_id, address, datetime.now().isoformat(timespec="minutes")))
            flash("New connection request submitted.", "success")
            return redirect(url_for("index"))
    return render_template("connection_request.html", villages=villages)

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        # Demo credentials only. Replace with hashed passwords and environment variables before deployment.
        if request.form.get("username") == "admin" and request.form.get("password") == "rural123":
            session["admin"] = True
            flash("Signed in successfully.", "success")
            return redirect(url_for("admin_dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("You have signed out.", "info")
    return redirect(url_for("index"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    with db() as con:
        complaints = con.execute("""SELECT c.*, r.name AS resident_name, v.name AS village_name,
            t.name AS technician_name FROM complaints c JOIN residents r ON r.id=c.resident_id
            JOIN villages v ON v.id=r.village_id LEFT JOIN technicians t ON t.id=c.technician_id
            ORDER BY CASE c.status WHEN 'Submitted' THEN 0 WHEN 'Assigned' THEN 1
            WHEN 'In Progress' THEN 2 ELSE 3 END, c.id DESC""").fetchall()
        technicians = con.execute("SELECT * FROM technicians ORDER BY name").fetchall()
        connections = con.execute("""SELECT cr.*, v.name AS village_name FROM connection_requests cr
            JOIN villages v ON v.id=cr.village_id ORDER BY cr.id DESC""").fetchall()
        outages = con.execute("""SELECT o.*, v.name AS village_name FROM outages o
            JOIN villages v ON v.id=o.village_id ORDER BY o.id DESC""").fetchall()
        villages = con.execute("SELECT * FROM villages ORDER BY name").fetchall()
    return render_template("admin_dashboard.html", complaints=complaints,
        technicians=technicians, connections=connections, outages=outages, villages=villages)

@app.route("/admin/complaint/<int:complaint_id>/update", methods=["POST"])
@admin_required
def update_complaint(complaint_id):
    status = request.form.get("status", "Submitted")
    technician_id = request.form.get("technician_id", type=int) or None
    note = request.form.get("resolution_note", "").strip()
    allowed = {"Submitted", "Assigned", "In Progress", "Resolved", "Closed"}
    if status not in allowed:
        flash("Invalid status.", "danger")
        return redirect(url_for("admin_dashboard"))
    with db() as con:
        con.execute("""UPDATE complaints SET status=?, technician_id=?, resolution_note=?, updated_at=?
            WHERE id=?""", (status, technician_id, note, datetime.now().isoformat(timespec="minutes"), complaint_id))
    flash(f"Complaint #{complaint_id} updated.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/outage/new", methods=["POST"])
@admin_required
def add_outage():
    village_id = request.form.get("village_id", type=int)
    reason = request.form.get("reason", "").strip()
    affected = request.form.get("affected_households", type=int) or 0
    if not village_id or not reason:
        flash("Choose a village and enter an outage reason.", "danger")
    else:
        with db() as con:
            con.execute("INSERT INTO outages(village_id,reason,started_at,affected_households) VALUES(?,?,?,?)",
                (village_id, reason, datetime.now().isoformat(timespec="minutes"), max(0, affected)))
        flash("Outage recorded.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/outage/<int:outage_id>/restore", methods=["POST"])
@admin_required
def restore_outage(outage_id):
    with db() as con:
        con.execute("UPDATE outages SET restored_at=? WHERE id=? AND restored_at IS NULL",
                    (datetime.now().isoformat(timespec="minutes"), outage_id))
    flash("Outage marked as restored.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/connection/<int:request_id>/update", methods=["POST"])
@admin_required
def update_connection(request_id):
    status = request.form.get("status", "Pending")
    if status not in {"Pending", "Under Review", "Approved", "Rejected", "Completed"}:
        status = "Pending"
    with db() as con:
        con.execute("UPDATE connection_requests SET status=? WHERE id=?", (status, request_id))
    flash("Connection request updated.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/health")
def health():
    return {"status": "ok", "application": "Rural Power Complaint System"}

init_db()

if __name__ == "__main__":
    app.run(debug=True)
