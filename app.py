import tempfile, os, json, sqlite3, uuid
from datetime import date
from functools import wraps
from flask import jsonify, Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
import matcher, demo, config, threading
from PIL import Image

DEMO = True   # sample found items + test_photos folder. Set False for real use.

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "findit.db")
UPLOADS = os.path.join(BASE, "static", "uploads")
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__)
app.secret_key = "change-this-secret-key"
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

CATEGORIES = ["Electronics", "Accessories", "Books", "Clothing", "Documents", "Bags", "Keys", "Bottles", "Jewellery", "Other"]
COLOURS = ["Gold", "Black", "White", "Grey", "Red", "Orange", "Yellow", "Green", "Blue", "Purple", "Brown"]
LOCATIONS = ["Library", "Canteen", "Admin Block", "CSE Block", "ECE Block", "Mechanical Block", "Hostel", "Ground", "Bus Stand", "Other"]
ALLOWED = {"png", "jpg", "jpeg", "webp"}

def shrink(path):
    """Downscale big phone photos (faster + smaller)."""
    im = Image.open(path).convert("RGB"); im.thumbnail((1024, 1024)); im.save(path, "JPEG", quality=90)

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    d = g.pop("db", None)
    if d: d.close()

def init_db():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, phone TEXT,
        college TEXT, password TEXT, is_admin INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, user_id INTEGER, item_name TEXT, item_type TEXT,
        category TEXT, colour TEXT, description TEXT, location TEXT, image_path TEXT, report_date TEXT,
        status TEXT DEFAULT 'Open', embedding TEXT, method TEXT);
    CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY, item_id INTEGER, claimer_id INTEGER,
        message TEXT, status TEXT DEFAULT 'Pending');
    CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, user_id INTEGER, lost_id INTEGER, found_id INTEGER,
        score INTEGER DEFAULT 0, seen INTEGER DEFAULT 0, UNIQUE(lost_id, found_id));
    CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER, message TEXT, sent_to TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    for col in ("score INTEGER DEFAULT 0", "seen INTEGER DEFAULT 0"):
        try: c.execute("ALTER TABLE alerts ADD COLUMN " + col)
        except sqlite3.OperationalError: pass
    if not c.execute("SELECT 1 FROM users WHERE is_admin=1").fetchone():
        c.execute("INSERT INTO users(name,email,college,password,is_admin) VALUES(?,?,?,?,1)",
                  ("Admin", "admin@findit.com", "College", generate_password_hash("admin123")))
    c.commit(); c.close()

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if "uid" not in session:
            flash("Please login first.")
            return redirect(url_for("login"))
        return f(*a, **k)
    return w

@app.context_processor
def inject():
    return dict(me=session.get("name"), is_admin=session.get("admin"),
                CATEGORIES=CATEGORIES, COLOURS=COLOURS, LOCATIONS=LOCATIONS)

def find_matches(item):
    other = "Found" if item["item_type"] == "Lost" else "Lost"
    rows = db().execute("SELECT * FROM items WHERE item_type=? AND status='Open' AND id!=?", (other, item["id"])).fetchall()
    out = []
    v1 = json.loads(item["embedding"])
    for r in rows:
        if r["method"] != item["method"]:
            continue
        v = matcher.visual_score(v1, json.loads(r["embedding"]), item["method"])
        out.append((matcher.final_score(v, item, r), r))
    out.sort(key=lambda x: -x[0])
    return [(s, r) for s, r in out if s >= 20][:8]

ALERT_MIN = {"clip": 60, "fg": 65}
SPOT_MIN = {"clip": 70, "fg": 60}     # camera "spotted" threshold

def match_json(lost, s, f):
    return dict(lost_id=lost["id"], lost_name=lost["item_name"], found_id=f["id"], found_name=f["item_name"],
                location=f["location"], date=f["report_date"], score=s,
                image=url_for("static", filename=f["image_path"]), link=url_for("item", item_id=f["id"]))

def _send_email(to, subject, body):
    try:
        import smtplib
        from email.message import EmailMessage
        m = EmailMessage(); m["From"] = config.EMAIL_USER; m["To"] = to; m["Subject"] = subject; m.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(config.EMAIL_USER, config.EMAIL_APP_PASSWORD); s.send_message(m)
        print("[EMAIL SENT]", to)
    except Exception as ex:
        print("[EMAIL FAILED]", ex)

def _send_sms(to, body):
    try:
        import urllib.request, urllib.parse, base64
        num = to if to.startswith("+") else config.DEFAULT_COUNTRY_CODE + to.lstrip("0")
        rq = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_SID}/Messages.json",
                                    urllib.parse.urlencode({"To": num, "From": config.TWILIO_FROM, "Body": body}).encode())
        rq.add_header("Authorization", "Basic " + base64.b64encode(f"{config.TWILIO_SID}:{config.TWILIO_TOKEN}".encode()).decode())
        urllib.request.urlopen(rq, timeout=15); print("[SMS SENT]", num)
    except Exception as ex:
        print("[SMS FAILED]", ex)

def notify(uid, subject, body):
    """Email + SMS to the user's registered email / mobile number."""
    u = db().execute("SELECT email, phone FROM users WHERE id=?", (uid,)).fetchone()
    real_e = bool(config.EMAIL_USER and config.EMAIL_APP_PASSWORD)
    real_s = bool(config.TWILIO_SID and config.TWILIO_TOKEN and config.TWILIO_FROM)
    if real_e and u["email"]: threading.Thread(target=_send_email, args=(u["email"], subject, body), daemon=True).start()
    if real_s and u["phone"]: threading.Thread(target=_send_sms, args=(u["phone"], body), daemon=True).start()
    mode = lambda r: "sent" if r else "demo mode - not configured"
    sent = f"Email {u['email']} [{mode(real_e)}] | SMS {u['phone']} [{mode(real_s)}]"
    print(f"\n[NOTIFY] {subject}\n   {body}\n   {sent}\n")
    db().execute("INSERT INTO notifications(user_id,message,sent_to) VALUES(?,?,?)", (uid, subject + " - " + body, sent))
    db().commit()

def make_alerts(lost):
    """Create (and notify) alerts for matches of this lost item that were not alerted before."""
    new = []
    for s, f in find_matches(lost):
        if s < ALERT_MIN[lost["method"]]:
            continue
        cur = db().execute("INSERT OR IGNORE INTO alerts(user_id,lost_id,found_id,score) VALUES(?,?,?,?)", (lost["user_id"], lost["id"], f["id"], s))
        db().commit()
        if cur.rowcount:
            new.append(match_json(lost, s, f))
            notify(lost["user_id"], f"FindIt AI: your {lost['item_name']} is found!",
                   f"Your {lost['item_name']} ({s}% match) was reported FOUND at {f['location']} on {f['report_date']}. Open FindIt AI to verify and claim it.")
    return new

@app.route("/scan/<int:item_id>")
@login_required
def scan(item_id):
    it = db().execute("SELECT * FROM items WHERE id=? AND user_id=?", (item_id, session["uid"])).fetchone()
    if not it:
        return redirect(url_for("dashboard"))
    n = db().execute("SELECT COUNT(*) FROM items WHERE item_type='Found' AND status='Open'").fetchone()[0]
    u = db().execute("SELECT email, phone FROM users WHERE id=?", (session["uid"],)).fetchone()
    return render_template("scan.html", it=it, n=n, u=u)

@app.route("/api/check/<int:lost_id>")
@login_required
def api_check(lost_id):
    lost = db().execute("SELECT * FROM items WHERE id=? AND user_id=?", (lost_id, session["uid"])).fetchone()
    if not lost:
        return jsonify(error="not found"), 404
    make_alerts(lost)
    res = [match_json(lost, s, f) for s, f in find_matches(lost) if s >= ALERT_MIN[lost["method"]]]
    db().execute("UPDATE alerts SET seen=1 WHERE lost_id=?", (lost_id,)); db().commit()
    return jsonify(matches=res)

@app.route("/api/lookaround/<int:lost_id>", methods=["POST"])
@login_required
def api_lookaround(lost_id):
    """Camera mode: look for the lost item inside a live view (whole frame + 5 zoomed regions)."""
    lost = db().execute("SELECT * FROM items WHERE id=? AND user_id=?", (lost_id, session["uid"])).fetchone()
    f = request.files.get("photo")
    if not lost or not f:
        return jsonify(error="bad request"), 400
    img = Image.open(f).convert("RGB"); W, H = img.size
    boxes = [(0, 0, W, H)]
    for frac, steps in ((0.5, (0, 0.25, 0.5)), (0.67, (0, 0.33))):       # sliding windows over the view
        for fy in steps:
            for fx in steps:
                boxes.append((int(fx * W), int(fy * H), int((fx + frac) * W), int((fy + frac) * H)))
    v1 = json.loads(lost["embedding"])
    if lost["method"] == "fg":
        boxes += matcher.candidates(img, v1)
    best, bbox = 0, None
    for b in boxes:
        if b[2] - b[0] < 16 or b[3] - b[1] < 16:
            continue
        vec, m = matcher.embed_image(img.crop(b))
        if m == lost["method"]:
            sc = matcher.visual_score(v1, vec, m)
            if sc > best:
                best, bbox = sc, b
    return jsonify(score=round(best), spotted=best >= SPOT_MIN[lost["method"]], box=[bbox[0] / W, bbox[1] / H, bbox[2] / W, bbox[3] / H] if bbox else None)

@app.route("/notifications")
@login_required
def notifications():
    rows = db().execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC", (session["uid"],)).fetchall()
    return render_template("notifications.html", rows=rows)

@app.route("/scanner")
def scanner():
    watching = [] if "uid" not in session else db().execute("SELECT * FROM items WHERE user_id=? AND item_type='Lost' AND status='Open' AND category!='' ORDER BY id DESC",
                            (session["uid"],)).fetchall()
    return render_template("scanner.html", watching=watching)

@app.route("/api/search", methods=["POST"])
def api_search():
    """Live scan: compare the photo with all FOUND items. Nothing is saved."""
    f = request.files.get("photo")
    if not f:
        return jsonify(error="no photo"), 400
    fd, path = tempfile.mkstemp(suffix=".jpg"); os.close(fd)
    try:
        f.save(path)
        vec, method = matcher.embed(path)
        colour = matcher.dominant_colour(path)
    except Exception:
        return jsonify(error="bad image"), 400
    finally:
        if os.path.exists(path): os.remove(path)
    me = dict(category="", colour=colour, location="")
    out = []
    for r in db().execute("SELECT * FROM items WHERE item_type='Found' AND status='Open' AND method=?", (method,)):
        s = matcher.final_score(matcher.visual_score(vec, json.loads(r["embedding"]), method), me, r)
        if s >= ALERT_MIN[method]:
            out.append(dict(score=s, found_id=r["id"], name=r["item_name"], location=r["location"], date=r["report_date"],
                            image=url_for("static", filename=r["image_path"]), link=url_for("item", item_id=r["id"])))
    out.sort(key=lambda x: -x["score"])
    return jsonify(matches=out[:3])

@app.route("/api/scan", methods=["POST"])
def api_scan():
    if "uid" not in session:
        return jsonify(error="login"), 401
    f = request.files.get("photo")
    if not f:
        return jsonify(error="no photo"), 400
    name = uuid.uuid4().hex + ".jpg"
    path = os.path.join(UPLOADS, name)
    f.save(path)
    try:
        shrink(path)
        vec, method = matcher.embed(path)
        colour = matcher.dominant_colour(path)
    except Exception:
        os.remove(path)
        return jsonify(error="bad image"), 400
    r = request.form
    cur = db().execute(
        "INSERT INTO items(user_id,item_name,item_type,category,colour,description,location,image_path,report_date,embedding,method)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (session["uid"], r.get("item_name") or "Scanned item", "Lost", r.get("category") or "Other", colour,
         "Added from scanner", "Unknown", "uploads/" + name, date.today().isoformat(), json.dumps(vec), method))
    db().commit()
    lost = db().execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone()
    db().execute("UPDATE alerts SET seen=1 WHERE lost_id=?", (lost["id"],)); db().commit()
    return jsonify(id=lost["id"], colour=colour, alerts=make_alerts(lost))

@app.route("/api/alerts")
def api_alerts():
    if "uid" not in session:
        return jsonify(alerts=[])
    d = db()
    for l in d.execute("SELECT * FROM items WHERE user_id=? AND item_type='Lost' AND status='Open'", (session["uid"],)).fetchall():
        make_alerts(l)
    out = []
    for r in d.execute("SELECT * FROM alerts WHERE user_id=? AND seen=0", (session["uid"],)).fetchall():
        lost = d.execute("SELECT * FROM items WHERE id=?", (r["lost_id"],)).fetchone()
        f = d.execute("SELECT * FROM items WHERE id=?", (r["found_id"],)).fetchone()
        if lost and f and lost["status"] == "Open" and f["status"] == "Open":
            out.append(match_json(lost, r["score"], f))
        d.execute("UPDATE alerts SET seen=1 WHERE id=?", (r["id"],))
    d.commit()
    return jsonify(alerts=out)

@app.route("/api/stop/<int:item_id>")
@login_required
def api_stop(item_id):
    db().execute("UPDATE items SET status='Resolved' WHERE id=? AND user_id=?", (item_id, session["uid"]))
    db().commit()
    return redirect(url_for("scanner"))

@app.route("/")
def index():
    d = db()
    stats = dict(total=d.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                 found=d.execute("SELECT COUNT(*) FROM items WHERE item_type='Found'").fetchone()[0],
                 resolved=d.execute("SELECT COUNT(*) FROM items WHERE status='Resolved'").fetchone()[0])
    recent = d.execute("SELECT * FROM items ORDER BY id DESC LIMIT 4").fetchall()
    return render_template("index.html", stats=stats, recent=recent)

@app.route("/browse")
def browse():
    q = request.args
    sql, args = "SELECT * FROM items WHERE 1=1", []
    if q.get("q"):
        sql += " AND (item_name LIKE ? OR description LIKE ? OR location LIKE ?)"
        args += [f"%{q['q']}%"] * 3
    for f in ("item_type", "category", "colour", "location"):
        if q.get(f):
            sql += f" AND {f}=?"; args.append(q[f])
    items = db().execute(sql + " ORDER BY id DESC", args).fetchall()
    return render_template("browse.html", items=items, q=q)

@app.route("/report/<kind>", methods=["GET", "POST"])
@login_required
def report(kind):
    kind = kind.capitalize()
    if kind not in ("Lost", "Found"):
        return redirect(url_for("index"))
    if request.method == "POST":
        f = request.files.get("photo")
        ext = f.filename.rsplit(".", 1)[-1].lower() if f and "." in f.filename else ""
        if ext not in ALLOWED:
            flash("Please upload a PNG / JPG / WEBP photo.")
            return render_template("report.html", kind=kind, today=date.today().isoformat())
        name = uuid.uuid4().hex + "." + ext
        path = os.path.join(UPLOADS, name)
        f.save(path)
        try:
            shrink(path)
            vec, method = matcher.embed(path)
        except Exception:
            os.remove(path)
            flash("Could not read that image. Try another photo.")
            return render_template("report.html", kind=kind, today=date.today().isoformat())
        r = request.form
        cur = db().execute(
            "INSERT INTO items(user_id,item_name,item_type,category,colour,description,location,image_path,report_date,embedding,method)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (session["uid"], r["item_name"], kind, r["category"], r["colour"], r["description"], r["location"],
             "uploads/" + name, r["report_date"], json.dumps(vec), method))
        db().commit()
        if kind == "Lost":
            return redirect(url_for("scan", item_id=cur.lastrowid))      # open the AI scanner right away
        for l in db().execute("SELECT * FROM items WHERE item_type='Lost' AND status='Open'").fetchall():
            make_alerts(l)                                               # tell owners whose item was just found
        return redirect(url_for("item", item_id=cur.lastrowid))
    return render_template("report.html", kind=kind, today=date.today().isoformat())

@app.route("/item/<int:item_id>")
def item(item_id):
    it = db().execute("SELECT i.*, u.name AS owner FROM items i JOIN users u ON u.id=i.user_id WHERE i.id=?", (item_id,)).fetchone()
    if not it:
        return redirect(url_for("browse"))
    return render_template("item.html", it=it, matches=find_matches(it))

@app.route("/claim/<int:item_id>", methods=["POST"])
@login_required
def claim(item_id):
    db().execute("INSERT INTO claims(item_id,claimer_id,message) VALUES(?,?,?)",
                 (item_id, session["uid"], request.form.get("message", "")))
    db().commit()
    flash("Verification request sent to the owner. Your contact stays private until they accept.")
    return redirect(url_for("item", item_id=item_id))

@app.route("/claim/<int:cid>/<action>")
@login_required
def claim_action(cid, action):
    st = "Accepted" if action == "accept" else "Rejected"
    db().execute("UPDATE claims SET status=? WHERE id=? AND item_id IN (SELECT id FROM items WHERE user_id=?)",
                 (st, cid, session["uid"]))
    db().commit()
    return redirect(url_for("dashboard"))

@app.route("/resolve/<int:item_id>")
@login_required
def resolve(item_id):
    db().execute("UPDATE items SET status='Resolved' WHERE id=? AND (user_id=? OR ?=1)",
                 (item_id, session["uid"], 1 if session.get("admin") else 0))
    db().commit()
    return redirect(url_for("dashboard"))

@app.route("/dashboard")
@login_required
def dashboard():
    d, uid = db(), session["uid"]
    mine = d.execute("SELECT * FROM items WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()
    stats = dict(total=len(mine), lost=sum(i["item_type"] == "Lost" for i in mine),
                 found=sum(i["item_type"] == "Found" for i in mine),
                 resolved=sum(i["status"] == "Resolved" for i in mine))
    incoming = d.execute("""SELECT c.*, i.item_name, u.name AS who, u.email, u.phone FROM claims c
        JOIN items i ON i.id=c.item_id JOIN users u ON u.id=c.claimer_id WHERE i.user_id=? ORDER BY c.id DESC""", (uid,)).fetchall()
    sent = d.execute("""SELECT c.*, i.item_name, u.email, u.phone FROM claims c JOIN items i ON i.id=c.item_id
        JOIN users u ON u.id=i.user_id WHERE c.claimer_id=? ORDER BY c.id DESC""", (uid,)).fetchall()
    return render_template("dashboard.html", mine=mine, stats=stats, incoming=incoming, sent=sent)

@app.route("/admin")
@login_required
def admin():
    if not session.get("admin"):
        return redirect(url_for("index"))
    items = db().execute("SELECT i.*, u.name AS owner FROM items i JOIN users u ON u.id=i.user_id ORDER BY i.id DESC").fetchall()
    return render_template("admin.html", items=items)

@app.route("/admin/delete/<int:item_id>")
@login_required
def admin_delete(item_id):
    if session.get("admin"):
        db().execute("DELETE FROM items WHERE id=?", (item_id,)); db().commit()
    return redirect(url_for("admin"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE email=?", (request.form["email"].strip().lower(),)).fetchone()
        if u and check_password_hash(u["password"], request.form["password"]):
            session.update(uid=u["id"], name=u["name"], admin=bool(u["is_admin"]))
            return redirect(url_for("dashboard"))
        flash("Wrong email or password.")
    return render_template("login.html", mode="login")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        r = request.form
        try:
            db().execute("INSERT INTO users(name,email,phone,college,password) VALUES(?,?,?,?,?)",
                         (r["name"], r["email"].strip().lower(), r["phone"], r["college"], generate_password_hash(r["password"])))
            db().commit()
            flash("Account created. Please login.")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("That email is already registered.")
    return render_template("login.html", mode="register")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

if __name__ == "__main__":
    init_db()
    if DEMO:
        demo.seed(DB, UPLOADS, matcher, BASE)
    print("\n>>> Open http://127.0.0.1:5000 in your browser\n")
    app.run(debug=True, use_reloader=False)
