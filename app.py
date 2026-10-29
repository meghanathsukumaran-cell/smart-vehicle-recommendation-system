from flask import Flask, jsonify, render_template, request, redirect, url_for, flash, session
import os
import sys
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import json
import sqlite3
from pathlib import Path

app = Flask(__name__)
app.secret_key = os.environ.get("SMARTVEHICLE_SECRET_KEY", "change-this-development-key-before-deployment")

# Vehicle data is stored in SQLite. vehicles.json seeds the database on first run.
DATA_FILE = Path(__file__).with_name("vehicles.json")
DB_FILE = Path(__file__).with_name("vehicles.db")


def get_db_connection():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    """Create the SQLite vehicle table and import JSON records only if it is empty."""
    with get_db_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS vehicles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price REAL,
                fuel TEXT,
                image TEXT,
                data_json TEXT NOT NULL
            )
        """)
        count = connection.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        if count == 0:
            if not DATA_FILE.exists():
                raise FileNotFoundError(
                    "vehicles.json is missing and the SQLite vehicle table is empty."
                )
            with DATA_FILE.open("r", encoding="utf-8") as file:
                records = json.load(file)
            for vehicle in records:
                connection.execute(
                    """INSERT OR REPLACE INTO vehicles
                       (id, name, price, fuel, image, data_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        vehicle.get("id"),
                        vehicle.get("name", "Unknown vehicle"),
                        vehicle.get("price"),
                        vehicle.get("fuel"),
                        vehicle.get("image", ""),
                        json.dumps(vehicle, ensure_ascii=False),
                    ),
                )


def load_vehicles():
    """Load vehicle dictionaries from SQLite."""
    init_db()
    with get_db_connection() as connection:
        rows = connection.execute(
            "SELECT data_json FROM vehicles ORDER BY name"
        ).fetchall()
    return [json.loads(row["data_json"]) for row in rows]


# Create the database and seed it from vehicles.json on first run.
init_db()

def init_user_tables():
    """Create account and personal-feature tables without altering vehicle records."""
    with get_db_connection() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS favourites (
                user_id INTEGER NOT NULL,
                vehicle_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, vehicle_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS recent_views (
                user_id INTEGER NOT NULL,
                vehicle_id TEXT NOT NULL,
                viewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, vehicle_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS saved_emi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                vehicle_label TEXT NOT NULL,
                salary REAL NOT NULL,
                price REAL NOT NULL,
                down_payment REAL NOT NULL,
                interest REAL NOT NULL,
                years INTEGER NOT NULL,
                monthly_emi REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)
        connection.execute("PRAGMA foreign_keys = ON")

init_user_tables()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    with get_db_connection() as connection:
        row = connection.execute(
            "SELECT id, name, email, role, active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row or not row["active"]:
        session.clear()
        return None
    return dict(row)


@app.context_processor
def inject_user():
    return {"logged_in_user": current_user()}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Please log in to use that feature.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            flash("Please log in with an administrator account.", "info")
            return redirect(url_for("login", next=request.path))
        if user["role"] != "admin":
            flash("Administrator access is required.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


# Official manufacturer pages. These are manually maintained links, not live API data.
OFFICIAL_MAKE_URLS = {
    "hyundai": "https://www.hyundai.com/in/en",
    "tata": "https://cars.tatamotors.com/",
    "maruti": "https://www.marutisuzuki.com/",
    "kia": "https://www.kia.com/in/home.html",
    "toyota": "https://www.toyotabharat.com/",
    "mahindra": "https://auto.mahindra.com/",
}


def local_vehicle_search(query=""):
    """Search the local demo dataset, like a tiny vehicle API would."""
    q = query.strip().lower()
    results = []
    for car in prepare_vehicles():
        if q and q not in car["name"].lower() and q not in car["fuel"].lower():
            continue
        make = car["name"].split()[0].lower()
        car["official_url"] = next(
            (url for brand, url in OFFICIAL_MAKE_URLS.items() if brand in make), None
        )
        results.append(car)
    return results


def add_value_scores(vehicles):
    max_mileage = max(v["mileage"] for v in vehicles)
    max_power = max(v["power"] for v in vehicles)
    max_price = max(v["price"] for v in vehicles)
    min_price = min(v["price"] for v in vehicles)
    for car in vehicles:
        price_score = 100 if max_price == min_price else 100 * (max_price - car["price"]) / (max_price - min_price)
        mileage_score = 100 * car["mileage"] / max_mileage
        power_score = 100 * car["power"] / max_power
        car["value_score"] = round(0.5 * price_score + 0.35 * mileage_score + 0.15 * power_score)
        car["value_label"] = "Illustrative value score; not an official rating"


def budget_matches(budget, price_lakh):
    ranges = {"under5": (0, 5), "5to8": (5, 8), "8to12": (8, 12),
              "12to20": (12, 20), "above20": (20, float("inf"))}
    if budget not in ranges:
        return None
    low, high = ranges[budget]
    return low <= price_lakh < high


def cc_matches(choice, cc):
    ranges = {"under1000": (0, 1000), "1000to1200": (1000, 1200),
              "1200to1500": (1200, 1500), "1500to2000": (1500, 2000),
              "above2000": (2000, float("inf"))}
    if choice not in ranges:
        return None
    low, high = ranges[choice]
    return cc < high if choice == "under1000" else cc > low if choice == "above2000" else low <= cc <= high


def score_vehicle(car, preferences):
    points = total = 0

    def add(result, weight=1):
        nonlocal points, total
        if result is not None:
            total += weight
            if result:
                points += weight

    add(budget_matches(preferences.get("budget"), car["price"]), 3)
    add(cc_matches(preferences.get("cc"), car["cc"]), 1)
    if preferences.get("mileage"):
        add(car["mileage"] >= float(preferences["mileage"]), 2)
    if preferences.get("fuel"):
        add(car["fuel"].lower() == preferences["fuel"].lower(), 2)
    if preferences.get("transmission") and preferences["transmission"] != "Any":
        add(car["transmission"].lower() == preferences["transmission"].lower(), 1)
    if preferences.get("seats"):
        requested = 8 if preferences["seats"] == "8" else int(preferences["seats"])
        add(car["seats"] >= requested, 2)
    family_need = {"1-2": 2, "3-4": 4, "5-6": 6, "7+": 7}.get(preferences.get("family"))
    if family_need:
        add(car["seats"] >= family_need, 2)
    if preferences.get("usage"):
        add(preferences["usage"] in car["usage"], 1)
    # Safety is not scored until ratings are verified for each variant.
    return round(points * 100 / total) if total else 0


def prepare_vehicles():
    vehicles = [v.copy() for v in load_vehicles()]
    add_value_scores(vehicles)
    return vehicles



@app.route("/")
def home():
    return render_template("index.html")


@app.route("/requirements")
def requirements():
    return render_template("requirements.html")


@app.route("/results", methods=["GET", "POST"])
def results():
    fields = ["budget", "cc", "mileage", "fuel", "transmission", "seats", "safety", "family", "usage"]
    preferences = {field: request.form.get(field, "") for field in fields} if request.method == "POST" else {}
    vehicles = prepare_vehicles()
    for car in vehicles:
        car["match"] = score_vehicle(car, preferences)
    vehicles.sort(key=lambda car: car["match"], reverse=True)
    if preferences.get("fuel"):
        vehicles = [v for v in vehicles if v["fuel"].lower() == preferences["fuel"].lower()]
    return render_template("results.html", preferences=preferences, vehicles=vehicles)


@app.route("/api-vehicles", methods=["GET", "POST"])
def api_vehicles():
    query = request.values.get("q", "").strip()
    vehicles = local_vehicle_search(query)
    return render_template("api_vehicles.html", query=query, vehicles=vehicles)


@app.route("/api/vehicles", methods=["GET"])
def vehicles_api():
    """Simple JSON API endpoint for demo: /api/vehicles or /api/vehicles?q=Creta"""
    query = request.args.get("q", "").strip()
    vehicles = local_vehicle_search(query)
    # Return only simple, useful fields in the public demo API response.
    data = [{
        "id": car["id"],
        "name": car["name"],
        "price_lakh": car["price"],
        "price_label": car["price_label"],
        "engine_cc": car["cc"],
        "power_hp": car["power"],
        "mileage_kmpl": car["mileage"],
        "seats": car["seats"],
        "fuel": car["fuel"],
        "transmission": car["transmission"],
        "official_manufacturer_url": car.get("official_url"),
    } for car in vehicles]
    return jsonify({"count": len(data), "source": "local_demo_dataset", "vehicles": data})


@app.route("/vehicle-details")
def vehicle_details():
    vehicle_id = request.args.get("vehicle", "creta")
    vehicles = prepare_vehicles()
    car = next((v for v in vehicles if v["id"] == vehicle_id), vehicles[0])
    user = current_user()
    if user:
        with get_db_connection() as connection:
            connection.execute(
                """INSERT INTO recent_views (user_id, vehicle_id, viewed_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(user_id, vehicle_id) DO UPDATE SET viewed_at=CURRENT_TIMESTAMP""",
                (user["id"], car["id"]),
            )
    return render_template("vehicle_details.html", car=car)


@app.route("/compare")
def compare():
    all_vehicles = prepare_vehicles()
    requested_ids = request.args.getlist("ids")
    # Also accept comma-separated ids for compatibility with simple links.
    if len(requested_ids) == 1 and "," in requested_ids[0]:
        requested_ids = [part.strip() for part in requested_ids[0].split(",") if part.strip()]
    valid_ids = {str(car["id"]) for car in all_vehicles}
    selected_ids = list(dict.fromkeys(str(item) for item in requested_ids if str(item) in valid_ids))[:4]
    selected_set = set(selected_ids)
    selected_vehicles = [car for car in all_vehicles if str(car["id"]) in selected_set]
    return render_template(
        "compare.html",
        vehicles=selected_vehicles,
        all_vehicles=all_vehicles,
        selected_ids=selected_ids,
    )


@app.route("/emi")
def emi():
    return render_template("emi.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not name or not email or len(password) < 8:
            flash("Enter your name and email, and use a password with at least 8 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        else:
            try:
                with get_db_connection() as connection:
                    connection.execute(
                        "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                        (name, email, generate_password_hash(password)),
                    )
                flash("Registration successful. You can now log in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("That email is already registered.", "error")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with get_db_connection() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE email = ? AND active = 1", (email,)
            ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            destination = request.args.get("next", "")
            if destination.startswith("/") and not destination.startswith("//"):
                return redirect(destination)
            return redirect(url_for("admin_dashboard") if user["role"] == "admin" else url_for("dashboard"))
        flash("Invalid email or password, or the account is inactive.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    with get_db_connection() as connection:
        favourites = connection.execute(
            """SELECT v.data_json FROM favourites f JOIN vehicles v ON v.id=f.vehicle_id
               WHERE f.user_id=? ORDER BY f.created_at DESC""", (user["id"],)
        ).fetchall()
        recent = connection.execute(
            """SELECT v.data_json FROM recent_views r JOIN vehicles v ON v.id=r.vehicle_id
               WHERE r.user_id=? ORDER BY r.viewed_at DESC LIMIT 6""", (user["id"],)
        ).fetchall()
        emi_rows = connection.execute(
            "SELECT * FROM saved_emi WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
            (user["id"],)
        ).fetchall()
    return render_template("dashboard.html", user=user,
        favourites=[json.loads(row["data_json"]) for row in favourites],
        recent=[json.loads(row["data_json"]) for row in recent], saved_emi=[dict(r) for r in emi_rows])


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        new_password = request.form.get("new_password", "")
        with get_db_connection() as connection:
            try:
                if not name or not email:
                    flash("Name and email are required.", "error")
                elif new_password and len(new_password) < 8:
                    flash("New password must be at least 8 characters.", "error")
                else:
                    if new_password:
                        connection.execute(
                            "UPDATE users SET name=?, email=?, password_hash=? WHERE id=?",
                            (name, email, generate_password_hash(new_password), user["id"]),
                        )
                    else:
                        connection.execute("UPDATE users SET name=?, email=? WHERE id=?",
                                            (name, email, user["id"]))
                    flash("Profile updated.", "success")
                    return redirect(url_for("profile"))
            except sqlite3.IntegrityError:
                flash("That email is already in use.", "error")
    return render_template("profile.html", user=current_user())


@app.route("/favourites")
@login_required
def favourites():
    user = current_user()
    with get_db_connection() as connection:
        rows = connection.execute(
            """SELECT v.data_json FROM favourites f JOIN vehicles v ON v.id=f.vehicle_id
               WHERE f.user_id=? ORDER BY f.created_at DESC""", (user["id"],)
        ).fetchall()
    return render_template("favourites.html", vehicles=[json.loads(r["data_json"]) for r in rows])


@app.route("/favourites/<vehicle_id>", methods=["POST"])
@login_required
def toggle_favourite(vehicle_id):
    user = current_user()
    with get_db_connection() as connection:
        exists = connection.execute("SELECT id FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if not exists:
            flash("Vehicle not found.", "error")
            return redirect(url_for("home"))
        saved = connection.execute(
            "SELECT 1 FROM favourites WHERE user_id=? AND vehicle_id=?", (user["id"], vehicle_id)
        ).fetchone()
        if saved:
            connection.execute("DELETE FROM favourites WHERE user_id=? AND vehicle_id=?",
                               (user["id"], vehicle_id))
            flash("Removed from favourites.", "success")
        else:
            connection.execute("INSERT INTO favourites (user_id, vehicle_id) VALUES (?, ?)",
                               (user["id"], vehicle_id))
            flash("Added to favourites.", "success")
    return redirect(request.referrer if request.referrer and request.referrer.startswith("/") else url_for("favourites"))


@app.route("/save-emi", methods=["POST"])
@login_required
def save_emi():
    user = current_user()
    try:
        salary = float(request.form.get("salary", 0))
        price = float(request.form.get("price", 0))
        down = float(request.form.get("down_payment", 0))
        interest = float(request.form.get("interest", 0))
        years = int(request.form.get("years", 0))
        label = request.form.get("vehicle_label", "Custom calculation").strip()[:100] or "Custom calculation"
        if salary <= 0 or price <= 0 or down < 0 or down >= price or interest < 0 or not 1 <= years <= 10:
            raise ValueError
        loan, months, rate = price-down, years*12, interest/1200
        emi_value = loan/months if rate == 0 else loan*rate*(1+rate)**months/((1+rate)**months-1)
    except (ValueError, TypeError):
        flash("Please enter valid EMI calculation values.", "error")
        return redirect(url_for("emi"))
    with get_db_connection() as connection:
        connection.execute(
            """INSERT INTO saved_emi
               (user_id, vehicle_label, salary, price, down_payment, interest, years, monthly_emi)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user["id"], label, salary, price, down, interest, years, emi_value),
        )
    flash("EMI calculation saved to your dashboard.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    with get_db_connection() as connection:
        counts = {
            "vehicles": connection.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0],
            "users": connection.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0],
        }
        users = connection.execute(
            "SELECT id, name, email, role, active, created_at FROM users ORDER BY created_at DESC"
        ).fetchall()
    return render_template("admin.html", counts=counts, users=[dict(u) for u in users],
                           vehicles=prepare_vehicles())


@app.route("/admin/vehicles/add", methods=["POST"])
@admin_required
def admin_add_vehicle():
    try:
        vehicle = {
            "id": request.form.get("id", "").strip().lower().replace(" ", "-"),
            "name": request.form.get("name", "").strip(),
            "price": float(request.form.get("price", 0)),
            "price_label": request.form.get("price_label", "").strip(),
            "fuel": request.form.get("fuel", "Petrol").strip(),
            "cc": int(request.form.get("cc", 0)),
            "power": float(request.form.get("power", 0)),
            "mileage": float(request.form.get("mileage", 0)),
            "seats": int(request.form.get("seats", 5)),
            "transmission": request.form.get("transmission", "Manual").strip(),
            "usage": request.form.get("usage", "City").strip(),
            "image": request.form.get("image", "").strip(),
        }
        if not vehicle["id"] or not vehicle["name"] or min(vehicle["price"], vehicle["cc"], vehicle["power"], vehicle["mileage"]) <= 0:
            raise ValueError
        with get_db_connection() as connection:
            connection.execute(
                """INSERT INTO vehicles (id,name,price,fuel,image,data_json)
                   VALUES (?,?,?,?,?,?)""",
                (vehicle["id"], vehicle["name"], vehicle["price"], vehicle["fuel"],
                 vehicle["image"], json.dumps(vehicle, ensure_ascii=False)),
            )
        flash("Vehicle added.", "success")
    except (ValueError, sqlite3.IntegrityError):
        flash("Could not add vehicle. Check all fields and ensure the vehicle ID is unique.", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/vehicles/<vehicle_id>/edit", methods=["POST"])
@admin_required
def admin_edit_vehicle(vehicle_id):
    """Update editable vehicle fields while preserving other fields in data_json."""
    try:
        name = request.form.get("name", "").strip()
        price = float(request.form.get("price", 0))
        price_label = request.form.get("price_label", "").strip()
        fuel = request.form.get("fuel", "Petrol").strip()
        cc = int(request.form.get("cc", 0))
        power = float(request.form.get("power", 0))
        mileage = float(request.form.get("mileage", 0))
        seats = int(request.form.get("seats", 5))
        transmission = request.form.get("transmission", "Manual").strip()
        usage = request.form.get("usage", "City").strip()
        image = request.form.get("image", "").strip()

        if not name or min(price, cc, power, mileage, seats) <= 0:
            raise ValueError

        with get_db_connection() as connection:
            row = connection.execute(
                "SELECT data_json FROM vehicles WHERE id=?", (vehicle_id,)
            ).fetchone()
            if not row:
                flash("Vehicle not found.", "error")
                return redirect(url_for("admin_dashboard"))

            vehicle = json.loads(row["data_json"])
            vehicle.update({
                "name": name, "price": price, "price_label": price_label,
                "fuel": fuel, "cc": cc, "power": power, "mileage": mileage,
                "seats": seats, "transmission": transmission, "usage": usage,
                "image": image,
            })
            connection.execute(
                """UPDATE vehicles SET name=?, price=?, fuel=?, image=?, data_json=?
                   WHERE id=?""",
                (name, price, fuel, image, json.dumps(vehicle, ensure_ascii=False), vehicle_id),
            )
        flash("Vehicle updated successfully.", "success")
    except (ValueError, TypeError):
        flash("Could not update vehicle. Check that numeric fields contain valid positive values.", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/vehicles/<vehicle_id>/delete", methods=["POST"])
@admin_required
def admin_delete_vehicle(vehicle_id):
    with get_db_connection() as connection:
        connection.execute("DELETE FROM vehicles WHERE id=?", (vehicle_id,))
        connection.execute("DELETE FROM favourites WHERE vehicle_id=?", (vehicle_id,))
        connection.execute("DELETE FROM recent_views WHERE vehicle_id=?", (vehicle_id,))
    flash("Vehicle deleted if it existed.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(user_id):
    with get_db_connection() as connection:
        target = connection.execute("SELECT id, role, active FROM users WHERE id=?", (user_id,)).fetchone()
        if target and target["role"] != "admin":
            connection.execute("UPDATE users SET active=? WHERE id=?",
                               (0 if target["active"] else 1, user_id))
            flash("User account status updated.", "success")
        else:
            flash("Admin accounts cannot be deactivated here.", "error")
    return redirect(url_for("admin_dashboard"))


def create_admin_cli():
    import getpass
    name = input("Admin name: ").strip()
    email = input("Admin email: ").strip().lower()
    password = getpass.getpass("Admin password (minimum 8 characters): ")
    if not name or not email or len(password) < 8:
        print("Name/email required and password must be at least 8 characters.")
        return
    try:
        with get_db_connection() as connection:
            connection.execute(
                "INSERT INTO users (name,email,password_hash,role) VALUES (?,?,?,'admin')",
                (name, email, generate_password_hash(password)),
            )
        print("Admin account created. Run 'py app.py' to start the website.")
    except sqlite3.IntegrityError:
        print("That email is already registered.")


if __name__ == "__main__":
    if "--create-admin" in sys.argv:
        create_admin_cli()
    else:
        app.run(debug=True)
