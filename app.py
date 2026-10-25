"""Plateforme opérations hôtels + prestataires (multi-hôtels).
Lancer : ADMIN_EMAIL=... ADMIN_PASSWORD=... uvicorn app:app --port 8000
Rôles : hôtel (agent < responsable < patron), prestataire, admin plateforme."""
import base64, hashlib, io, json, os, re, secrets, sqlite3, time
from pathlib import Path
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
UP = BASE / "uploads"; UP.mkdir(exist_ok=True)
DB_PATH = Path(os.getenv("DB_PATH", BASE / "platform.db"))
MODEL = os.getenv("MODEL", "claude-sonnet-5-5")
COMMISSION = float(os.getenv("COMMISSION_RATE", "0.10"))   # part prélevée par la plateforme sur chaque prestation
SLA_H = {"urgent": 2, "haute": 8, "normale": 24, "basse": 72}
CATS = ["plomberie", "électricité", "climatisation", "literie/linge", "ménage", "mobilier", "serrurerie", "autre"]
TRADES = ["plomberie", "électricité", "climatisation", "serrurerie", "menuiserie", "peinture", "nettoyage", "piscine/jardin", "autre"]
CAT2TRADE = {"plomberie": "plomberie", "électricité": "électricité", "climatisation": "climatisation", "serrurerie": "serrurerie",
             "mobilier": "menuiserie", "ménage": "nettoyage", "literie/linge": "nettoyage"}
ROLES = {"agent": 1, "responsable": 2, "patron": 3}

app = FastAPI(title="Plateforme Opérations Hôtels")

def db():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

def hash_pw(pw):
    s = os.urandom(16); return s.hex() + ":" + hashlib.scrypt(pw.encode(), salt=s, n=2**14, r=8, p=1).hex()

def check_pw(pw, stored):
    s, h = stored.split(":"); return secrets.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(s), n=2**14, r=8, p=1).hex(), h)

def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS orgs(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, city TEXT, address TEXT, phone TEXT, adr REAL DEFAULT 800, created REAL);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, org_id INTEGER, name TEXT, email TEXT UNIQUE, pw TEXT, role TEXT,
          phone TEXT, city TEXT, trades TEXT, company TEXT, status TEXT DEFAULT 'valide', created REAL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER);
        CREATE TABLE IF NOT EXISTS places(id INTEGER PRIMARY KEY AUTOINCREMENT, org_id INTEGER, name TEXT, kind TEXT);
        CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, org_id INTEGER, place_id INTEGER, title TEXT, description TEXT,
          category TEXT, urgency TEXT, status TEXT DEFAULT 'nouveau', created REAL, due REAL, resolved REAL,
          reporter_id INTEGER, assignee_id INTEGER, photo TEXT, audio TEXT, blocked INTEGER DEFAULT 0, next_arrival TEXT, ai_note TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, ts REAL, user_id INTEGER, text TEXT);
        CREATE TABLE IF NOT EXISTS missions(id INTEGER PRIMARY KEY AUTOINCREMENT, org_id INTEGER, ticket_id INTEGER, trade TEXT, city TEXT, note TEXT,
          budget_mad REAL, status TEXT DEFAULT 'ouverte', provider_id INTEGER, created REAL, accepted REAL, done REAL, final_price REAL,
          commission REAL, done_note TEXT, done_photo TEXT, rating INTEGER, rating_comment TEXT, commission_paid INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS notifs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, ts REAL, text TEXT, link TEXT, read INTEGER DEFAULT 0);
        """)
        em, pw = os.getenv("ADMIN_EMAIL"), os.getenv("ADMIN_PASSWORD")
        if em and pw and not c.execute("SELECT 1 FROM users WHERE email=?", (em.lower(),)).fetchone():
            c.execute("INSERT INTO users(name,email,pw,role,created) VALUES('Admin plateforme',?,?, 'admin',?)", (em.lower(), hash_pw(pw), time.time()))
init()

# ---------- auth ----------
def user_of(token):
    with db() as c:
        r = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (token or "",)).fetchone()
    if not r: raise HTTPException(401, "Non connecté")
    return dict(r)

def hotel(token, role="agent"):
    u = user_of(token)
    if u["role"] not in ROLES or ROLES[u["role"]] < ROLES[role]: raise HTTPException(403, "Droits insuffisants")
    return u

def provider(token):
    u = user_of(token)
    if u["role"] != "prestataire": raise HTTPException(403, "Réservé aux prestataires")
    return u

def admin(token):
    u = user_of(token)
    if u["role"] != "admin": raise HTTPException(403, "Réservé à l'administrateur")
    return u

from contextvars import ContextVar
PEND = ContextVar("pend", default=None)

def notify(user_id, text, link=""):
    """Mise en file : écrite après la requête (évite les verrous SQLite imbriqués)."""
    row = (user_id, time.time(), text, link); lst = PEND.get()
    if lst is not None: lst.append(row)
    else:
        with db() as c: c.execute("INSERT INTO notifs(user_id,ts,text,link) VALUES(?,?,?,?)", row)

@app.middleware("http")
async def flush_notifs(request, call_next):
    lst = []; PEND.set(lst); resp = await call_next(request)
    if lst:
        with db() as c: c.executemany("INSERT INTO notifs(user_id,ts,text,link) VALUES(?,?,?,?)", lst)
    return resp

def notify_org(org_id, text, link="", min_role="responsable"):
    with db() as c:
        ids = [r["id"] for r in c.execute("SELECT id,role FROM users WHERE org_id=?", (org_id,)) if r["role"] in ROLES and ROLES[r["role"]] >= ROLES[min_role]]
    for i in ids: notify(i, text, link)

ATT = {}
def throttle(key, n=10, win=600):
    now = time.time(); ATT[key] = [t for t in ATT.get(key, []) if t > now - win]
    if len(ATT[key]) >= n: raise HTTPException(429, "Trop de tentatives, réessayez plus tard")
    ATT[key].append(now)

def public_user(u, org=None):
    d = {k: u[k] for k in ("id", "name", "email", "role", "org_id", "status", "city", "trades", "phone", "company")}
    if org: d["org"] = org
    return d

def org_of(c, org_id):
    r = c.execute("SELECT * FROM orgs WHERE id=?", (org_id,)).fetchone() if org_id else None
    return dict(r) if r else None

def start_session(c, user_id):
    t = secrets.token_urlsafe(24); c.execute("INSERT INTO sessions VALUES(?,?)", (t, user_id)); return t

class Login(BaseModel): email: str; password: str

@app.post("/api/login")
def login(b: Login):
    email = b.email.strip().lower(); throttle("login:" + email)
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u or not check_pw(b.password, u["pw"]): raise HTTPException(401, "Email ou mot de passe incorrect")
        return {"token": start_session(c, u["id"]), "user": public_user(dict(u), org_of(c, u["org_id"]))}

@app.get("/api/me")
def me(x_token: str = Header(default="")):
    u = user_of(x_token)
    with db() as c: return public_user(u, org_of(c, u["org_id"]))

class SignupHotel(BaseModel):
    hotel_name: str; city: str; name: str; email: str; password: str; phone: str = ""; address: str = ""

@app.post("/api/signup-hotel")
def signup_hotel(b: SignupHotel, request: Request):
    throttle("signup:" + (request.client.host if request.client else "?"), 10, 3600)
    email = b.email.strip().lower()
    if len(b.password) < 8 or "@" not in email or not b.hotel_name.strip() or not b.city.strip(): raise HTTPException(400, "Champs invalides (mot de passe : 8 caractères minimum)")
    try:
        with db() as c:
            oid = c.execute("INSERT INTO orgs(name,city,address,phone,created) VALUES(?,?,?,?,?)", (b.hotel_name.strip(), b.city.strip(), b.address, b.phone, time.time())).lastrowid
            uid = c.execute("INSERT INTO users(org_id,name,email,pw,role,created) VALUES(?,?,?,?, 'patron',?)", (oid, b.name.strip(), email, hash_pw(b.password), time.time())).lastrowid
            for i in (101, 102, 103): c.execute("INSERT INTO places(org_id,name,kind) VALUES(?,?,?)", (oid, f"Chambre {i}", "chambre"))
            for n, k in (("Réception", "commun"), ("Restaurant", "commun"), ("Piscine", "commun")): c.execute("INSERT INTO places(org_id,name,kind) VALUES(?,?,?)", (oid, n, k))
            tok = start_session(c, uid)
    except sqlite3.IntegrityError: raise HTTPException(409, "Cet email est déjà utilisé")
    return {"token": tok}

class SignupProv(BaseModel):
    name: str; email: str; password: str; phone: str; city: str; trades: list[str]; company: str = ""

@app.post("/api/signup-provider")
def signup_provider(b: SignupProv, request: Request):
    throttle("signup:" + (request.client.host if request.client else "?"), 10, 3600)
    email = b.email.strip().lower(); tr = [t for t in b.trades if t in TRADES]
    if len(b.password) < 8 or "@" not in email or not tr or not b.phone.strip() or not b.city.strip(): raise HTTPException(400, "Champs invalides (au moins un métier, mot de passe 8 caractères)")
    try:
        with db() as c:
            uid = c.execute("INSERT INTO users(name,email,pw,role,phone,city,trades,company,status,created) VALUES(?,?,?, 'prestataire',?,?,?,?, 'en_attente',?)",
                            (b.name.strip(), email, hash_pw(b.password), b.phone.strip(), b.city.strip(), ",".join(tr), b.company, time.time())).lastrowid
            tok = start_session(c, uid)
            for a in c.execute("SELECT id FROM users WHERE role='admin'"): notify(a["id"], f"Nouveau prestataire à valider : {b.name} ({', '.join(tr)}, {b.city})", "#/admin")
    except sqlite3.IntegrityError: raise HTTPException(409, "Cet email est déjà utilisé")
    return {"token": tok}

# ---------- notifications ----------
@app.get("/api/notifications")
def notifs(x_token: str = Header(default="")):
    u = user_of(x_token)
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM notifs WHERE user_id=? ORDER BY id DESC LIMIT 40", (u["id"],))]
    return {"unread": sum(1 for r in rows if not r["read"]), "items": rows}

@app.post("/api/notifications/read")
def notifs_read(x_token: str = Header(default="")):
    u = user_of(x_token)
    with db() as c: c.execute("UPDATE notifs SET read=1 WHERE user_id=?", (u["id"],))
    return {"ok": True}

# ---------- tri IA ----------
KW = {"plomberie": r"fuite|robinet|wc|toilette|douche|eau|chasse|bouch|évier|evier|canalis",
      "électricité": r"lumi|ampoule|prise|courant|électr|electr|disjonct|télé|tele\b|tv",
      "climatisation": r"clim|chauff|froid|chaud|température|ventil",
      "literie/linge": r"drap|serviette|linge|couverture|oreiller|matelas|lit\b",
      "ménage": r"propre|sale|ménage|menage|poussière|odeur|tache|poubelle",
      "mobilier": r"armoire|chaise|table|porte|tiroir|fenêtre|fenetre|rideau|cassé|casse",
      "serrurerie": r"serrure|clé|clef|badge|verrou|bloqu.*porte|coffre"}
URG = r"fuite|inond|incendie|fumée|fumee|gaz|court-circuit|étincelle|danger|enferm|plus d'eau|plus de courant"

def triage(title, desc, photo=None):
    text = f"{title} {desc}".lower()
    cat = next((k for k, rx in KW.items() if re.search(rx, text)), "autre")
    urg = "urgent" if re.search(URG, text) else "normale"; note = "tri par règles"
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            content = []
            if photo: content.append({"type": "image", "source": {"type": "base64", "media_type": photo[1], "data": base64.b64encode(photo[0]).decode()}})
            content.append({"type": "text", "text": f"Signalement dans un hôtel : « {title} » — {desc}\nRéponds par un JSON : "
                f'{{"category": un de {CATS}, "urgency": "urgent|haute|normale|basse", "summary": phrase courte}}. urgent = sécurité, fuite, plus d\'eau/courant, chambre inutilisable à court terme.'})
            r = anthropic.Anthropic().messages.create(model=MODEL, max_tokens=250, messages=[{"role": "user", "content": content}])
            j = json.loads(re.search(r"\{.*\}", "".join(b.text for b in r.content if b.type == "text"), re.S).group(0))
            if j.get("category") in CATS: cat = j["category"]
            if j.get("urgency") in SLA_H: urg = j["urgency"]
            note = "IA : " + str(j.get("summary", ""))[:200]
        except Exception: note = "tri par règles (IA indisponible)"
    return cat, urg, note

def days_until(s):
    try:
        from datetime import date
        return (date.fromisoformat(s) - date.today()).days
    except Exception: return None

async def save_upload(f, kinds, maxmb=8):
    if not f or not f.filename: return None, None
    data = await f.read()
    if len(data) > maxmb * 1024 * 1024: raise HTTPException(413, "Fichier trop volumineux")
    mt = (f.content_type or "").split(";")[0]
    if not any(mt.startswith(k) for k in kinds): raise HTTPException(400, "Type de fichier non accepté")
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic", "audio/webm": ".webm", "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav"}.get(mt, ".bin")
    name = secrets.token_urlsafe(12) + ext; (UP / name).write_bytes(data); return name, (data, mt)

# ---------- lieux / réglages ----------
@app.get("/api/places")
def places(x_token: str = Header(default="")):
    u = hotel(x_token)
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM places WHERE org_id=? ORDER BY kind='chambre' DESC, id", (u["org_id"],))]

class PlaceIn(BaseModel): name: str; kind: str = "chambre"

@app.post("/api/places")
def add_place(b: PlaceIn, x_token: str = Header(default="")):
    u = hotel(x_token, "responsable")
    with db() as c: return {"id": c.execute("INSERT INTO places(org_id,name,kind) VALUES(?,?,?)", (u["org_id"], b.name.strip(), b.kind)).lastrowid}

@app.get("/api/places/{pid}/qr.svg")
def qr(pid: int, request: Request):
    import qrcode, qrcode.image.svg
    url = f"{os.getenv('PUBLIC_URL', str(request.base_url).rstrip('/'))}/q/{pid}"
    buf = io.BytesIO(); qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10).save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")

@app.get("/q/{pid}")
def qr_landing(pid: int): return RedirectResponse(f"/#/new?place={pid}")

class OrgIn(BaseModel): name: str | None = None; city: str | None = None; address: str | None = None; phone: str | None = None; adr: float | None = None

@app.patch("/api/org")
def patch_org(b: OrgIn, x_token: str = Header(default="")):
    u = hotel(x_token, "patron")
    with db() as c:
        for k, v in b.model_dump(exclude_none=True).items(): c.execute(f"UPDATE orgs SET {k}=? WHERE id=?", (v, u["org_id"]))
    return {"ok": True}

# ---------- tickets ----------
def ticket_row(r):
    d = dict(r); d["overdue"] = d["status"] != "resolu" and d["due"] is not None and d["due"] < time.time(); return d

BASE_Q = """SELECT t.*, p.name AS place, ru.name AS reporter, au.name AS assignee
            FROM tickets t JOIN places p ON p.id=t.place_id LEFT JOIN users ru ON ru.id=t.reporter_id LEFT JOIN users au ON au.id=t.assignee_id"""

@app.post("/api/tickets")
async def create_ticket(place_id: int = Form(...), title: str = Form(...), description: str = Form(""), urgency: str = Form("auto"),
                        blocked: int = Form(0), next_arrival: str = Form(""), photo: UploadFile = File(None), audio: UploadFile = File(None), x_token: str = Header(default="")):
    u = hotel(x_token)
    with db() as c:
        if not c.execute("SELECT 1 FROM places WHERE id=? AND org_id=?", (place_id, u["org_id"])).fetchone(): raise HTTPException(400, "Lieu inconnu")
    pname, pdata = await save_upload(photo, ["image/"]); aname, _ = await save_upload(audio, ["audio/"], 15)
    cat, ai_urg, note = triage(title, description, pdata)
    urg = ai_urg if urgency == "auto" else (urgency if urgency in SLA_H else "normale")
    if blocked:
        d = days_until(next_arrival)
        if d is not None and d <= 2: urg, note = "urgent", note + f" · chambre bloquée, arrivée dans {max(d,0)} j"
        elif urg in ("normale", "basse"): urg = "haute"
    now = time.time()
    with db() as c:
        tid = c.execute("""INSERT INTO tickets(org_id,place_id,title,description,category,urgency,created,due,reporter_id,photo,audio,blocked,next_arrival,ai_note)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (u["org_id"], place_id, title.strip(), description.strip(), cat, urg, now, now + SLA_H[urg] * 3600, u["id"], pname, aname, int(bool(blocked)), next_arrival or None, note)).lastrowid
        c.execute("INSERT INTO events(ticket_id,ts,user_id,text) VALUES(?,?,?,?)", (tid, now, u["id"], f"Créé ({urg}, {cat})"))
    if urg == "urgent": notify_org(u["org_id"], f"🚨 Ticket urgent : {title.strip()}", f"#/t/{tid}")
    return {"id": tid, "urgency": urg, "category": cat, "note": note}

@app.get("/api/tickets")
def list_tickets(status: str = "", x_token: str = Header(default="")):
    u = hotel(x_token)
    q = BASE_Q + " WHERE t.org_id=?" + (" AND t.status=?" if status else "") + " ORDER BY CASE t.urgency WHEN 'urgent' THEN 0 WHEN 'haute' THEN 1 WHEN 'normale' THEN 2 ELSE 3 END, t.due LIMIT 300"
    with db() as c: return [ticket_row(r) for r in c.execute(q, (u["org_id"], status) if status else (u["org_id"],))]

def mission_view(c, m):
    d = dict(m)
    if d["provider_id"]:
        p = c.execute("SELECT name,phone,company FROM users WHERE id=?", (d["provider_id"],)).fetchone()
        if p:
            d["provider"] = p["name"] + (f" ({p['company']})" if p["company"] else ""); d["provider_phone"] = p["phone"]
            r = c.execute("SELECT AVG(rating) a, COUNT(rating) n FROM missions WHERE provider_id=? AND rating IS NOT NULL", (d["provider_id"],)).fetchone()
            d["provider_rating"] = round(r["a"], 1) if r["a"] else None; d["provider_rating_n"] = r["n"]
    d.pop("commission", None); d.pop("commission_paid", None); return d

@app.get("/api/tickets/{tid}")
def get_ticket(tid: int, x_token: str = Header(default="")):
    u = hotel(x_token)
    with db() as c:
        r = c.execute(BASE_Q + " WHERE t.id=? AND t.org_id=?", (tid, u["org_id"])).fetchone()
        if not r: raise HTTPException(404)
        ev = [dict(e) | {"user": (c.execute("SELECT name FROM users WHERE id=?", (e["user_id"],)).fetchone() or {"name": ""})["name"]} for e in c.execute("SELECT * FROM events WHERE ticket_id=? ORDER BY ts", (tid,))]
        m = c.execute("SELECT * FROM missions WHERE ticket_id=? AND status!='annulee' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        mv = mission_view(c, m) if m else None
    return ticket_row(r) | {"events": ev, "mission": mv, "default_trade": CAT2TRADE.get(r["category"], "autre")}

class Patch(BaseModel):
    status: str | None = None; assignee_id: int | None = None; urgency: str | None = None; comment: str | None = None

@app.patch("/api/tickets/{tid}")
def patch_ticket(tid: int, b: Patch, x_token: str = Header(default="")):
    u = hotel(x_token); now = time.time()
    with db() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=? AND org_id=?", (tid, u["org_id"])).fetchone()
        if not t: raise HTTPException(404)
        log = []
        if b.status and b.status != t["status"]:
            if b.status not in ("nouveau", "en_cours", "resolu"): raise HTTPException(400)
            if b.status != "en_cours" and ROLES[u["role"]] < 2 and not (b.status == "resolu" and t["assignee_id"] == u["id"]): raise HTTPException(403, "Droits insuffisants")
            c.execute("UPDATE tickets SET status=?, resolved=? WHERE id=?", (b.status, now if b.status == "resolu" else None, tid)); log.append(f"Statut → {b.status}")
        if b.assignee_id is not None:
            if ROLES[u["role"]] < 2: raise HTTPException(403, "Droits insuffisants")
            if b.assignee_id and not c.execute("SELECT 1 FROM users WHERE id=? AND org_id=?", (b.assignee_id, u["org_id"])).fetchone(): raise HTTPException(400)
            c.execute("UPDATE tickets SET assignee_id=? WHERE id=?", (b.assignee_id or None, tid))
            n = c.execute("SELECT name FROM users WHERE id=?", (b.assignee_id,)).fetchone(); log.append(f"Assigné à {n['name'] if n else '—'}")
            if b.assignee_id: notify(b.assignee_id, f"Ticket qui vous est assigné : {t['title']}", f"#/t/{tid}")
            if t["status"] == "nouveau" and not b.status: c.execute("UPDATE tickets SET status='en_cours' WHERE id=?", (tid,))
        if b.urgency and b.urgency != t["urgency"]:
            if ROLES[u["role"]] < 2: raise HTTPException(403, "Droits insuffisants")
            if b.urgency not in SLA_H: raise HTTPException(400)
            c.execute("UPDATE tickets SET urgency=?, due=? WHERE id=?", (b.urgency, t["created"] + SLA_H[b.urgency] * 3600, tid)); log.append(f"Urgence → {b.urgency}")
        if b.comment: log.append("💬 " + b.comment.strip())
        for l in log: c.execute("INSERT INTO events(ticket_id,ts,user_id,text) VALUES(?,?,?,?)", (tid, now, u["id"], l))
    return {"ok": True}

# ---------- équipe ----------
@app.get("/api/staff")
def staff(x_token: str = Header(default="")):
    u = hotel(x_token)
    with db() as c: return [dict(r) for r in c.execute("SELECT id,name,email,role FROM users WHERE org_id=?", (u["org_id"],))]

class NewUser(BaseModel): name: str; email: str; role: str; password: str

@app.post("/api/staff")
def add_staff(b: NewUser, x_token: str = Header(default="")):
    u = hotel(x_token, "patron")
    if b.role not in ROLES or len(b.password) < 8 or "@" not in b.email: raise HTTPException(400, "Rôle, email ou mot de passe (8 caractères min) invalide")
    try:
        with db() as c: c.execute("INSERT INTO users(org_id,name,email,pw,role,created) VALUES(?,?,?,?,?,?)", (u["org_id"], b.name.strip(), b.email.strip().lower(), hash_pw(b.password), b.role, time.time()))
    except sqlite3.IntegrityError: raise HTTPException(409, "Cet email existe déjà")
    return {"ok": True}

# ---------- missions (côté hôtel) ----------
class MissionIn(BaseModel): trade: str; budget_mad: float | None = None; note: str = ""

@app.post("/api/tickets/{tid}/mission")
def create_mission(tid: int, b: MissionIn, x_token: str = Header(default="")):
    u = hotel(x_token, "responsable")
    if b.trade not in TRADES: raise HTTPException(400, "Métier inconnu")
    with db() as c:
        t = c.execute("SELECT * FROM tickets WHERE id=? AND org_id=?", (tid, u["org_id"])).fetchone()
        if not t: raise HTTPException(404)
        if c.execute("SELECT 1 FROM missions WHERE ticket_id=? AND status IN ('ouverte','acceptee','terminee')", (tid,)).fetchone(): raise HTTPException(409, "Une mission est déjà en cours pour ce ticket")
        o = org_of(c, u["org_id"])
        mid = c.execute("INSERT INTO missions(org_id,ticket_id,trade,city,note,budget_mad,created) VALUES(?,?,?,?,?,?,?)", (u["org_id"], tid, b.trade, o["city"], b.note, b.budget_mad, time.time())).lastrowid
        c.execute("INSERT INTO events(ticket_id,ts,user_id,text) VALUES(?,?,?,?)", (tid, time.time(), u["id"], f"Mission publiée aux prestataires ({b.trade})"))
    return {"id": mid, "notified": notify_providers(mid)}

def notify_providers(mid):
    with db() as c:
        m = c.execute("SELECT m.*, t.urgency, t.title FROM missions m JOIN tickets t ON t.id=m.ticket_id WHERE m.id=?", (mid,)).fetchone()
        provs = [r for r in c.execute("SELECT * FROM users WHERE role='prestataire' AND status='valide'")
                 if m["trade"] in (r["trades"] or "").split(",") and (r["city"] or "").strip().lower() == (m["city"] or "").strip().lower()]
    for p in provs: notify(p["id"], f"{'🚨 ' if m['urgency']=='urgent' else ''}Nouvelle mission {m['trade']} à {m['city']} : {m['title']}", "#/pmissions")
    return len(provs)

@app.post("/api/missions/{mid}/release")
def release(mid: int, x_token: str = Header(default="")):
    u = hotel(x_token, "responsable")
    with db() as c:
        m = c.execute("SELECT * FROM missions WHERE id=? AND org_id=?", (mid, u["org_id"])).fetchone()
        if not m or m["status"] not in ("acceptee", "ouverte"): raise HTTPException(400, "Impossible")
        if m["provider_id"]: notify(m["provider_id"], "Le prestataire a été remplacé sur une mission", "#/pmissions")
        c.execute("UPDATE missions SET status='ouverte', provider_id=NULL, accepted=NULL WHERE id=?", (mid,))
        c.execute("INSERT INTO events(ticket_id,ts,user_id,text) VALUES(?,?,?,?)", (m["ticket_id"], time.time(), u["id"], "Mission remise en ligne"))
    return {"notified": notify_providers(mid)}

@app.post("/api/missions/{mid}/cancel")
def cancel(mid: int, x_token: str = Header(default="")):
    u = hotel(x_token, "responsable")
    with db() as c:
        m = c.execute("SELECT * FROM missions WHERE id=? AND org_id=?", (mid, u["org_id"])).fetchone()
        if not m or m["status"] not in ("acceptee", "ouverte"): raise HTTPException(400, "Impossible")
        if m["provider_id"]: notify(m["provider_id"], "Une mission a été annulée par l'hôtel", "#/pmissions")
        c.execute("UPDATE missions SET status='annulee' WHERE id=?", (mid,))
        c.execute("INSERT INTO events(ticket_id,ts,user_id,text) VALUES(?,?,?,?)", (m["ticket_id"], time.time(), u["id"], "Mission annulée"))
    return {"ok": True}

class Confirm(BaseModel): price_ok: bool; rating: int | None = None; comment: str = ""

@app.post("/api/missions/{mid}/confirm")
def confirm(mid: int, b: Confirm, x_token: str = Header(default="")):
    u = hotel(x_token, "responsable")
    with db() as c:
        m = c.execute("SELECT * FROM missions WHERE id=? AND org_id=?", (mid, u["org_id"])).fetchone()
        if not m or m["status"] != "terminee": raise HTTPException(400, "Mission non terminée")
        r = b.rating if b.rating and 1 <= b.rating <= 5 else None
        if b.price_ok:
            c.execute("UPDATE missions SET status='confirmee', rating=?, rating_comment=? WHERE id=?", (r, b.comment, mid))
            c.execute("UPDATE tickets SET status='resolu', resolved=? WHERE id=?", (time.time(), m["ticket_id"]))
            c.execute("INSERT INTO events(ticket_id,ts,user_id,text) VALUES(?,?,?,?)", (m["ticket_id"], time.time(), u["id"], f"Intervention confirmée ({m['final_price']:.0f} MAD) — ticket résolu"))
            notify(m["provider_id"], "Mission confirmée par l'hôtel. Merci !", "#/pmissions")
        else:
            c.execute("UPDATE missions SET status='litige', rating_comment=? WHERE id=?", (b.comment, mid))
            c.execute("INSERT INTO events(ticket_id,ts,user_id,text) VALUES(?,?,?,?)", (m["ticket_id"], time.time(), u["id"], "Montant contesté — litige"))
            for a in c.execute("SELECT id FROM users WHERE role='admin'"): notify(a["id"], f"Litige sur la mission #{mid}", "#/admin")
    return {"ok": True}

# ---------- prestataire ----------
def provider_mission_row(c, m, reveal):
    t = c.execute("SELECT t.title,t.description,t.photo,t.urgency,t.audio, p.name place FROM tickets t JOIN places p ON p.id=t.place_id WHERE t.id=?", (m["ticket_id"],)).fetchone()
    d = {k: m[k] for k in ("id", "trade", "city", "note", "budget_mad", "status", "created", "accepted", "done", "final_price", "commission")} | {"title": t["title"], "description": t["description"], "photo": t["photo"], "audio": t["audio"], "urgency": t["urgency"]}
    if reveal:
        o = org_of(c, m["org_id"]); d |= {"hotel": o["name"], "address": o["address"], "hotel_phone": o["phone"], "place": t["place"]}
    return d

@app.get("/api/provider/missions")
def provider_missions(scope: str = "open", x_token: str = Header(default="")):
    u = provider(x_token)
    with db() as c:
        if scope == "open":
            if u["status"] != "valide": return []
            tr = (u["trades"] or "").split(",")
            rows = [m for m in c.execute("SELECT * FROM missions WHERE status='ouverte' ORDER BY id DESC") if m["trade"] in tr and (m["city"] or "").strip().lower() == (u["city"] or "").strip().lower()]
            return [provider_mission_row(c, m, False) for m in rows]
        rows = c.execute("SELECT * FROM missions WHERE provider_id=? ORDER BY id DESC", (u["id"],)).fetchall()
        return [provider_mission_row(c, m, True) for m in rows]

@app.post("/api/missions/{mid}/accept")
def accept(mid: int, x_token: str = Header(default="")):
    u = provider(x_token)
    if u["status"] != "valide": raise HTTPException(403, "Compte en attente de validation")
    with db() as c:
        m = c.execute("SELECT * FROM missions WHERE id=?", (mid,)).fetchone()
        if not m or m["trade"] not in (u["trades"] or "").split(","): raise HTTPException(404)
        cur = c.execute("UPDATE missions SET status='acceptee', provider_id=?, accepted=? WHERE id=? AND status='ouverte'", (u["id"], time.time(), mid))
        if cur.rowcount == 0: raise HTTPException(409, "Mission déjà prise par un autre prestataire")
        c.execute("UPDATE tickets SET status='en_cours' WHERE id=? AND status='nouveau'", (m["ticket_id"],))
        c.execute("INSERT INTO events(ticket_id,ts,user_id,text) VALUES(?,?,?,?)", (m["ticket_id"], time.time(), u["id"], f"Mission acceptée par {u['name']}"))
    notify_org(m["org_id"], f"✅ {u['name']} a accepté la mission {m['trade']}", f"#/t/{m['ticket_id']}")
    return {"ok": True}

@app.post("/api/missions/{mid}/withdraw")
def withdraw(mid: int, x_token: str = Header(default="")):
    u = provider(x_token)
    with db() as c:
        m = c.execute("SELECT * FROM missions WHERE id=? AND provider_id=? AND status='acceptee'", (mid, u["id"])).fetchone()
        if not m: raise HTTPException(400, "Impossible")
        c.execute("UPDATE missions SET status='ouverte', provider_id=NULL, accepted=NULL WHERE id=?", (mid,))
        c.execute("INSERT INTO events(ticket_id,ts,user_id,text) VALUES(?,?,?,?)", (m["ticket_id"], time.time(), u["id"], f"{u['name']} s'est désisté — mission remise en ligne"))
    notify_org(m["org_id"], f"⚠️ {u['name']} s'est désisté, mission remise en ligne", f"#/t/{m['ticket_id']}")
    notify_providers(mid); return {"ok": True}

@app.post("/api/missions/{mid}/done")
async def done(mid: int, final_price: float = Form(...), note: str = Form(""), photo: UploadFile = File(None), x_token: str = Header(default="")):
    u = provider(x_token)
    if final_price < 0 or final_price > 1_000_000: raise HTTPException(400, "Montant invalide")
    name, _ = await save_upload(photo, ["image/"])
    with db() as c:
        m = c.execute("SELECT * FROM missions WHERE id=? AND provider_id=? AND status='acceptee'", (mid, u["id"])).fetchone()
        if not m: raise HTTPException(400, "Impossible")
        c.execute("UPDATE missions SET status='terminee', done=?, final_price=?, commission=?, done_note=?, done_photo=? WHERE id=?", (time.time(), final_price, round(final_price * COMMISSION, 2), note, name, mid))
        c.execute("INSERT INTO events(ticket_id,ts,user_id,text) VALUES(?,?,?,?)", (m["ticket_id"], time.time(), u["id"], f"Intervention terminée par {u['name']} — montant déclaré {final_price:.0f} MAD (payé sur place)"))
    notify_org(m["org_id"], f"🔧 Intervention terminée ({final_price:.0f} MAD) — à confirmer", f"#/t/{m['ticket_id']}")
    return {"ok": True}

@app.get("/api/provider/stats")
def provider_stats(x_token: str = Header(default="")):
    u = provider(x_token)
    with db() as c:
        r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(final_price),0) s, COALESCE(SUM(commission),0) com, AVG(rating) a FROM missions WHERE provider_id=? AND status='confirmee'", (u["id"],)).fetchone()
        due = c.execute("SELECT COALESCE(SUM(commission),0) s FROM missions WHERE provider_id=? AND status='confirmee' AND commission_paid=0", (u["id"],)).fetchone()["s"]
    return {"done": r["n"], "revenue": r["s"], "commission_total": r["com"], "commission_due": due, "rating": round(r["a"], 1) if r["a"] else None, "rate": COMMISSION}

# ---------- admin ----------
@app.get("/api/admin/overview")
def admin_overview(x_token: str = Header(default="")):
    admin(x_token)
    with db() as c:
        provs = [dict(r) for r in c.execute("SELECT id,name,email,phone,city,trades,company,status,created FROM users WHERE role='prestataire' ORDER BY status='en_attente' DESC, id DESC")]
        orgs = [dict(r) | {"tickets": c.execute("SELECT COUNT(*) FROM tickets WHERE org_id=?", (r["id"],)).fetchone()[0]} for r in c.execute("SELECT * FROM orgs ORDER BY id DESC")]
        ledger = [dict(r) for r in c.execute("""SELECT m.id, m.final_price, m.commission, m.commission_paid, m.status, m.done, u.name provider, o.name hotel
                  FROM missions m JOIN users u ON u.id=m.provider_id JOIN orgs o ON o.id=m.org_id WHERE m.status IN ('confirmee','litige') ORDER BY m.id DESC LIMIT 200""")]
    return {"providers": provs, "orgs": orgs, "ledger": ledger, "rate": COMMISSION,
            "commission_due": sum(r["commission"] or 0 for r in ledger if r["status"] == "confirmee" and not r["commission_paid"])}

class ProvStatus(BaseModel): status: str

@app.post("/api/admin/providers/{pid}")
def admin_provider(pid: int, b: ProvStatus, x_token: str = Header(default="")):
    admin(x_token)
    if b.status not in ("valide", "suspendu", "en_attente"): raise HTTPException(400)
    with db() as c: c.execute("UPDATE users SET status=? WHERE id=? AND role='prestataire'", (b.status, pid))
    if b.status == "valide": notify(pid, "Votre compte est validé : vous recevrez les missions de votre zone.", "#/pmissions")
    return {"ok": True}

@app.post("/api/admin/missions/{mid}/paid")
def admin_paid(mid: int, x_token: str = Header(default="")):
    admin(x_token)
    with db() as c: c.execute("UPDATE missions SET commission_paid=1 WHERE id=?", (mid,))
    return {"ok": True}

@app.post("/api/admin/missions/{mid}/resolve")
def admin_resolve(mid: int, b: Confirm, x_token: str = Header(default="")):
    """Arbitrage d'un litige : price_ok=true confirme le montant déclaré par le prestataire."""
    admin(x_token)
    with db() as c:
        m = c.execute("SELECT * FROM missions WHERE id=? AND status='litige'", (mid,)).fetchone()
        if not m: raise HTTPException(400)
        c.execute("UPDATE missions SET status=? WHERE id=?", ("confirmee" if b.price_ok else "annulee", mid))
        if b.price_ok: c.execute("UPDATE tickets SET status='resolu', resolved=? WHERE id=?", (time.time(), m["ticket_id"]))
    return {"ok": True}

# ---------- tableau de bord hôtel ----------
@app.get("/api/dashboard")
def dashboard(x_token: str = Header(default="")):
    u = hotel(x_token, "responsable"); now = time.time()
    with db() as c:
        T = [dict(r) for r in c.execute(BASE_Q + " WHERE t.org_id=?", (u["org_id"],))]
        adr = org_of(c, u["org_id"])["adr"]
        ms = c.execute("SELECT COUNT(*) n, COALESCE(SUM(final_price),0) s FROM missions WHERE org_id=? AND status='confirmee'", (u["org_id"],)).fetchone()
    open_ = [t for t in T if t["status"] != "resolu"]; overdue = [t for t in open_ if t["due"] < now]
    done30 = [t for t in T if t["status"] == "resolu" and t["resolved"] and t["resolved"] > now - 30 * 86400]
    avg_h = round(sum((t["resolved"] - t["created"]) / 3600 for t in done30) / len(done30), 1) if done30 else None
    sla_ok = round(100 * sum(t["resolved"] <= t["due"] for t in done30) / len(done30)) if done30 else None
    blocked = [{"id": t["id"], "place": t["place"], "title": t["title"], "hours": round((now - t["created"]) / 3600, 1), "loss_mad": round((now - t["created"]) / 86400 * adr), "next_arrival": t["next_arrival"]} for t in open_ if t["blocked"]]
    lost_res = round(sum(((t["resolved"] - t["created"]) / 86400) * adr for t in done30 if t["blocked"]))
    byplace, bycat, load = {}, {}, {}
    for t in T:
        if t["created"] > now - 90 * 86400:
            byplace[t["place"]] = byplace.get(t["place"], 0) + 1; bycat[t["category"]] = bycat.get(t["category"], 0) + 1
    for t in open_: k = t["assignee"] or "Non assigné"; load[k] = load.get(k, 0) + 1
    top = lambda d, n=5: sorted(d.items(), key=lambda x: -x[1])[:n]
    return {"open": len(open_), "overdue": len(overdue), "avg_resolution_h": avg_h, "sla_ok_pct": sla_ok, "blocked": blocked, "blocked_loss_now": sum(b["loss_mad"] for b in blocked),
            "blocked_loss_30d_resolved": lost_res, "top_places": top(byplace), "by_category": top(bycat, 8), "load": top(load, 8), "adr": adr,
            "provider_missions": ms["n"], "provider_spend": ms["s"]}

@app.post("/api/demo-data")
def demo(x_token: str = Header(default="")):
    import random; u = hotel(x_token, "patron"); now = time.time()
    samples = [("Fuite sous le lavabo", "plomberie", "urgent", 1), ("Climatisation ne refroidit plus", "climatisation", "haute", 1), ("Ampoule grillée salle de bain", "électricité", "basse", 0),
               ("Serrure badge ne répond plus", "serrurerie", "haute", 1), ("Serviettes manquantes", "literie/linge", "normale", 0), ("Chasse d'eau coule", "plomberie", "normale", 0)]
    with db() as c:
        pids = [r["id"] for r in c.execute("SELECT id FROM places WHERE org_id=? AND kind='chambre'", (u["org_id"],))]
        for i, (t, cat, urg, blk) in enumerate(samples * 2):
            created = now - random.randint(1, 60) * 3600 * (1 + i % 3); st = random.choice(["nouveau", "en_cours", "resolu", "resolu"])
            res = created + random.randint(1, 30) * 3600 if st == "resolu" else None
            c.execute("INSERT INTO tickets(org_id,place_id,title,description,category,urgency,status,created,due,resolved,reporter_id,blocked,ai_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (u["org_id"], random.choice(pids), t, "", cat, urg, st, created, created + SLA_H[urg] * 3600, res, u["id"], blk, "démo"))
    return {"ok": True}

@app.get("/api/config")
def config(): return {"trades": TRADES, "commission": COMMISSION}

app.mount("/uploads", StaticFiles(directory=UP), name="uploads")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
@app.get("/")
def index(): return FileResponse(BASE / "static" / "index.html")
