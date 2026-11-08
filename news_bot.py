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


BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36",
              "Accept-Language": "fa,en;q=0.8"}


def download(url: str, limit: int, referer: str = None):
    """فایل را تا سقف مشخص دانلود می‌کند؛ اگر بزرگ‌تر بود None."""
    req = urllib.request.Request(url, headers={**BROWSER_UA, **({"Referer": referer} if referer else {})})
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


EXTRA_RE = re.compile(r"\s*[+＋]\s*(?:عکس|تصویر|تصاویر|فیلم|ویدیو|ویدئو|گزارش تصویری)(?:\s*ها)?\s*(?=[\s!؟?.،:|)\]]|$)")


def strip_extras(title: str) -> str:
    """عبارت‌هایی مثل «+عکس» یا «+فیلم» را از عنوان خبر حذف می‌کند."""
    t = re.sub(r"\s+", " ", EXTRA_RE.sub(" ", title)).strip()
    return re.sub(r"\s+([!؟?.،:])", r"\1", t)


def is_persian(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("؀" <= c <= "ۿ" for c in letters) / len(letters) > 0.4


def parse_feed(xml_bytes: bytes, source: str, emoji: str):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = strip_extras(clean_html(it.findtext("title")))
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
        imgs = []
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
            imgs.append(u)
            if w >= best_w:
                image, best_w = u, w
        if not video:  # ویدیو داخل متن خبر
            m = re.search(r'<(?:video|source)[^>]+src=["\']([^"\']+\.mp4[^"\']*)', raw, re.I)
            video = m.group(1) if m else None
        if not image:  # عکس داخل متن خبر
            m = re.search(r'<img[^>]+src=["\']([^"\']+)', raw, re.I)
            image = m.group(1) if m else None
        imgs += re.findall(r'<img[^>]+src=["\']([^"\']+)', raw, re.I)
        items.append({"title": title, "link": link, "summary": clean_html(raw),
                      "ts": ts, "image": image, "images": pick_images(image, imgs),
                      "video": video, "source": source, "emoji": emoji})
    return items


JUNK_RE = re.compile(r"logo|icon|avatar|pixel|spacer|sprite|banner|emoji|\.gif|\.svg|/ads?/", re.I)


def pick_images(main, urls, limit: int = 5):
    """عکس اصلی + عکس‌های دیگر خبر (بدون تکراری و آیکون)؛ برای ارسال آلبوم."""
    out, seen = [], set()
    for u in ([main] if main else []) + urls:
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith("http") or JUNK_RE.search(u):
            continue
        key = u.split("?")[0].rsplit("/", 1)[-1].lower()  # نسخه‌های هم‌اندازهٔ یک عکس را یکی بگیر
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out[:limit]


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


GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def gemini_translate(title: str, summary: str):
    """ترجمهٔ روان به سبک رسانه‌های ورزشی ایران (اختیاری، با کلید رایگان Gemini)."""
    prompt = (
        "You are a professional Persian (Farsi) sports journalist working for an Iranian sports outlet. "
        "Translate the football/sports news headline and summary below into fluent, natural, concise Persian, "
        "the way Iranian sports media (like Varzesh3) would write it. Use the common Persian spellings of "
        "player, coach, club, league and country names. Do not add facts or opinions. Keep the summary to "
        "at most 3 short sentences. If the summary is empty, return an empty string for it. "
        'Reply only with JSON: {"title": "...", "summary": "..."}\n\n'
        + json.dumps({"title": title, "summary": summary}, ensure_ascii=False))
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read().decode())
        obj = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        t = str(obj.get("title", "")).strip()
        sm = str(obj.get("summary", "")).strip()
    except Exception as e:
        raise TranslateError("gemini:" + type(e).__name__)
    if not t:
        raise TranslateError("gemini:empty")
    return t, sm


def translate_item(it: dict):
    """(عنوان، خلاصه) فارسی. اگر Gemini تنظیم باشد با آن، وگرنه/در خطا با ترجمهٔ گوگل."""
    summary_src = "" if it["summary"].startswith(it["title"][:40]) else it["summary"][:500]
    if is_persian(it["title"]) and (not summary_src or is_persian(summary_src)):
        return it["title"], summary_src
    if GEMINI_KEY:
        try:
            return gemini_translate(it["title"], summary_src)
        except TranslateError as e:
            print("  Gemini جواب نداد، ترجمهٔ ساده:", e)
    title = to_persian(it["title"])
    try:
        summary = to_persian(summary_src)
    except TranslateError:
        summary = ""
    return title, summary


def to_persian(text: str) -> str:
    return text if (not text or is_persian(text)) else translate_fa(text)


def upgrade_image(url: str) -> str:
    return re.sub(r"/standard/\d+/", "/standard/976/", url) if "ichef.bbci.co.uk" in url else url


def _meta(page: str, prop: str):
    m = (re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']+)' % re.escape(prop), page, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']%s["\']' % re.escape(prop), page, re.I))
    return html.unescape(m.group(1)) if m else None


def fetch_page(link: str) -> str:
    req = urllib.request.Request(link, headers=BROWSER_UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(1_500_000).decode("utf-8", "ignore")


VIDEO_EXT = r"\.(?:mp4|m3u8|webm)"


def find_videos(page: str, link: str) -> list:
    """همهٔ لینک‌های ویدیوی داخل صفحهٔ خبر (mp4 اول، بعد webm، بعد m3u8)."""
    txt = html.unescape(page.replace("\\/", "/").replace("\\u002F", "/").replace("\\u0026", "&"))
    found = []
    for prop in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        v = _meta(txt, prop)
        if v:
            found.append(v)
    for m in re.finditer(r'<(?:video|source)[^>]+src=["\']([^"\']+)', txt, re.I):
        found.append(m.group(1))
    for m in re.finditer(r'(?:contentUrl|file|src|url|data-src|data-video|video_url)["\']?\s*[:=]\s*["\']([^"\']+?' + VIDEO_EXT + r'[^"\']*)', txt, re.I):
        found.append(m.group(1))
    for m in re.finditer(r'["\'(=\s]((?:https?:)?//[^"\'\s<>()]+?' + VIDEO_EXT + r'(?:\?[^"\'\s<>()]*)?)', txt, re.I):
        found.append(m.group(1))
    out = []
    for u in found:
        u = urllib.parse.urljoin(link, u.strip())
        if u.startswith("http") and re.search(VIDEO_EXT, u, re.I) and u not in out and not JUNK_RE.search(u):
            out.append(u)
    out.sort(key=lambda u: 0 if re.search(r"\.mp4", u, re.I) else (1 if ".webm" in u.lower() else 2))
    return out


def _fetch(url: str, referer: str = None, limit: int = 1_500_000) -> str:
    req = urllib.request.Request(url, headers={**BROWSER_UA, **({"Referer": referer} if referer else {})})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(limit).decode("utf-8", "ignore")


def head_size(url: str, referer: str):
    """حجم فایل از روی هدر (اگر معلوم باشد)، وگرنه None."""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={**BROWSER_UA, "Referer": referer})
        with urllib.request.urlopen(req, timeout=15) as r:
            n = r.headers.get("Content-Length")
            return int(n) if n else None
    except Exception:
        return None


def aparat_hashes(page: str) -> list:
    hs = re.findall(r"aparat\.com/video/video/embed/videohash/([A-Za-z0-9]+)", page)
    hs += re.findall(r"aparat\.com/v/([A-Za-z0-9]+)", page)
    return list(dict.fromkeys(hs))


def aparat_sources(h: str, ref: str = "https://www.khabarvarzeshi.com/", dbg: bool = False) -> list:
    """لینک‌های فایل ویدیوی آپارات (mp4 با کیفیت بالا به پایین؛ بعد HLS)."""
    cands = []  # (اولویت، ارتفاع، لینک)

    def add(u, height=0):
        u = u.replace("\\/", "/")
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http") and re.search(VIDEO_EXT, u, re.I) and all(u != c[2] for c in cands):
            cands.append((0 if ".mp4" in u.lower() else 1, height, u))

    def walk(o, height=0):
        if isinstance(o, dict):
            p = str(o.get("profile", o.get("quality", o.get("label", ""))))
            m = re.match(r"(\d{3,4})", p)
            hh = int(m.group(1)) if m else height
            for v in o.values():
                walk(v, hh)
        elif isinstance(o, list):
            for v in o:
                walk(v, height)
        elif isinstance(o, str):
            add(o, height)

    for api in (f"https://www.aparat.com/api/fa/v1/video/video/show/videohash/{h}",
                f"https://www.aparat.com/etc/api/video/videohash/{h}"):
        try:
            raw = _fetch(api, ref)
            if dbg:
                print("   API:", api.split("aparat.com")[1][:50], "←", raw[:300].replace("\n", " "))
            walk(json.loads(raw))
        except Exception as e:
            print("  API آپارات جواب نداد:", api.split("aparat.com")[1][:40], type(e).__name__, getattr(e, "code", ""))
        if cands:
            break
    if not cands:  # صفحهٔ پخش‌کننده
        try:
            frame = _fetch(f"https://www.aparat.com/video/video/embed/videohash/{h}/vt/frame", ref)
            txt = html.unescape(frame.replace("\\/", "/"))
            if dbg:
                print(f"   frame: {len(frame)} نویسه؛ شروع:", re.sub(r"\s+", " ", frame[:200]))
                for m in list(re.finditer(r"m3u8|\.mp4|multiSRC|file_link", txt))[:4]:
                    print("   frame سرنخ:", re.sub(r"\s+", " ", txt[max(0, m.start() - 80):m.end() + 120]))
            for m in re.finditer(r'(?:src|file|url|hls)["\']?\s*[:=]\s*["\']([^"\']+?' + VIDEO_EXT + r'[^"\']*)', txt, re.I):
                add(m.group(1))
        except Exception as e:
            print("  صفحهٔ پخش آپارات باز نشد:", type(e).__name__, getattr(e, "code", ""))
    ok = [c for c in cands if c[1] == 0 or c[1] <= 720]
    ok.sort(key=lambda c: (c[0], -c[1]))
    return [c[2] for c in ok]


def page_media(link: str, want_video: bool):
    """از صفحهٔ خبر: عکس (og:image) و فهرست لینک‌های ویدیو."""
    try:
        page = fetch_page(link)
    except Exception as e:
        print("  صفحهٔ خبر باز نشد:", type(e).__name__, getattr(e, "code", ""))
        return None, [], None
    videos = find_videos(page, link) if want_video else []
    hashes = aparat_hashes(page) if want_video else []
    watch = f"https://www.aparat.com/v/{hashes[0]}" if hashes else None  # لینک تماشا (برای ویدیوی حجیم)
    if want_video and not videos:
        for h in hashes:
            videos += aparat_sources(h, link)
            if videos:
                break
        if videos:
            print(f"  ویدیوی آپارات پیدا شد ({len(videos)} فایل)")
    return _meta(page, "og:image"), videos, watch


def debug_page(link: str, trial: bool = False):
    """برای عیب‌یابی: نشان می‌دهد در صفحهٔ خبر چه چیزهایی دربارهٔ ویدیو هست."""
    try:
        page = fetch_page(link)
    except Exception as e:
        print("   ❌ صفحه باز نشد:", type(e).__name__, getattr(e, "code", ""))
        return
    print(f"   صفحه باز شد ({len(page)} نویسه)، تگ video: {len(re.findall(r'<video', page, re.I))}")
    print("   ویدیوهای پیدا‌شده:", find_videos(page, link)[:3] or "هیچ")
    for h in aparat_hashes(page)[:1]:
        srcs = aparat_sources(h, link, dbg=True)
        print("   آپارات:", h, "←", [u[:90] for u in srcs[:3]] or "فایلی پیدا نشد")
        if srcs and ".m3u8" in srcs[0]:
            print("   کیفیت‌های HLS:", hls_variants(srcs[0], "https://www.aparat.com/") or "نامشخص")
        if trial and srcs:
            t0 = time.time()
            try:
                data = ffmpeg_fetch(srcs[0], "https://www.aparat.com/") if ".m3u8" in srcs[0] \
                    else download(srcs[0], VIDEO_LIMIT, "https://www.aparat.com/")
                print(f"   🧪 آزمایش دانلود: {'موفق، %d مگابایت' % (len(data) // 1048576) if data else 'بزرگ‌تر از ۴۸ مگابایت'}"
                      f" ({time.time() - t0:.0f} ثانیه)")
            except Exception as e:
                print("   🧪 آزمایش دانلود ناموفق:", type(e).__name__, getattr(e, "code", ""))
    hints = re.findall(r'(?:https?:)?//[^"\'\s<>()\\]+(?:video|player|aparat|embed|stream|media)[^"\'\s<>()\\]*', page, re.I)
    for h in list(dict.fromkeys(hints))[:8]:
        print("   سرنخ:", h[:140])
    for f in re.findall(r'<iframe[^>]+src=["\']([^"\']+)', page, re.I)[:3]:
        print("   iframe:", f[:140])


def og_image(link: str):
    return page_media(link, False)[0]


def build_post(it: dict, title: str, summary: str, limit: int, watch: str = None) -> str:
    """شکل پست (بین هر بخش دو Enter یعنی یک خط خالی):
    عنوان / خلاصه / [لینک تماشای ویدیو، اگر ویدیو فرستاده نشده] / [نام منبع] / امضای کانال"""
    head = f"{it['emoji']} <b>{html.escape(title)}</b>"
    source = f"[{html.escape(it['source'])}]"
    foot = source + "\n\n" + html.escape(SIGNATURE)
    watch_line = f"🎬 مشاهدهٔ ویدیو: {watch}" if watch else ""
    budget = limit - len(head) - len(foot) - len(watch_line) - 10
    body = summary
    if len(body) > budget:
        body = body[:max(budget, 0)].rsplit(" ", 1)[0] + "…" if budget > 40 else ""
    parts = [head] + ([html.escape(body)] if body else []) + ([watch_line] if watch_line else []) + [foot]
    return "\n\n".join(parts)


# ───────────── ارسال ─────────────
_FFMPEG = None


def get_ffmpeg() -> str:
    """مسیر ffmpeg؛ اگر روی سیستم نبود، همان لحظه از pip (بستهٔ imageio-ffmpeg) نصب می‌شود."""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    import shutil, subprocess
    path = shutil.which("ffmpeg")
    if not path:
        try:
            import imageio_ffmpeg
        except ImportError:
            print("  ffmpeg نبود؛ نصب از pip ...")
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "imageio-ffmpeg"],
                           check=True, timeout=240)
            import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
    _FFMPEG = path
    return path


def hls_variants(url: str, referer: str) -> list:
    """کیفیت‌های یک فهرست HLS به ترتیب ظاهر شدن: [(ارتفاع، پهنای‌باند), ...]"""
    try:
        text = _fetch(url, referer, 300_000)
    except Exception:
        return []
    out = []
    for line in text.splitlines():
        if line.startswith("#EXT-X-STREAM-INF"):
            r = re.search(r"RESOLUTION=\d+x(\d+)", line)
            bw = re.search(r"BANDWIDTH=(\d+)", line)
            out.append((int(r.group(1)) if r else 0, int(bw.group(1)) if bw else 0))
    return out


def ffmpeg_fetch(url: str, referer: str):
    """ویدیوی HLS (m3u8) را با ffmpeg به mp4 تبدیل می‌کند. از کیفیت بالا شروع می‌کند
    و اگر از ۴۸ مگابایت بزرگ‌تر شد، کیفیت پایین‌تر را امتحان می‌کند. اگر نشد None."""
    import subprocess, tempfile
    variants = hls_variants(url, referer)
    idxs = sorted(range(len(variants)), key=lambda i: -variants[i][0])
    idxs = [i for i in idxs if 0 < variants[i][0] <= 720] or idxs
    attempts = idxs[:3] or [None]
    hdr = f"Referer: {referer}\r\nUser-Agent: {BROWSER_UA['User-Agent']}\r\n"
    for idx in attempts:
        out = os.path.join(tempfile.mkdtemp(), "v.mp4")
        base = [get_ffmpeg(), "-y", "-loglevel", "error", "-headers", hdr, "-i", url]
        tail = ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart",
                "-fs", str(VIDEO_LIMIT + 1048576), "-t", "900", out]
        cmds = [base + ["-map", f"0:p:{idx}"] + tail, base + tail] if idx is not None else [base + tail]
        ok = False
        for cmd in cmds:
            try:
                subprocess.run(cmd, check=True, timeout=300, capture_output=True)
                ok = os.path.exists(out) and os.path.getsize(out) > 0
            except subprocess.CalledProcessError as e:
                print("  ffmpeg خطا:", (e.stderr or b"")[-200:].decode("utf-8", "ignore").strip())
            if ok:
                break
        if not ok:
            continue
        size = os.path.getsize(out)
        label = f"{variants[idx][0]}p" if idx is not None and variants else "?"
        if size > VIDEO_LIMIT:
            print(f"  کیفیت {label} بزرگ‌تر از ۴۸ مگابایت شد؛ پایین‌تر امتحان می‌شود")
            continue
        print(f"  ویدیو آماده شد: {label}، {size // 1048576} مگابایت")
        with open(out, "rb") as f:
            return f.read()
    return None


def send_post(it: dict, title: str, summary: str):
    """اولویت: ویدیو، بعد عکس‌ها، بعد متن ساده."""
    caption = build_post(it, title, summary, 1000)
    base = {"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML"}
    page_img, watch = None, None
    videos = [it["video"]] if it.get("video") else []
    if not videos:
        looks_video = bool(re.search(r"ویدیو|ویدئو|فیلم|video|watch|highlights|clip", it["title"], re.I))
        page_img, videos, watch = page_media(it["link"], looks_video)
        if looks_video and not videos:
            print("  🔍 ویدیو در صفحهٔ خبر پیدا نشد (برای بررسی، اجرای «test» را بزنید)")
    for v in videos[:6]:
        is_hls = ".m3u8" in v.lower()
        ref = "https://www.aparat.com/" if "aparat" in v.lower() else it["link"]
        if not is_hls:
            size = head_size(v, ref)
            if size and size > VIDEO_LIMIT:
                print(f"  ویدیو {size // 1048576}MB بزرگ است؛ کیفیت پایین‌تر امتحان می‌شود")
                continue
        if not is_hls and "aparat" not in v.lower():
            try:
                tg("sendVideo", {**base, "video": v, "supports_streaming": "true"})
                return "video"
            except TgError:
                pass
        try:  # تلگرام نتوانست از لینک بگیرد؛ خودمان دانلود و آپلود می‌کنیم
            data = ffmpeg_fetch(v, ref) if is_hls else download(v, VIDEO_LIMIT, ref)
            if data:
                tg("sendVideo", {**base, "supports_streaming": "true"},
                   files={"video": ("video.mp4", data, "video/mp4")}, timeout=300)
                return "video"
            print("  ویدیو بزرگ‌تر از ۴۸ مگابایت است:", v[:100])
        except Exception as e:
            print("  ارسال ویدیو نشد:", type(e).__name__, getattr(e, "code", ""), v[:100])
    if watch:  # ویدیو فرستاده نشد (مثلاً حجم زیاد): لینک تماشا را زیر خبر می‌گذاریم
        caption = build_post(it, title, summary, 1000, watch)
        base["caption"] = caption
    imgs = [upgrade_image(u) for u in it.get("images", [])]
    if len(imgs) >= 2:  # چند عکس: آلبوم
        media = [{"type": "photo", "media": u} for u in imgs]
        media[0].update({"caption": caption, "parse_mode": "HTML"})
        try:
            tg("sendMediaGroup", {"chat_id": CHANNEL, "media": json.dumps(media)})
            return "album"
        except TgError as e:
            print("  ارسال آلبوم نشد، یک عکس می‌فرستیم:", e)
    candidates = [upgrade_image(it["image"])] if it.get("image") else []
    tried_og = False
    while True:
        if not candidates:
            if tried_og:
                break
            tried_og = True
            extra = page_img or og_image(it["link"])
            if extra:
                candidates.append(extra)
            continue
        try:
            tg("sendPhoto", {**base, "photo": candidates.pop(0)})
            return "photo"
        except TgError as e:
            print("  ارسال عکس نشد:", e)
    tg("sendMessage", {"chat_id": CHANNEL, "text": build_post(it, title, summary, 4000, watch),
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
        vids = [i for i in sorted(items, key=lambda i: i["ts"], reverse=True)
                if re.search(r"ویدیو|ویدئو|فیلم|video", i["title"], re.I)][:3]
        for n, i in enumerate(vids):
            print(f"\n🎬 بررسی ویدیو: {i['title'][:60]}\n   {i['link']}\n   ویدیو در فید: {i['video']}")
            debug_page(i["link"], trial=(n == 0))
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
            title, summary = translate_item(it)
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
