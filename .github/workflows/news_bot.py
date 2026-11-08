#!/usr/bin/env python3
"""ربات اخبار ورزشی: فید RSS منابع را می‌خواند و خبر تازه را با عکس/ویدیو در کانال تلگرام می‌گذارد.
فقط از کتابخانهٔ استاندارد پایتون استفاده می‌کند و برای اجرا روی GitHub Actions طراحی شده است."""
import os, re, sys, json, time, html, hashlib, uuid, email.utils
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL = os.environ.get("CHANNEL", "").strip()                      # مثل @footballistanews
SIGNATURE = os.environ.get("SIGNATURE", "⚽️  @footballistanews 🥇")  # خط آخر هر پست
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))               # حداکثر خبر در هر اجرا
MAX_AGE = int(os.environ.get("MAX_AGE_HOURS", "4")) * 3600          # فقط خبرهای تازه‌تر از این
TEST = os.environ.get("TEST", "0") == "1"                           # فقط بررسی فیدها، بدون ارسال
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
FEEDS_FILE = os.environ.get("FEEDS_FILE", "feeds.txt")
UA = {"User-Agent": "Mozilla/5.0 (compatible; SportsNewsBot/1.0)"}
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
VIDEO_LIMIT = 48 * 1024 * 1024  # سقف آپلود ربات‌ها ~۵۰ مگابایت


class TgError(Exception):
    pass


class TranslateError(Exception):
    pass


# ───────────── شبکه ─────────────
def http_get(url: str, timeout: int = 20, limit: int = None) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit) if limit else r.read()


def download(url: str, limit: int):
    """فایل را تا سقف مشخص دانلود می‌کند؛ اگر بزرگ‌تر بود None."""
    req = urllib.request.Request(url, headers=UA)
    data = bytearray()
    with urllib.request.urlopen(req, timeout=60) as r:
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            data += chunk
            if len(data) > limit:
                return None
    return bytes(data)


def tg(method: str, params: dict, files: dict = None, timeout: int = 60):
    """فراخوانی API تلگرام (متن، عکس، ویدیو). در خطا TgError می‌دهد."""
    if files:
        boundary = uuid.uuid4().hex
        body = bytearray()
        for k, v in params.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
        for k, (fname, data, ctype) in files.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                     f'filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n').encode() + data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(API + method, data=bytes(body), headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(API + method, data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read().decode())
        except Exception:
            res = {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        raise TgError(type(e).__name__)  # آدرس (که توکن دارد) در پیام خطا نیاید
    if not res.get("ok"):
        raise TgError(res.get("description", "unknown"))
    return res


# ───────────── فید ─────────────
def clean_html(t) -> str:
    t = re.sub(r"<[^>]+>", " ", t or "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def is_persian(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("؀" <= c <= "ۿ" for c in letters) / len(letters) > 0.4


def parse_feed(xml_bytes: bytes, source: str, emoji: str):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = clean_html(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        raw = (it.findtext("description") or "") + " " + (
            it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")
        ts = time.time()
        pub = it.findtext("pubDate")
        if pub:
            try:
                ts = email.utils.parsedate_to_datetime(pub).timestamp()
            except Exception:
                pass
        image, best_w, video = None, -1, None
        for e in it.iter():
            tag = e.tag.split("}")[-1]
            if tag not in ("thumbnail", "content", "enclosure"):
                continue
            u = e.get("url")
            kind = (e.get("type") or e.get("medium") or "").lower()
            if not u:
                continue
            if "video" in kind or u.lower().split("?")[0].endswith((".mp4", ".m4v")):
                video = video or u
                continue
            if tag == "enclosure" and not kind.startswith("image"):
                continue
            try:
                w = int(e.get("width") or 0)
            except ValueError:
                w = 0
            if w >= best_w:
                image, best_w = u, w
        if not video:  # ویدیو داخل متن خبر
            m = re.search(r'<(?:video|source)[^>]+src=["\']([^"\']+\.mp4[^"\']*)', raw, re.I)
            video = m.group(1) if m else None
        if not image:  # عکس داخل متن خبر
            m = re.search(r'<img[^>]+src=["\']([^"\']+)', raw, re.I)
            image = m.group(1) if m else None
        items.append({"title": title, "link": link, "summary": clean_html(raw),
                      "ts": ts, "image": image, "video": video, "source": source, "emoji": emoji})
    return items


def load_feeds():
    feeds = []
    with open(FEEDS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                feeds.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "⚽"))
    return feeds


# ───────────── ترجمه و متن پست ─────────────
def translate_fa(text: str) -> str:
    """ترجمهٔ رایگان به فارسی (سرویس غیررسمی گوگل)."""
    if not text:
        return ""
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=fa&dt=t&q="
           + urllib.parse.quote(text))
    try:
        data = json.loads(http_get(url, timeout=15).decode("utf-8"))
        out = "".join(seg[0] for seg in data[0] if seg and seg[0])
    except Exception as e:
        raise TranslateError(type(e).__name__)
    if not out:
        raise TranslateError("empty")
    return out


def to_persian(text: str) -> str:
    return text if (not text or is_persian(text)) else translate_fa(text)


def upgrade_image(url: str) -> str:
    return re.sub(r"/standard/\d+/", "/standard/976/", url) if "ichef.bbci.co.uk" in url else url


def og_image(link: str):
    try:
        page = http_get(link, timeout=10, limit=300_000).decode("utf-8", "ignore")
    except Exception:
        return None
    m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', page))
    return html.unescape(m.group(1)) if m else None


def build_post(it: dict, title: str, summary: str, limit: int) -> str:
    """شکل پست (بین هر بخش دو Enter یعنی یک خط خالی):
    عنوان / خلاصه / [نام منبع] / امضای کانال"""
    head = f"{it['emoji']} <b>{html.escape(title)}</b>"
    source = f"[{html.escape(it['source'])}]"
    foot = source + "\n\n" + html.escape(SIGNATURE)
    budget = limit - len(head) - len(foot) - 10
    body = summary
    if len(body) > budget:
        body = body[:max(budget, 0)].rsplit(" ", 1)[0] + "…" if budget > 40 else ""
    parts = [head] + ([html.escape(body)] if body else []) + [foot]
    return "\n\n".join(parts)


# ───────────── ارسال ─────────────
def send_post(it: dict, title: str, summary: str):
    """اولویت: ویدیو، بعد عکس، بعد متن ساده."""
    caption = build_post(it, title, summary, 1000)
    base = {"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML"}
    if it.get("video"):
        try:
            tg("sendVideo", {**base, "video": it["video"], "supports_streaming": "true"})
            return "video"
        except TgError:
            pass
        try:  # تلگرام نتوانست از لینک بگیرد؛ خودمان دانلود و آپلود می‌کنیم
            data = download(it["video"], VIDEO_LIMIT)
            if data:
                tg("sendVideo", {**base, "supports_streaming": "true"},
                   files={"video": ("video.mp4", data, "video/mp4")}, timeout=300)
                return "video"
        except Exception as e:
            print("  ارسال ویدیو نشد:", type(e).__name__)
    candidates = [upgrade_image(it["image"])] if it.get("image") else []
    tried_og = False
    while True:
        if not candidates:
            if tried_og:
                break
            tried_og = True
            extra = og_image(it["link"])
            if extra:
                candidates.append(extra)
            continue
        try:
            tg("sendPhoto", {**base, "photo": candidates.pop(0)})
            return "photo"
        except TgError as e:
            print("  ارسال عکس نشد:", e)
    tg("sendMessage", {"chat_id": CHANNEL, "text": build_post(it, title, summary, 4000),
                       "parse_mode": "HTML", "disable_web_page_preview": "true"})
    return "text"


# ───────────── وضعیت ─────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(seen: list):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(seen[-3000:], f)


def main():
    if not TEST and (not BOT_TOKEN or not CHANNEL):
        sys.exit("BOT_TOKEN و CHANNEL باید تنظیم شوند.")
    items = []
    for name, url, emoji in load_feeds():
        try:
            got = parse_feed(http_get(url), name, emoji)
            print(f"✅ {name}: {len(got)} خبر  ({url})")
            items += got
        except Exception as e:
            print(f"❌ {name}: خوانده نشد ({type(e).__name__})  ({url})")
    if TEST:
        print(f"مجموع: {len(items)} خبر. (حالت آزمایشی؛ چیزی ارسال نشد)")
        return
    items.sort(key=lambda i: i["ts"], reverse=True)
    state = load_state()
    warmup = state is None          # بار اول: فقط علامت می‌زنیم تا خبرهای قدیمی یک‌جا ارسال نشوند
    seen = state or []
    seen_set = set(seen)
    cutoff = time.time() - MAX_AGE
    posted = 0
    for it in items:
        if it["ts"] < cutoff:
            continue
        h = hashlib.sha1(it["link"].encode()).hexdigest()[:16]
        if h in seen_set:
            continue
        if warmup:
            seen.append(h); seen_set.add(h)
            continue
        if posted >= MAX_PER_RUN:
            break
        try:
            title = to_persian(it["title"])
            try:
                summary = to_persian(it["summary"][:500]) if not it["summary"].startswith(it["title"][:40]) else ""
            except TranslateError:
                summary = ""
        except TranslateError as e:
            print("ترجمه نشد، دفعهٔ بعد دوباره:", e)
            break
        try:
            kind = send_post(it, title, summary)
        except TgError as e:
            print("❌ ارسال نشد:", e, "— ربات باید در کانال ادمین با دسترسی ارسال پیام باشد.")
            break
        print(f"📤 {kind}: {it['title'][:60]}")
        seen.append(h); seen_set.add(h)
        posted += 1
        time.sleep(3)
    save_state(seen)
    print(f"تمام شد. ارسال‌شده: {posted}" + (" (اجرای اول: فقط علامت‌گذاری)" if warmup else ""))


if __name__ == "__main__":
    main()
