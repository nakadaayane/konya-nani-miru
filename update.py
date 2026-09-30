"""Filmarks から「観たい（Clip!）」映画と、観たい以外のおすすめ（配信の人気作・今話題の映画）を取得して、
「今夜なに観る？」ページのデータを作る。

使い方:
    python filmarks/update.py            # 全部取得 → data/clips.json → out/filmarks-clips.html + out/data.json
    python filmarks/update.py --build    # 取得せず、既存の clips.json から out/ だけ作り直す
    python filmarks/update.py --public   # 公開版（GitHub Pages）: site/index.html + site/data.json
    python filmarks/update.py --test 1231 121234   # 指定IDの作品ページだけ解析して表示

Filmarks の公開ページ（ログイン不要）を1秒間隔で読むだけなので、アカウントには触れない。
観た作品（Mark済み）はおすすめから外す。

公開版は誰でも見られるので、ポスターは複製せず Filmarks の画像を直接表示し、あらすじは載せない。
"""
import base64
import io
import json
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from PIL import Image

USER_ID = "bsgjxnegvakdy"
BASE = "https://filmarks.com"
WAIT = 1.0  # 1リクエストごとの待ち秒数

# 観たい以外のおすすめの取り先（一覧は1ページ36本）
VOD_LISTS = {"U-NEXT": "unext", "Prime Video": "prime_video"}  # 契約中のサービスの人気ランキング
VOD_PAGES = 10    # 上位360本
TREND_PAGES = 10  # 今話題の上位360本
AWARDS = {        # 映画賞の受賞作（人気順）: 表示名 -> Filmarks の賞ID
    "アカデミー賞": 1, "日本アカデミー賞": 19,
    "カンヌ国際映画祭": 42, "ヴェネチア国際映画祭": 44, "ベルリン国際映画祭": 43,
}
AWARD_PAGES = 2   # 各賞の上位72本
SIMILAR_TOP = 100 # 観たい作品の「似ている作品」でよく挙がる上位何本を足すか

# 映画館（観たい作品のうち上映中のもの）
ORIGIN = {"name": "大岡山駅", "lat": 35.607474, "lng": 139.685767}  # 近い順の基準
PREFS = {13: "tokyo", 14: "kanagawa"}  # Filmarks の都道府県ID -> URLの名前
SCHEDULE_DAYS = 3        # 今日・明日・あさって
MAX_DISTANCE = 30000     # 大岡山から30kmまでの映画館
FAV_THEATERS = ["109シネマズ二子玉川", "TOHOシネマズ 日比谷", "TOHOシネマズ 大井町"]  # よく行く映画館（イベントも取る）

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
POSTERS = DATA / "posters"
OUT = ROOT / "out"    # Claude の Artifact 用（非公開）
SITE = ROOT / "site"  # GitHub Pages 用（公開）
TEMPLATE = ROOT / "template.html"
NOTE_PRIVATE = "その日最初にClaudeアプリを開いたときに自動で更新します"
JST = timezone(timedelta(hours=9))  # GitHub Actions は世界標準時で動くので、日本時間を明示する
NOTE_PUBLIC = "毎朝6時ごろに自動で更新しています"

session = requests.Session()
session.headers["User-Agent"] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


def get(url, retries=3):
    for i in range(retries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r
            print(f"  HTTP {r.status_code}: {url}")
        except requests.RequestException as e:
            print(f"  error {e}: {url}")
        time.sleep(3 * (i + 1))
    return None


def get_json(url, params):
    """Filmarks のページが裏で使っている JSON（上映館・上映時刻）。表示用と同じものを1件ずつ読む"""
    try:
        r = session.get(url, params=params, timeout=30,
                        headers={"Accept": "application/json", "Referer": f"{BASE}/"})
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            return r.json()
        print(f"  JSON取得できず HTTP {r.status_code}: {url}")
    except (requests.RequestException, ValueError) as e:
        print(f"  error {e}: {url}")
    return None


def txt(el):
    return el.get_text(strip=True) if el else ""


def movie_ids_in(soup, card_sel):
    ids = []
    for card in soup.select(card_sel):
        a = card.select_one('a[href^="/movies/"]')
        m = a and re.match(r"/movies/(\d+)", a["href"])
        if m and int(m.group(1)) not in ids:
            ids.append(int(m.group(1)))
    return ids


def paged_ids(path, card_sel, label):
    """/users/... の一覧（観たい・観た）を最後のページまでたどってIDを集める"""
    ids, page = [], 1
    while True:
        r = get(f"{BASE}{path}?page={page}")
        if r is None:
            break
        soup = BeautifulSoup(r.text, "lxml")
        got = movie_ids_in(soup, card_sel)
        if not got:
            break
        ids += [i for i in got if i not in ids]
        print(f"{label} page {page}: {len(got)}件（累計 {len(ids)}）")
        if not soup.select_one('.c2-pagination a[rel="next"]'):
            break
        page += 1
        time.sleep(WAIT)
    return ids


# ---------- 作品ページ ----------
def info_after(soup, container_sel, label):
    """「製作国・地域：」「ジャンル：」などの見出しの直後の ul から項目を取る"""
    for h in soup.select(f"{container_sel} h3"):
        if txt(h).startswith(label):
            ul = h.find_next_sibling()
            if ul and ul.name == "ul":
                return [txt(a) for a in ul.select("li")]
    return []


def release_status(root):
    if root.select_one(".c2-tag-release-now"):
        return "上映中"
    if root.select_one('[class*="c2-tag-release-coming"]'):
        return "公開予定"
    return None


def parse_movie(mid, html):
    soup = BeautifulSoup(html, "lxml")
    d = {"id": mid, "url": f"{BASE}/movies/{mid}"}

    d["title"] = txt(soup.select_one(".p-content-detail__title span"))
    m = re.search(r"(\d{4})", txt(soup.select_one(".p-content-detail__title small a")))
    d["year"] = int(m.group(1)) if m else None
    d["original"] = txt(soup.select_one(".p-content-detail__original"))

    d["release"] = None
    d["runtime"] = None
    for h in soup.select(".p-content-detail__primary-info h3"):
        t = txt(h)
        m = re.search(r"上映日：\s*(\d{4})年(\d{2})月(\d{2})日", t)
        if m:
            d["release"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.search(r"上映時間：\s*(\d+)分", t)
        if m:
            d["runtime"] = int(m.group(1))
    d["countries"] = info_after(soup, ".p-content-detail__primary-info", "製作国")
    d["genres"] = info_after(soup, ".p-content-detail__secondary-info", "ジャンル")
    d["distributors"] = info_after(soup, ".p-content-detail__secondary-info", "配給")

    try:
        d["score"] = float(txt(soup.select_one(".p-content-detail-state .c2-rating-l__text")))
    except ValueError:
        d["score"] = None  # 「-」= まだ評価なし

    left = soup.select_one(".p-content-detail__left") or soup
    d["marks"] = d["clips"] = None
    for sel, key in ((".js-btn-mark", "marks"), (".js-btn-clip", "clips")):
        el = left.select_one(sel)
        attr = el and (el.get("data-mark") or el.get("data-clip"))
        if attr:
            try:
                d[key] = json.loads(attr).get("count")
            except json.JSONDecodeError:
                pass

    poster = left.select_one(".c2-poster-l img")
    d["poster_src"] = poster["src"] if poster else None
    d["status"] = release_status(left.select_one(".c2-poster-l") or left)
    m = re.search(r"(\d+)館", txt(left.select_one(".c2-button-theater-l__subtext")))
    d["theaters"] = int(m.group(1)) if m else 0

    d["trailer"] = None
    d["official"] = None
    for a in left.select(".p-content-detail-links a"):
        li = a.find_parent("li")
        cls = " ".join(li.get("class", [])) if li else ""
        if "youtube" in cls:
            d["trailer"] = a["href"]
        elif "official" in cls:
            d["official"] = a["href"]

    syn = soup.select_one("content-detail-synopsis")
    try:
        d["synopsis"] = json.loads(syn.get(":outline")) if syn else ""
    except (json.JSONDecodeError, TypeError):
        d["synopsis"] = ""

    d["directors"] = []
    for blk in soup.select(".p-content-detail__people-list-others-inner"):
        if txt(blk.select_one("h3")) == "監督":
            d["directors"] = [txt(x) for x in blk.select(".c2-button-tertiary-s__text")]
    d["cast"] = [
        txt(x) for x in soup.select(".p-people-list__casts .c2-button-tertiary-s-multi-text__text")
    ][:8]

    vods = []
    for a in soup.select("#js-tab-content__vod a.c2-list-vod"):
        name = a.get("data-gtm-info") or txt(a.select_one(".c2-list-vod__content-header-text-title"))
        kinds = [txt(t) for t in a.select(".c2-list-vod__content-tag [class$='__text']")]
        vods.append({"name": name, "kinds": kinds, "href": a.get("href")})
    d["vod"] = vods

    d["similar_ids"] = []
    for sec in soup.select(".c2-works-collection-horizontal-scroll"):
        if txt(sec.select_one("h3")) == "似ている作品":
            d["similar_ids"] = movie_ids_in(sec, ".c2-works-card-m")
    return d


# ---------- ランキング一覧（配信サービス別・今話題） ----------
def parse_cassette(c):
    """一覧ページの1作品分。作品ページを開かなくても上映時間・評価・あらすじ（冒頭）まで取れる"""
    clip = json.loads(c["data-clip"])
    mid = clip["movie_id"]
    d = {"id": mid, "url": f"{BASE}/movies/{mid}", "clips": clip.get("count")}
    try:
        d["marks"] = json.loads(c.get("data-mark") or "{}").get("count")
    except json.JSONDecodeError:
        d["marks"] = None
    d["title"] = txt(c.select_one(".p-content-cassette__title"))
    d["original"] = ""
    img = c.select_one(".p-content-cassette__jacket img")
    d["poster_src"] = img["src"] if img else None

    d["release"] = d["runtime"] = None
    d["countries"] = []
    for h in c.select(".p-content-cassette__other-info h4"):
        label, nxt = txt(h), h.find_next_sibling()
        if label.startswith("上映日") and nxt is not None:
            m = re.search(r"(\d{4})年(\d{2})月(\d{2})日", txt(nxt))
            if m:
                d["release"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        elif label.startswith("上映時間") and nxt is not None:
            m = re.search(r"(\d+)分", txt(nxt))
            if m:
                d["runtime"] = int(m.group(1))
        elif label.startswith("製作国") and nxt is not None and nxt.name == "ul":
            d["countries"] = [txt(li) for li in nxt.select("li")]
    d["year"] = int(d["release"][:4]) if d["release"] else None
    d["genres"] = [txt(li) for li in c.select("ul.genres li")]
    d["distributors"] = [txt(li) for li in c.select("ul.distributors li")]
    try:
        d["score"] = float(txt(c.select_one(".c-rating__score")))
    except ValueError:
        d["score"] = None
    d["synopsis"] = txt(c.select_one(".p-content-cassette__synopsis-desc-text"))
    d["directors"], d["cast"] = [], []
    for wrap in c.select(".p-content-cassette__people-wrap"):
        term = txt(wrap.select_one("h4"))
        names = [txt(li) for li in wrap.select("li")]
        if term == "監督":
            d["directors"] = names
        elif term == "出演者":
            d["cast"] = names[:8]
    d["status"] = release_status(c)
    d["theaters"] = 0
    d["trailer"] = d["official"] = None
    btn = c.select_one("a.p-content-cassette__vod-button")
    kinds = [txt(x) for x in c.select(".c-vod-service-types__label")]
    d["has_vod"] = bool(kinds)  # 見放題・レンタルなどの印。なければどこでも配信していない
    d["vod"] = [{"name": btn.get("data-gtm-info"), "kinds": kinds, "href": btn.get("href")}] if btn else []
    return d


def fetch_ranking(path, pages, label):
    """ランキング一覧を pages ページ分読んで {id: (順位, 作品データ)} を返す"""
    out = {}
    for page in range(1, pages + 1):
        time.sleep(WAIT)
        r = get(f"{BASE}{path}?page={page}")
        if r is None:
            break
        soup = BeautifulSoup(r.text, "lxml")
        cs = soup.select(".js-cassette")
        for i, c in enumerate(cs):
            try:
                d = parse_cassette(c)
            except Exception as e:  # 1件壊れていても最後まで進める
                print(f"  parse error ({label}): {e}")
                continue
            out.setdefault(d["id"], ((page - 1) * 36 + i + 1, d))
        print(f"{label} page {page}: {len(cs)}件")
        if not cs:
            break
    return out


def fetch_poster(d):
    """ポスターを 240×336 の webp で保存（一度取ったら再取得しない）"""
    src = d.get("poster_src")
    if not src:
        return
    path = POSTERS / f"{d['id']}.webp"
    if path.exists():
        return
    small = re.sub(r"/fitpad/\d+/\d+/", "/fitpad/240/336/", src)
    r = get(small) or get(src)
    if r is not None:
        path.write_bytes(r.content)
        time.sleep(0.2)


# ---------- 映画館（観たい × 上映中） ----------
def fetch_schedules(clips):
    """観たい作品のうち上映中（または近日公開）のものについて、大岡山から近い上映館と上映時刻を取る"""
    today = datetime.now(JST).date()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(SCHEDULE_DAYS)]
    targets = [d for d in clips if d.get("status") == "上映中" or d.get("theaters")
               or (d.get("status") == "公開予定" and d.get("release") and d["release"] <= dates[-1])]
    theaters, shows, seen = {}, [], set()
    for n, d in enumerate(targets, 1):
        for pid, slug in PREFS.items():
            for date in dates:
                time.sleep(WAIT)
                data = get_json(f"{BASE}/movies/{d['id']}/areas", {
                    "scheduleDate": date, "prefectureId": pid, "limit": 1000,
                    "latitude": ORIGIN["lat"], "longitude": ORIGIN["lng"]})
                for area in (data or {}).get("areas", []):
                    for t in area.get("theaters", []):
                        dist = t.get("distance")
                        if dist is None or dist > MAX_DISTANCE:
                            continue
                        screens = [{
                            "formats": sc.get("screenFormat") or [],
                            "info": sc.get("information"),
                            "times": [[x.get("start"), x.get("end")] for x in sc.get("showtimes") or []],
                        } for sc in t.get("screens") or []]
                        screens = [sc for sc in screens if sc["times"]]
                        if not screens:
                            continue
                        theaters.setdefault(t["id"], {
                            "id": t["id"], "name": t.get("name"), "distance": dist,
                            "area": area.get("name"), "official": t.get("url"),
                            "page": f"{BASE}/theaters/{slug}/{area.get('id')}/{t['id']}",
                            "fav": t.get("name") in FAV_THEATERS,
                        })
                        key = (d["id"], t["id"], date)
                        if key in seen:  # 都県の境目の映画館は、東京都と神奈川県の両方で返ってくる
                            continue
                        seen.add(key)
                        shows.append({"movie": d["id"], "theater": t["id"], "date": date, "screens": screens})
        print(f"[上映館 {n}/{len(targets)}] {d['title']}")
    return {"origin": ORIGIN, "dates": dates,
            "theaters": sorted(theaters.values(), key=lambda t: t["distance"]), "shows": shows}


# ---------- イベント ----------
def clip_matcher(clips):
    """イベントの題名（『』「」の中）が観たい作品と一致したら、その作品IDを返す"""
    def norm(x):
        return re.sub(r"[\s　『』「」【】・:：!！?？\-－―〜~]", "", x or "").lower()
    names = [(norm(d["title"]), d["id"]) for d in clips if len(norm(d["title"])) >= 2]

    def match(text):
        for q in re.findall(r"[『「](.+?)[』」]", text or ""):
            nq = norm(q)
            for name, mid in names:
                if nq == name or (len(name) >= 4 and nq.startswith(name)):
                    return mid
        return None
    return match


def fetch_toho_events(match):
    """TOHOシネマズの「舞台挨拶・イベント」から、日比谷・大井町で実施するものを拾う"""
    r = get("https://www.tohotheater.jp/event/")
    if r is None:
        return []
    r.encoding = "cp932"
    soup = BeautifulSoup(r.text, "lxml")
    keys = {"日比谷": "TOHOシネマズ 日比谷", "大井町": "TOHOシネマズ 大井町"}
    out = []
    for li in soup.select("li.c-mainList01__item"):
        a = li.select_one(".c-mainList01__title a")
        if not a:
            continue
        title, text = txt(a), txt(li.select_one(".c-mainList01__text"))
        url = "https://www.tohotheater.jp" + a["href"] if a["href"].startswith("/") else a["href"]
        where = [v for k, v in keys.items() if k in title + text]
        if not where and re.search(r"\d+劇場", title + text):  # 「TOHOシネマズ69劇場にて」は詳細ページの実施劇場を見る
            time.sleep(WAIT)
            d = get(url)
            if d is not None:
                d.encoding = "cp932"
                full = BeautifulSoup(d.text, "lxml").get_text(" ", strip=True)
                seg = full[full.find("実施劇場"):][:1000] if "実施劇場" in full else ""
                where = [v for k, v in keys.items() if k in seg]
        for w in where:
            out.append({"theater": w, "title": title, "text": text, "url": url,
                        "posted": txt(li.select_one(".c-mainList01__date")), "clip": match(title)})
    return out


def fetch_109_events(match, name="109シネマズ二子玉川", url="https://109cinemas.net/futakotamagawa/"):
    """109シネマズ二子玉川のトップの「お知らせ」（舞台挨拶・最速上映など）。60日より古いものは外す"""
    r = get(url)
    if r is None:
        return []
    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "lxml")
    limit = (datetime.now(JST) - timedelta(days=60)).strftime("%Y/%m/%d")
    out = []
    for li in soup.select("li"):
        a, t = li.select_one("h3 a"), li.select_one("time")
        if not (a and t):
            continue
        posted = txt(t).strip("[]")
        if posted < limit:
            continue
        href = a["href"]
        href = "https://109cinemas.net" + href if href.startswith("/") else href
        out.append({"theater": name, "title": txt(a), "text": "", "url": href,
                    "posted": posted.replace("/", "."), "clip": match(txt(a))})
    return out


def fetch_filmarks_events(match):
    """Filmarksリバイバル上映（上映中・上映予定・イベント・キャンペーン）と、FILMAGAのお知らせ"""
    revival, articles = [], []
    r = get("https://revival.filmarks.com/")
    if r is not None:
        soup = BeautifulSoup(r.text, "lxml")
        section = None
        for h in soup.select("h2, h3"):
            if h.name == "h2":
                section = txt(h)
                if section.startswith("上映終了") or section.startswith("終了した"):
                    break
                continue
            if section:
                a = h.find_parent("a") or h.find("a")
                href = a["href"] if a and a.get("href") else ""
                link = "https://revival.filmarks.com/" + href if href.startswith("#") else (href or "https://revival.filmarks.com/")
                revival.append({"section": section, "title": txt(h), "url": link, "clip": match(txt(h))})
    time.sleep(WAIT)
    r = get("https://filmaga.filmarks.com/writers/premium-ticket/")
    if r is not None:
        soup = BeautifulSoup(r.text, "lxml")
        seen = set()
        for a in soup.select('a[href*="/articles/"]'):
            title = txt(a)
            if not title or a["href"] in seen:
                continue
            seen.add(a["href"])
            articles.append({"title": title, "url": a["href"], "clip": match(title)})
            if len(articles) >= 10:
                break
    return {"revival": revival, "articles": articles}


def fetch_events(clips):
    match = clip_matcher(clips)
    out = {"theaters": [], "filmarks": {"revival": [], "articles": []},
           "fav": FAV_THEATERS, "official": {
               "109シネマズ二子玉川": "https://109cinemas.net/futakotamagawa/",
               "TOHOシネマズ 日比谷": "https://www.tohotheater.jp/event/",
               "TOHOシネマズ 大井町": "https://www.tohotheater.jp/event/"}}
    for f in (lambda: fetch_109_events(match), lambda: fetch_toho_events(match)):
        try:
            out["theaters"] += f()
        except Exception as e:  # 映画館のサイトが変わっても、ほかの取得は止めない
            print(f"  イベント取得エラー: {e}")
        time.sleep(WAIT)
    try:
        out["filmarks"] = fetch_filmarks_events(match)
    except Exception as e:
        print(f"  Filmarksイベント取得エラー: {e}")
    print(f"イベント: 映画館 {len(out['theaters'])}件、リバイバル {len(out['filmarks']['revival'])}件、"
          f"FILMAGA {len(out['filmarks']['articles'])}件")
    return out


# ---------- 出力 ----------
def poster_data_uri(path):
    """一覧では幅64pxで表示するので、高精細画面向けに128px幅へ縮めて埋め込む"""
    img = Image.open(path).convert("RGB")
    img.thumbnail((128, 180))
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=72)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()


def build(public=False):
    """非公開版: out/filmarks-clips.html ＋ out/data.json（ポスター埋め込み・あらすじあり）
    公開版:   site/index.html ＋ site/data.json（ポスターは Filmarks の画像を直接表示・あらすじなし）"""
    data = json.loads((DATA / "clips.json").read_text(encoding="utf-8"))
    data["update_note"] = NOTE_PUBLIC if public else NOTE_PRIVATE
    for d in data["movies"]:
        src = d.pop("poster_src", None)
        if public:
            d["poster"] = re.sub(r"/fitpad/\d+/\d+/", "/fitpad/300/420/", src) if src else None  # カード表示用に一回り大きく
            d["synopsis"] = ""
        else:
            p = POSTERS / f"{d['id']}.webp"
            d["poster"] = poster_data_uri(p) if p.exists() else None
    dest, page = (SITE, "index.html") if public else (OUT, "filmarks-clips.html")
    dest.mkdir(exist_ok=True)
    (dest / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    shutil.copyfile(TEMPLATE, dest / page)
    size = (dest / "data.json").stat().st_size / 1e6
    print(f"出力: {dest}（data.json {size:.1f}MB）")


def main():
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    args = sys.argv[1:]
    public = "--public" in args
    args = [a for a in args if a != "--public"]
    if args[:1] == ["--test"]:
        for mid in args[1:]:
            r = get(f"{BASE}/movies/{mid}")
            print(json.dumps(parse_movie(int(mid), r.text), ensure_ascii=False, indent=1))
            time.sleep(WAIT)
        return
    if args[:1] == ["--extras"]:
        data = json.loads((DATA / "clips.json").read_text(encoding="utf-8"))
        clips = [d for d in data["movies"] if d["source"] == "clip"]
        data["theater"] = fetch_schedules(clips)
        data["events"] = fetch_events(clips)
        (DATA / "clips.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        build(public)
        return
    if args[:1] == ["--build"]:
        build(public)
        return

    DATA.mkdir(exist_ok=True)
    POSTERS.mkdir(exist_ok=True)
    started = time.time()

    # 1. 観たい・観た
    clip_ids = paged_ids(f"/users/{USER_ID}/clips", ".p-contents-grid .c-content-item", "観たい")
    time.sleep(WAIT)
    mark_ids = set(paged_ids(f"/users/{USER_ID}", ".c-content-card", "観た"))

    # 2. ランキングと映画賞（観たい作品にも「U-NEXT人気 12位」などの印をつけるために先に読む）
    rankings = {}
    for name, slug in VOD_LISTS.items():
        rankings[f"{name}人気"] = fetch_ranking(f"/list/vod/{slug}", VOD_PAGES, f"{name}人気")
    rankings["今話題"] = fetch_ranking("/list/trend", TREND_PAGES, "今話題")
    awards = {name: fetch_ranking(f"/list/award/{aid}", AWARD_PAGES, name) for name, aid in AWARDS.items()}

    def tags_of(mid):
        return {
            "ranks": [{"list": k, "rank": v[mid][0]} for k, v in rankings.items() if mid in v],
            "awards": [k for k, v in awards.items() if mid in v],
        }

    # 3. 観たい作品は作品ページから全部の配信サービスと「似ている作品」を取る
    movies, failed = [], []
    similar = {}  # 似ている作品ID -> 何本の観たい作品に挙がっているか
    for i, mid in enumerate(clip_ids, 1):
        time.sleep(WAIT)
        r = get(f"{BASE}/movies/{mid}")
        try:
            d = parse_movie(mid, r.text) if r is not None else None
        except Exception as e:
            print(f"  parse error {mid}: {e}")
            d = None
        if d is None:
            failed.append(mid)
            continue
        for sid in d.pop("similar_ids"):
            similar[sid] = similar.get(sid, 0) + 1
        d.update(source="clip", order=i, watched=mid in mark_ids, **tags_of(mid))
        if not public:  # 公開版はポスターを複製しない
            fetch_poster(d)
        movies.append(d)
        print(f"[観たい {i}/{len(clip_ids)}] {d['title']}  {d['runtime']}分  配信{len(d['vod'])}件")

    # 4. 観たい以外のおすすめ（観た作品・観たい作品は除く）
    skip = set(clip_ids) | mark_ids
    picks = {}
    for lst in [*rankings.values(), *awards.values()]:
        for mid, (_, d) in lst.items():
            if mid in skip:
                continue
            if mid not in picks:
                picks[mid] = d
            else:  # U-NEXT と Prime Video の両方に載っている作品は配信情報をまとめる
                names = {v["name"] for v in picks[mid]["vod"]}
                picks[mid]["vod"] += [v for v in d["vod"] if v["name"] not in names]
                picks[mid]["has_vod"] = picks[mid].get("has_vod") or d.get("has_vod")
    sim_top = [sid for sid, _ in sorted(similar.items(), key=lambda x: -x[1]) if sid not in skip][:SIMILAR_TOP]

    # 配信ランキングに出てこない作品は、どこで配信しているか一覧からはわからないので作品ページを読む。
    # ただし一覧で「配信の印」がない作品（映画館だけ・未配信）は読まない
    vod_ids = {mid for k, v in rankings.items() if k != "今話題" for mid in v}
    need = [mid for mid, d in picks.items() if mid not in vod_ids and d.get("has_vod")]
    need += [sid for sid in sim_top if sid not in picks]
    for j, mid in enumerate(need, 1):
        time.sleep(WAIT)
        r = get(f"{BASE}/movies/{mid}")
        try:
            if r is not None:
                d = parse_movie(mid, r.text)
                d.pop("similar_ids", None)
                picks[mid] = d
        except Exception as e:
            print(f"  parse error {mid}: {e}")
        if j % 25 == 0 or j == len(need):
            print(f"[おすすめの作品ページ {j}/{len(need)}]")
    for n, (mid, d) in enumerate(picks.items(), 1):
        d.pop("has_vod", None)
        d.update(source="pick", order=len(clip_ids) + n, watched=False,
                 similar=similar.get(mid, 0) if mid in sim_top else 0, **tags_of(mid))
        if not public:  # 公開版はポスターを複製しない
            fetch_poster(d)
        movies.append(d)
    for d in movies:
        d.setdefault("similar", 0)

    # 5. 観たい × 上映中の映画館と、よく行く映画館・Filmarksのイベント
    clips = [d for d in movies if d["source"] == "clip"]
    try:
        theater = fetch_schedules(clips)
    except Exception as e:
        print(f"  上映館の取得エラー: {e}")
        theater = {"origin": ORIGIN, "dates": [], "theaters": [], "shows": []}
    events = fetch_events(clips)

    data = {
        "user": USER_ID,
        "fetched_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "total": len(clip_ids),
        "marks": len(mark_ids),
        "lists": {k: len(v) for k, v in rankings.items()},
        "awards": list(AWARDS),
        "similar_top": len(sim_top),
        "theater": theater,
        "events": events,
        "failed": failed,
        "movies": movies,
    }
    (DATA / "clips.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(
        f"観たい {len(clip_ids) - len(failed)}/{len(clip_ids)}件（失敗 {failed}）、"
        f"おすすめ {len(picks)}件、観た {len(mark_ids)}件を除外、"
        f"{(time.time() - started) / 60:.1f}分"
    )
    build(public)


if __name__ == "__main__":
    main()
