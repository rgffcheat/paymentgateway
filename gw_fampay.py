"""FamPay adapter (auto verification via the merchant's OWN Gmail, IMAP + App Password).  -- v15 rewrite
Flow: customer pays a UNIQUE amount (unique paise) -> FamPay/bank credit email lands in merchant Gmail
-> we read it and match amount + time. Safety: only mails with DKIM=pass and a matching sender count;
UTR / mail id is de-duplicated in core so one email can never confirm two orders.

WHAT CHANGED (fixes the website freeze + payments stuck on Pending):
 * Gmail is now read by ONE background thread per Gmail account (persistent IMAP connection, every ~6 s,
   only NEW mails are downloaded, each mail is downloaded/parsed only once).
 * status() NEVER touches the network any more: it only looks at the in-memory list of parsed mails.
   So 2-3-100 QR pages open at once cost nothing, web threads / DB connections are never blocked,
   login & dashboard stay fast.
 * Every amount found in the mail is compared (not just the first one), so extra numbers in the mail
   (balance, limit, etc.) can no longer hide the real payment amount.
 * Sender filter can be a comma list (default 'fam'); Test button now shows what was found.
"""
import imaplib, email, re, time, threading, hashlib
from email.utils import parsedate_to_datetime
from email.header import decode_header, make_header
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

LABEL = "FamPay / FamApp"; AVAILABLE = True
FIELDS = [("shop", "Shop Name", False), ("upi", "UPI ID", False), ("gmail", "Gmail Address", False), ("app_password", "Gmail App Password (16 digits)", True)]
SENDER_DEFAULT = "fam"          # comma separated substrings; a mail counts if its From header contains ANY of them
INTERVAL = 3                    # seconds between Gmail checks while payments are being waited for
ACTIVE_FOR = 180                # keep scanning this long after the last status() call, then go idle
_CREDIT = re.compile(r"(received|credited|paid you|sent you|payment from|credit)", re.I)
_DEBIT = re.compile(r"(debited|you paid|you sent|paid to|sent to)", re.I)
_AMT = re.compile(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_UTR = re.compile(r"\b(\d{12})\b")


def _text(msg):
    parts = []
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        if p.get_content_type() in ("text/plain", "text/html"):
            try: s = p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "ignore")
            except Exception: continue
            parts.append(re.sub(r"<[^>]+>", " ", s) if p.get_content_type() == "text/html" else s)
    return re.sub(r"\s+", " ", " ".join(parts))


def _senders(cfg):
    return [s.strip().lower() for s in str(cfg.get("sender") or SENDER_DEFAULT).replace('"', "").split(",") if s.strip()] or [SENDER_DEFAULT]


def _parse(raw, senders):
    msg = email.message_from_bytes(raw)
    frm = str(make_header(decode_header(msg.get("From", "")))).lower()
    subj = str(make_header(decode_header(msg.get("Subject", ""))))
    if not any(s in frm for s in senders): return None
    body = subj + " " + _text(msg)
    if not _CREDIT.search(body) or _DEBIT.search(subj): return None
    amts = set()
    for m in _AMT.finditer(body):
        try: amts.add(round(float(m.group(1).replace(",", "")) * 100))
        except ValueError: pass
    if not amts: return None
    try: ts = parsedate_to_datetime(msg.get("Date")); ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception: return None
    auth = " ".join(msg.get_all("Authentication-Results") or []).lower()
    u = _UTR.search(body)
    mid = (msg.get("Message-ID") or raw[:200].decode("latin1"))
    h = hashlib.sha1(mid.encode()).hexdigest()
    return {"amts": amts, "paise": min(amts) if len(amts) == 1 else sorted(amts)[0], "ts": ts.astimezone(timezone.utc), "dkim": "dkim=pass" in auth,
            "key": h[:24], "utr": u.group(1) if u else "MSG" + h[:12].upper(), "from": frm[:80]}


def _login(cfg):
    M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=25)
    M.login(cfg["gmail"], cfg["app_password"].replace(" ", "")); return M


class _Acct:
    def __init__(s, cfg):
        s.cfg = dict(cfg); s.lock = threading.Lock(); s.items = {}; s.seen = set(); s.M = None
        s.ok = False; s.err = ""; s.last = 0.0; s.active = time.time(); s.idle = False; s.wake = threading.Event(); s.scans = 0
        s.thread = None


_accts: dict = {}; _glock = threading.Lock()


def _close(a):
    try:
        if a.M: a.M.logout()
    except Exception: pass
    a.M = None


def _uid_fetch(M, uids, spec):
    out = []
    for i in range(0, len(uids), 40):
        t, d = M.uid("fetch", b",".join(uids[i:i + 40]), spec)
        if t != "OK": raise imaplib.IMAP4.error("fetch failed")
        for x in d:
            if isinstance(x, tuple):
                m = re.search(rb"UID (\d+)", x[0]); out.append((m.group(1) if m else b"", x[1]))
    return out


def _cycle(a):
    """One Gmail check: only mails not seen before are downloaded."""
    if a.M is None: a.M = _login(a.cfg)
    a.M.select("INBOX", readonly=True)                      # re-select = Gmail shows newly arrived mail
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
    typ, data = a.M.uid("search", None, "SINCE", since)
    if typ != "OK": raise imaplib.IMAP4.error("search failed")
    uids = [u for u in (data[0].split() if data and data[0] else []) if u not in a.seen][-150:]
    if uids:
        senders = _senders(a.cfg)
        heads = _uid_fetch(a.M, uids, "(BODY.PEEK[HEADER.FIELDS (FROM)])")   # cheap: headers only, then full body only for the right sender
        want = []
        for uid, h in heads:
            frm = str(make_header(decode_header(email.message_from_bytes(h).get("From", "")))).lower()
            if any(s in frm for s in senders): want.append(uid)
        for uid, raw in _uid_fetch(a.M, want, "(BODY.PEEK[])") if want else []:
            r = _parse(raw, senders)
            if r:
                r["seen"] = datetime.now(timezone.utc)
                print("fampay: new credit mail Rs", sorted(r["amts"])[0] / 100, "mail time", r["ts"].strftime("%H:%M:%S"), "UTC, read by us after", int((r["seen"] - r["ts"]).total_seconds()), "s", flush=True)
                with a.lock: a.items[r["key"]] = r
        a.seen.update(uids)
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    with a.lock:
        for k in [k for k, v in a.items.items() if v["ts"] < cutoff]: a.items.pop(k, None)
    if len(a.seen) > 5000: a.seen = set(list(a.seen)[-2000:])


def _loop(a):
    while True:
        if time.time() - a.active > ACTIVE_FOR:            # nobody is waiting for a payment: stop touching Gmail
            a.idle = True; _close(a); a.wake.wait(900); a.wake.clear(); a.idle = False; continue
        try: _cycle(a); a.ok = True; a.err = ""; delay = INTERVAL
        except Exception as ex:
            a.ok = False; a.err = f"{type(ex).__name__}: {str(ex)[:120]}"; _close(a); delay = 15
            print("fampay scan error:", a.err, flush=True)
        a.last = time.time(); a.scans += 1
        a.wake.wait(delay); a.wake.clear()


def _acct(cfg):
    key = cfg["gmail"].lower().strip()
    with _glock:
        a = _accts.get(key)
        if a is None: a = _accts[key] = _Acct(cfg)
        elif a.cfg.get("app_password") != cfg.get("app_password") or a.cfg.get("sender") != cfg.get("sender"): a.cfg = dict(cfg); a.seen = set(); a.items = {}; _close(a)
        a.active = time.time()
        if a.thread is None or not a.thread.is_alive():
            a.thread = threading.Thread(target=_loop, args=(a,), daemon=True, name="fampay-" + key[:12]); a.thread.start()
        elif a.idle: a.wake.set()
    return a


def create(cfg, env, pay_id, amount_paise):
    uri = f"upi://pay?pa={quote(cfg['upi'])}&pn={quote(cfg['shop'])}&am={amount_paise/100:.2f}&cu=INR&tn={quote('Order ' + pay_id[-8:])}"
    return {"qr_data": uri, "provider_ref": ""}


def status(cfg, env, pay_id, amount=None, since=None, used_keys=(), until=None, **kw):
    """Memory-only lookup: returns instantly, never waits for Gmail."""
    pend = {"state": "PENDING", "order_id": pay_id, "amount": 0, "txn_id": ""}
    if not amount or not since: return pend
    a = _acct(cfg)
    since = (since if since.tzinfo else since.replace(tzinfo=timezone.utc)) - timedelta(seconds=30)
    if until is not None: until = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
    with a.lock: recs = list(a.items.values())
    for r in sorted(recs, key=lambda x: x["ts"]):
        if r["key"] in used_keys: continue   # this email already confirmed another order - look at the next one
        if r["dkim"] and amount in r["amts"] and r["ts"] >= since and (until is None or r["ts"] <= until):
            return {"state": "SUCCESS", "order_id": pay_id, "amount": amount, "txn_id": r["utr"], "key": r["key"]}
    return pend


def test(cfg, env):
    try:
        t = _Acct(cfg); _cycle(t); _close(t)
    except imaplib.IMAP4.error: return False, "Gmail login failed - check Gmail + 16-digit App Password (2-step verification must be ON, IMAP enabled)"
    except Exception as e: return False, f"Cannot reach Gmail: {str(e)[:120]}"
    recs = sorted(t.items.values(), key=lambda x: x["ts"], reverse=True)
    if not recs: return True, f"Gmail connected (IMAP OK). No FamPay credit mail from sender '{', '.join(_senders(cfg))}' in the last 24h - after a real payment, if still nothing, set the correct sender text."
    last = recs[0]; ist = (last["ts"] + timedelta(hours=5, minutes=30)).strftime("%H:%M:%S")
    return True, f"Gmail connected (IMAP OK). {len(recs)} credit mail(s) in 24h; latest Rs {sorted(last['amts'])[0]/100:.2f} (mail time {ist} IST - compare with when you paid), DKIM {'OK' if last['dkim'] else 'NOT passed (payment will not auto-confirm)'}."


def refund(*a, **k): raise RuntimeError("Refunds are manual for FamPay")
def refund_status(*a, **k): return {"state": "PENDING", "msg": "manual"}
