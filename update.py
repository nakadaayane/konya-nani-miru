"""Filmarks から「観たい（Clip!）」映画と、観たい以外のおすすめ（配信の人気作・今話題の映画）を取得して、
「今夜なに観る？」ページのデータを作る。

使い方:
    python filmarks/update.py            # 全部取得 → data/clips.json → out/filmarks-clips.html + out/data.json
    python filmarks/update.py --build    # 取得せず、既存の clips.json から out/ だけ作り直す
    python filmarks/update.py --public   # 公開版（GitHub Pages）: site/index.html + site/data.json
    python filmarks/update.py --test 1231 121234   # 指定IDの作品ページだけ解析して表示

Filmarks の公開ページ（ログイン不要）を1秒間隔で読むだけなので、アカウントには触れない。
観た作品（Mark済み）はおすすめから外す。

公開版は誰でも見られるので、ポスターは複製せず Filmarks の画像を直接表示し、あらすじは冒頭だけ載せる。
配信の吹替の有無は U-NEXT（作品ページが裏で読むデータ）と JustWatch（Prime Video の音声言語）、
Xで話題のポストは Yahoo!リアルタイム検索から取る。
"""
import base64
import io
import json
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
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
SCHEDULE_MAX_DAYS = 14   # 映画館が公開している最終日まで取る（念のため最長2週間）
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
            if r.status_code == 429:
                ra = r.headers.get("Retry-After", "")
                time.sleep(min(int(ra) if ra.isdigit() else 30 * (i + 1), 120))
                continue
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
    last = (today + timedelta(days=SCHEDULE_MAX_DAYS - 1)).isoformat()
    targets = [d for d in clips if d.get("status") == "上映中" or d.get("theaters")
               or (d.get("status") == "公開予定" and d.get("release") and d["release"] <= last)]
    theaters, shows, seen, fetched = {}, [], set(), set()
    for n, d in enumerate(targets, 1):
        for pid, slug in PREFS.items():
            # まず今日の分を読み、返ってくる「スケジュールが出ている最終日」まで1日ずつ読む
            day, until = today, None
            while True:
                date = day.isoformat()
                time.sleep(WAIT)
                data = get_json(f"{BASE}/movies/{d['id']}/areas", {
                    "scheduleDate": date, "prefectureId": pid, "limit": 1000,
                    "latitude": ORIGIN["lat"], "longitude": ORIGIN["lng"]})
                fetched.add(date)
                if until is None:  # 公開前の作品で期間が返ってこないときは、公開日までは見ておく
                    to = ((data or {}).get("schedulePeriodTo") or "")[:10]
                    until = min(to or max(d.get("release") or date, date), last)
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
                        ends = [sc.get("releaseEndDate") for sc in t.get("screens") or [] if sc.get("releaseEndDate")]
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
                        show = {"movie": d["id"], "theater": t["id"], "date": date, "screens": screens}
                        if ends:
                            show["end"] = min(ends)[:10]
                        shows.append(show)
                day += timedelta(days=1)
                if day.isoformat() > until:
                    break
        print(f"[上映館 {n}/{len(targets)}] {d['title']}")
    return {"origin": ORIGIN, "dates": sorted({x["date"] for x in shows} or fetched),
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
        return None
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
        return None
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
    revival, articles = None, None
    r = get("https://revival.filmarks.com/")
    if r is not None:
        revival = []
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
    # FILMAGA は記事一覧のページが混み合うと断られやすいので、まず RSS を読む
    r = get("https://filmaga.filmarks.com/writers/premium-ticket/feed/", retries=2)
    if r is not None:
        try:
            feed = BeautifulSoup(r.content, "xml")
            articles = [{"title": txt(it.find("title")), "url": txt(it.find("link")),
                         "posted": datetime.strptime(txt(it.find("pubDate"))[:16], "%a, %d %b %Y").strftime("%Y.%m.%d")
                         if it.find("pubDate") else "",
                         "clip": match(txt(it.find("title")))} for it in feed.find_all("item")][:10]
        except Exception as e:
            print(f"  FILMAGAのRSSが読めませんでした: {e}")
            articles = None
    if not articles:
        time.sleep(WAIT)
        r = get("https://filmaga.filmarks.com/writers/premium-ticket/")
    else:
        r = None
    if r is not None:
        articles = []
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


def previous_data():
    """前回公開したデータ（取れなかった取得元は前回分で埋める）"""
    for f in (SITE / "data.json", OUT / "data.json"):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def attach_clips(events, clips):
    """イベントの題名が観たい作品と一致したら、その作品IDを付ける（観たいを読み終えてから）"""
    match = clip_matcher(clips)
    for e in events["theaters"] + events["filmarks"]["revival"] + events["filmarks"]["articles"]:
        e["clip"] = match(e.get("title"))
    return events


def fetch_events():
    """イベントは更新の最初に読む（Filmarks本体を続けて読んだあとだと、FILMAGAに断られやすいため）"""
    match = lambda title: None  # 観たいとの照合は attach_clips で
    prev = previous_data()
    prev_ev, prev_at = prev.get("events") or {}, prev.get("fetched_at")
    out = {"theaters": [], "filmarks": {"revival": [], "articles": []}, "stale": {},
           "fav": FAV_THEATERS, "official": {
               "109シネマズ二子玉川": "https://109cinemas.net/futakotamagawa/",
               "TOHOシネマズ 日比谷": "https://www.tohotheater.jp/event/",
               "TOHOシネマズ 大井町": "https://www.tohotheater.jp/event/"}}
    sources = [("109シネマズ二子玉川", lambda: fetch_109_events(match), ["109シネマズ二子玉川"]),
               ("TOHOシネマズ", lambda: fetch_toho_events(match), ["TOHOシネマズ 日比谷", "TOHOシネマズ 大井町"])]
    for label, f, names in sources:
        try:
            got = f()
        except Exception as e:  # 映画館のサイトが変わっても、ほかの取得は止めない
            print(f"  イベント取得エラー（{label}）: {e}")
            got = None
        if got is None:  # 取れなかったら前回分を出す
            got = [e for e in prev_ev.get("theaters", []) if e.get("theater") in names]
            for n in names:
                out["stale"][n] = (prev_ev.get("stale") or {}).get(n) or prev_at
            print(f"  {label}: 取得できなかったので前回分（{len(got)}件）を使います")
        out["theaters"] += got
        time.sleep(WAIT)
    try:
        fm = fetch_filmarks_events(match)
    except Exception as e:
        print(f"  Filmarksイベント取得エラー: {e}")
        fm = {"revival": None, "articles": None}
    for key in ("revival", "articles"):
        if fm[key] is None:
            fm[key] = (prev_ev.get("filmarks") or {}).get(key) or []
            out["stale"][key] = (prev_ev.get("stale") or {}).get(key) or prev_at
            print(f"  Filmarks {key}: 取得できなかったので前回分（{len(fm[key])}件）を使います")
    out["filmarks"] = fm
    print(f"イベント: 映画館 {len(out['theaters'])}件、リバイバル {len(out['filmarks']['revival'])}件、"
          f"FILMAGA {len(out['filmarks']['articles'])}件")
    return out


# ---------- 変わったこと（前回の公開データと比べる） ----------
CHANGE_DAYS = 3  # お知らせを残す日数


def compute_changes(prev, data):
    today = datetime.now(JST).date()
    t0 = today.isoformat()
    svod = lambda m: {v["name"] for v in m.get("vod", []) if any("見放題" in k for k in v.get("kinds", []))}
    clips = [m for m in data["movies"] if m["source"] == "clip"]
    prev_clips = {m["id"]: m for m in prev.get("movies", []) if m.get("source") == "clip"}
    items = []

    def add(key, kind, text, movie=None, url=None):
        items.append({"key": key, "kind": kind, "date": t0, "text": text, "movie": movie, "url": url})

    if prev_clips:
        for m in clips:
            old = prev_clips.get(m["id"])
            if old is None:
                add(f"clip-{m['id']}", "clip", f"『{m['title']}』を観たいに追加しました", m["id"], m["url"])
                continue
            for name in sorted(svod(m) - svod(old)):
                add(f"svod-{m['id']}-{name}", "svod", f"『{m['title']}』が{name}で見放題になりました", m["id"],
                    next((v["href"] for v in m["vod"] if v["name"] == name), m["url"]))
            if m.get("vod") and not old.get("vod") and not svod(m):
                add(f"vod-{m['id']}", "vod", f"『{m['title']}』の配信が始まりました（レンタル・購入）", m["id"], m["url"] + "/vod")
        prev_shown = {x["movie"] for x in (prev.get("theater") or {}).get("shows", [])}
        if prev.get("theater"):
            now_shown = {}
            for x in data["theater"]["shows"]:
                now_shown.setdefault(x["movie"], set()).add(x["theater"])
            names = {m["id"]: m for m in clips}
            for mid, ths in now_shown.items():
                if mid not in prev_shown and mid in names:
                    add(f"theater-{mid}", "theater", f"『{names[mid]['title']}』が近くの映画館{len(ths)}館で上映中になりました", mid)
        prev_urls = {e.get("url") for e in (prev.get("events") or {}).get("theaters", [])}
        for e in data["events"]["theaters"]:
            if e["url"] not in prev_urls and (e.get("clip") or re.search("舞台挨拶|登壇|トーク", e["title"])):
                add(f"event-{e['url']}", "event", f"{e['theater']}：{e['title']}", e.get("clip"), e["url"])
        prev_rev = {r.get("title") for r in ((prev.get("events") or {}).get("filmarks") or {}).get("revival", [])}
        for r in data["events"]["filmarks"]["revival"]:
            if r["title"] not in prev_rev:
                add(f"revival-{r['title']}", "event", f"Filmarksリバイバル上映（{r['section']}）：{r['title']}", r.get("clip"), r["url"])
    for m in clips:  # 公開が近い（3日以内）
        rel = m.get("release")
        if rel and t0 <= rel <= (today + timedelta(days=3)).isoformat():
            add(f"release-{m['id']}-{rel}", "release", f"『{m['title']}』は{int(rel[5:7])}月{int(rel[8:10])}日公開です", m["id"], m["url"])
    ending = {}
    for x in data["theater"]["shows"]:  # 近くの映画館での上映終了が近い（7日以内・映画館が終了日を出しているときだけ）
        if x.get("end") and x["end"] <= (today + timedelta(days=7)).isoformat():
            ending[x["movie"]] = min(ending.get(x["movie"], x["end"]), x["end"])
    titles = {m["id"]: m["title"] for m in clips}
    for mid, end in ending.items():
        if mid in titles:
            add(f"ending-{mid}-{end}", "ending", f"『{titles[mid]}』の近くでの上映は{int(end[5:7])}月{int(end[8:10])}日までの館があります", mid)

    keys = {i["key"] for i in items}
    keep_from = (today - timedelta(days=CHANGE_DAYS - 1)).isoformat()
    items += [i for i in prev.get("changes", []) if i.get("date", "") >= keep_from and i.get("key") not in keys]
    order = {"svod": 0, "event": 1, "theater": 2, "ending": 3, "release": 4, "vod": 5, "clip": 6}
    items.sort(key=lambda i: (i["date"], -order.get(i["kind"], 9)), reverse=True)
    return items


# ---------- 出力 ----------
# ---------- 配信の吹替（U-NEXT・Prime Video） ----------
CACHE = ROOT / "cache"  # リポジトリに入れて、毎日の更新で引き継ぐ（毎回は調べ直さない）
DUB_CACHE = CACHE / "dub.json"
DUB_RECHECK = {True: 60, False: 21, None: 14}  # 前回の結果ごとに、何日たったら調べ直すか
DUB_MAX = 300  # 1回の更新で調べる上限（初回のあとは、ほぼ新しく入った作品だけ）
UNEXT_API = "https://cc.unext.jp/"
UNEXT_TITLE_HASH = "0295df1eacb9e942a2c96cb4f1e5f47c3ac96f2bc50589d167e4708b6b701bbd"  # 作品ページが使う問い合わせ（cosmo_getVideoTitle）
JW_API = "https://apis.justwatch.com/graphql"
JW_QUERY = """query S($country: Country!, $language: Language!, $first: Int!, $filter: TitleFilter) {
  popularTitles(country: $country, first: $first, filter: $filter) { edges { node { objectType
    content(country: $country, language: $language) { title originalReleaseYear }
    offers(country: $country, platform: WEB) { audioLanguages package { clearName } } } } } }"""


def norm_title(x):
    return re.sub(r"[\s・･:：!！?？、,.。\-‐―—~〜&＆'’\"“”/／]", "", x or "").lower()


def unext_client():
    """U-NEXT のサイトが名乗っているクライアント名と版（版はサイトの更新で変わるので毎回読む）"""
    r = get("https://video.unext.jp/")
    m = r and re.search(r'src="([^"]*/pages/_app-[^"]+\.js)"', r.text)
    js = m and get(requests.compat.urljoin("https://video.unext.jp/", m.group(1)))
    v = js and re.search(r'clientAwareness:\{name:"([^"]+)",version:"([^"]+)"\}', js.text)
    return (v.group(1), v.group(2)) if v else None


def unext_dub(sid, client):
    """U-NEXT の作品の吹替の有無。True / False、取れなければ None"""
    try:
        r = session.get(UNEXT_API, timeout=30, params={
            "operationName": "cosmo_getVideoTitle",
            "variables": json.dumps({"code": sid}, separators=(",", ":")),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": UNEXT_TITLE_HASH}}, separators=(",", ":")),
        }, headers={"Origin": "https://video.unext.jp", "Referer": "https://video.unext.jp/", "Content-Type": "application/json",
                    "apollo-require-preflight": "true", "apollographql-client-name": client[0], "apollographql-client-version": client[1]})
        st = (r.json().get("data") or {}).get("webfront_title_stage")
        if st is None:
            print(f"  U-NEXT 取れず {sid}: {r.text[:120]}")
            return None
        return bool(st.get("hasDub") or st.get("hasDubTrack"))
    except (requests.RequestException, ValueError) as e:
        print(f"  U-NEXT error {sid}: {e}")
        return None


def prime_dub(d):
    """JustWatch の Prime Video（Amazon）の音声言語に日本語があるか。True / False、わからなければ None"""
    try:
        r = requests.post(JW_API, timeout=30, headers={"User-Agent": session.headers["User-Agent"]}, json={
            "query": JW_QUERY, "variables": {"country": "JP", "language": "ja", "first": 5, "filter": {"searchQuery": d["title"]}}})
        edges = r.json()["data"]["popularTitles"]["edges"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        print(f"  JustWatch error {d['title']}: {e}")
        return None
    want, y = norm_title(d["title"]), d.get("year")
    for e in edges:
        n = e["node"]
        c = n.get("content") or {}
        ny = c.get("originalReleaseYear")
        if n.get("objectType") != "MOVIE" or norm_title(c.get("title")) != want or (y and ny and abs(ny - y) > 1):
            continue
        langs = [o.get("audioLanguages") or [] for o in n.get("offers") or []
                 if "Amazon" in ((o.get("package") or {}).get("clearName") or "")]
        langs = [x for x in langs if x]  # 音声言語が空のものは「わからない」
        if not langs:
            return None
        return any("ja" in x for x in langs)
    return None


def fetch_dubs(movies):
    """日本以外の作品の U-NEXT・Prime Video の配信に「吹替があるか」をつける（vod の各項目に dub: True/False）"""
    CACHE.mkdir(exist_ok=True)
    try:
        cache = json.loads(DUB_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    today = datetime.now(JST).date()

    def stale(key):
        c = cache.get(key)
        try:
            return (today - date.fromisoformat(c["checked"])).days >= DUB_RECHECK[c.get("dub")]
        except (TypeError, KeyError, ValueError):
            return True

    def save():
        DUB_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")

    foreign = [d for d in movies if d.get("countries") and "日本" not in d["countries"]]
    jobs = []  # (キャッシュのキー, 作品, サービス名, U-NEXTの作品コード)
    for d in sorted(foreign, key=lambda d: (d["source"] != "clip", d["order"])):
        for v in d["vod"]:
            if v["name"] == "U-NEXT":
                m = re.search(r"SID\d+", v.get("href") or "")
                if m and stale(m.group(0)):
                    jobs.append((m.group(0), d, "U-NEXT", m.group(0)))
            elif v["name"] == "Prime Video" and stale(f"prime:{d['id']}"):
                jobs.append((f"prime:{d['id']}", d, "Prime Video", None))
    jobs = jobs[:DUB_MAX]
    client = unext_client() if any(j[2] == "U-NEXT" for j in jobs) else None
    if jobs and any(j[2] == "U-NEXT" for j in jobs) and not client:
        print("  U-NEXT のクライアント情報が読めないので、今回は U-NEXT の吹替を調べません")
    unext_fail = 0  # U-NEXT の仕組みが変わって読めないときに、何百回も試さないため
    for n, (key, d, svc, sid) in enumerate(jobs, 1):
        if svc == "U-NEXT" and (not client or unext_fail >= 8):
            continue
        time.sleep(WAIT)
        dub = unext_dub(sid, client) if svc == "U-NEXT" else prime_dub(d)
        if svc == "U-NEXT":
            unext_fail = unext_fail + 1 if dub is None else 0
            if unext_fail == 8:
                print("  U-NEXT が続けて読めないので、今回はここで U-NEXT をやめます")
        if dub is None and key in cache:  # 取れなかったときは前回の結果を残す
            continue
        cache[key] = {"dub": dub, "checked": today.isoformat()}
        if n % 50 == 0 or n == len(jobs):
            print(f"[吹替 {n}/{len(jobs)}]")
            save()
    save()
    found = 0
    for d in foreign:
        for v in d["vod"]:
            m = re.search(r"SID\d+", v.get("href") or "") if v["name"] == "U-NEXT" else None
            key = m.group(0) if m else f"prime:{d['id']}" if v["name"] == "Prime Video" else None
            c = cache.get(key) if key else None
            if c and c.get("dub") is not None:
                v["dub"] = c["dub"]
        found += any(v.get("dub") for v in d["vod"])
    print(f"配信で吹替あり {found}本／日本以外 {len(foreign)}本（今回調べた {len(jobs)}件）")


# ---------- Xで話題（Yahoo!リアルタイム検索） ----------
X_SEARCH = "https://search.yahoo.co.jp/realtime/search"
BUZZ_TOP = 16        # 何作品まで載せるか
BUZZ_SCAN = 40       # 今話題ランキングの上位何本を調べるか
BUZZ_MIN_LIKES = 30  # これより「いいね」が少ないポストは載せない
BUZZ_DAYS = 7        # 何日以内のポストか


def x_posts(query):
    try:
        r = session.get(X_SEARCH, params={"p": query, "md": "h"}, timeout=30, headers={"Accept-Language": "ja"})
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
        return json.loads(m.group(1))["props"]["pageProps"]["pageData"]["timeline"]["entry"] or []
    except Exception as e:
        print(f"  Xの検索エラー {query}: {e}")
        return []


def clean_post(x):
    x = (x or "").replace("\tSTART\t", "").replace("\tEND\t", "")
    x = re.sub(r"https?://t\.co/\S+", "", x)
    return re.sub(r"\s+", " ", x).strip()


def fetch_buzz(movies, trend):
    """観たい×上映中・公開間近の作品と、今話題の上位について、Xで反応の多いポストを拾い、話題の大きい順に並べる"""
    now = time.time()
    today = datetime.now(JST).date().isoformat()
    soon = (datetime.now(JST).date() + timedelta(days=14)).isoformat()
    by_id = {d["id"]: d for d in movies}
    cands = [d for d in movies if d["source"] == "clip" and (
        d.get("status") == "上映中" or d.get("theaters") or (d.get("release") and today <= d["release"] <= soon))]
    cands += [by_id[mid] for mid, _ in sorted(trend.items(), key=lambda x: x[1][0])[:BUZZ_SCAN] if mid in by_id]
    cands = list({d["id"]: d for d in cands}.values())
    items, seen = [], set()
    for n, d in enumerate(cands, 1):
        time.sleep(1.5)
        want = norm_title(d["title"])
        if len(want) < 2:
            continue
        posts = []
        for e in x_posts(f'"{d["title"]}"' + (" 映画" if len(want) <= 4 else "")):
            text = clean_post(e.get("displayTextBody") or e.get("displayText"))
            likes, rt = int(e.get("likesCount") or 0), int(e.get("rtCount") or 0)
            if (likes < BUZZ_MIN_LIKES or e.get("possiblySensitive") or e.get("inReplyTo")
                    or now - int(e.get("createdAt") or 0) > BUZZ_DAYS * 86400
                    or want not in norm_title(text) or "ネタバレ" in text or text.count("『") > 4
                    or not e.get("screenName") or e.get("id") in seen):
                continue
            seen.add(e["id"])
            posts.append({"id": e["id"], "user": e["screenName"], "name": e.get("name") or "",
                          "likes": likes, "rt": rt, "time": int(e.get("createdAt") or 0), "text": text[:240],
                          "url": f"https://x.com/{e['screenName']}/status/{e['id']}"})
        posts.sort(key=lambda x: -x["likes"])
        if posts:
            items.append({"movie": d["id"], "score": sum(x["likes"] + x["rt"] for x in posts[:5]), "posts": posts[:2]})
        if n % 10 == 0 or n == len(cands):
            print(f"[Xで話題 {n}/{len(cands)}] 話題あり {len(items)}本")
    items.sort(key=lambda x: -x["score"])
    return {"fetched_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"), "items": items[:BUZZ_TOP]}


def poster_data_uri(path):
    """一覧では幅64pxで表示するので、高精細画面向けに128px幅へ縮めて埋め込む"""
    img = Image.open(path).convert("RGB")
    img.thumbnail((128, 180))
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=72)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()


def excerpt(text, n=90):
    """あらすじの冒頭 n 字（公開版用）。続きは作品の Filmarks ページで読んでもらう"""
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[:n].rstrip("、。 ・") + "…"


def build(public=False):
    """非公開版: out/filmarks-clips.html ＋ out/data.json（ポスター埋め込み・あらすじあり）
    公開版:   site/index.html ＋ site/data.json（ポスターは Filmarks の画像を直接表示・あらすじは冒頭だけ）"""
    data = json.loads((DATA / "clips.json").read_text(encoding="utf-8"))
    data["update_note"] = NOTE_PUBLIC if public else NOTE_PRIVATE
    for d in data["movies"]:
        src = d.pop("poster_src", None)
        if public:
            d["poster"] = re.sub(r"/fitpad/\d+/\d+/", "/fitpad/300/420/", src) if src else None  # カード表示用に一回り大きく
            d["synopsis"] = excerpt(d.get("synopsis"))  # 誰でも見られるので、冒頭だけ（続きは Filmarks で）
        else:
            p = POSTERS / f"{d['id']}.webp"
            d["poster"] = poster_data_uri(p) if p.exists() else None
    dest, page = (SITE, "index.html") if public else (OUT, "filmarks-clips.html")
    dest.mkdir(exist_ok=True)
    (dest / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    shutil.copyfile(TEMPLATE, dest / page)
    if public:  # ホーム画面に追加したときのアイコンと設定
        for f in (ROOT / "assets").glob("*"):
            shutil.copyfile(f, dest / f.name)
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
        data["events"] = attach_clips(fetch_events(), clips)
        fetch_dubs(data["movies"])
        data["buzz"] = fetch_buzz(data["movies"], {d["id"]: (r["rank"], d) for d in data["movies"]
                                                   for r in d.get("ranks", []) if r["list"] == "今話題"})
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
    time.sleep(WAIT)
    events = fetch_events()

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
    attach_clips(events, clips)

    # 6. 配信の吹替（U-NEXT・Prime Video）と、Xで話題のポスト。取れなくても他は出す
    try:
        fetch_dubs(movies)
    except Exception as e:
        print(f"  吹替の取得エラー: {e}")
    try:
        buzz = fetch_buzz(movies, rankings["今話題"])
    except Exception as e:
        print(f"  Xの話題の取得エラー: {e}")
        buzz = {"items": []}
    if not buzz["items"]:  # 取れなかった日は前回分を残す
        buzz = previous_data().get("buzz") or buzz

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
        "buzz": buzz,
        "failed": failed,
        "movies": movies,
    }
    data["changes"] = compute_changes(previous_data(), data)
    print(f"変わったこと: {sum(1 for c in data['changes'] if c['date'] == data['fetched_at'][:10])}件（残しているもの {len(data['changes'])}件）")
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
