import os
import json
import time
import hashlib
import threading
import asyncio
import base64
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from flask import Flask
from telethon import TelegramClient

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_OPEN = -1003982891138       # @MBmybetting
CHANNEL_GREY = -1003720979095       # @MBmybetting2
API_URL = "https://zcodesystem.com/livebettingbot/get_sport_data.php"
SEEN_FILE = "seen.json"
CHECK_EVERY = 60

SEND_DELAY = 2
BET_DELAY = 2
LOOP_DELAY = 1

TG_API_ID = int(os.environ.get("API_ID", "0"))
TG_API_HASH = os.environ.get("API_HASH", "")
TG_SESSION_B64 = os.environ.get("SESSION_BASE64", "")
TG_SESSION_FILE = "mb_session.session"

TG_CHANNEL_ID_ABS = -1001978715517      # AsianBetSports

MB_COLOR_ZC = (180, 255, 100)
MB_COLOR_ABS = (100, 180, 255)
# =======================

app = Flask(__name__)


# ========== ОБЩЕЕ ==========

def decode_session():
    if not TG_SESSION_B64:
        print("[TG] SESSION_BASE64 не задан", flush=True)
        return False
    try:
        data = base64.b64decode(TG_SESSION_B64)
        with open(TG_SESSION_FILE, "wb") as f:
            f.write(data)
        print("[TG] Сессия декодирована", flush=True)
        return True
    except Exception as e:
        print(f"[TG] Ошибка декодирования: {e}", flush=True)
        return False


def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
                return set()
        except Exception as e:
            print(f"[SEEN] Ошибка чтения: {e}", flush=True)
            return set()
    return set()


def save_seen(seen):
    try:
        tmp = SEEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, ensure_ascii=False)
        os.replace(tmp, SEEN_FILE)
    except Exception as e:
        print(f"[SEEN] Ошибка записи: {e}", flush=True)


def find_font():
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        if os.path.exists(p):
            return p
    return None


def make_card(pred, mode="open", mb_color=(255, 200, 0), show_odd=True, output="card.png"):
    W, H = 900, 900
    BG, WHITE, GREY = (15, 32, 55), (255, 255, 255), (180, 190, 200)
    ACCENT = mb_color
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    fp = find_font()
    if fp:
        f_mb = ImageFont.truetype(fp, 200)
        f_league = ImageFont.truetype(fp, 40)
        f_match = ImageFont.truetype(fp, 52)
        f_signal = ImageFont.truetype(fp, 46)
        f_odd = ImageFont.truetype(fp, 64)
        f_score = ImageFont.truetype(fp, 90)
    else:
        f_mb = f_league = f_match = f_signal = f_odd = f_score = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), "MB", font=f_mb)
    draw.text(((W - (bbox[2] - bbox[0])) // 2 - bbox[0], 80), "MB", fill=ACCENT, font=f_mb)
    line_y = 80 + (bbox[3] - bbox[1]) + 80
    draw.line([(100, line_y), (W - 100, line_y)], fill=ACCENT, width=4)

    y = line_y + 60
    league_t = pred.get("league", "") or ""
    bbox = draw.textbbox((0, 0), league_t, font=f_league)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, y), league_t, fill=GREY, font=f_league)

    y += 110
    match_t = pred.get("match", "") or ""
    bbox = draw.textbbox((0, 0), match_t, font=f_match)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, y), match_t, fill=WHITE, font=f_match)

    if mode == "grey":
        y += 130
        score_t = pred.get("score", "") or "0:0"
        bbox = draw.textbbox((0, 0), score_t, font=f_score)
        draw.text(((W - (bbox[2] - bbox[0])) // 2, y), score_t, fill=WHITE, font=f_score)

        y += 160
        msg = "ОЖИДАЕТСЯ ГОЛ"
        bbox = draw.textbbox((0, 0), msg, font=f_odd)
        draw.text(((W - (bbox[2] - bbox[0])) // 2, y), msg, fill=ACCENT, font=f_odd)
    else:
        y += 160
        msg = "ОЖИДАЕТСЯ ГОЛ"
        bbox = draw.textbbox((0, 0), msg, font=f_signal)
        draw.text(((W - (bbox[2] - bbox[0])) // 2, y), msg, fill=WHITE, font=f_signal)

        if show_odd and pred.get("odd"):
            y += 130
            odd_t = f"@{pred.get('odd', '')}"
            bbox = draw.textbbox((0, 0), odd_t, font=f_odd)
            draw.text(((W - (bbox[2] - bbox[0])) // 2, y), odd_t, fill=ACCENT, font=f_odd)

    img.save(output)
    return output


def send_to_telegram(image_path, caption, channel):
    """
    Стандартный синтаксис Telegram Bot API:
      - файл фото -> files
      - chat_id и caption -> data (обычные form-поля)
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as f:
            files = {"photo": ("card.png", f, "image/png")}
            data = {"chat_id": str(channel), "caption": caption}
            r = requests.post(url, files=files, data=data, timeout=60)
        return r.status_code, r.text[:200]
    except Exception as e:
        return 0, str(e)[:200]


# ========== ZCODESYSTEM ==========

def get_data(bet_type=0):
    payload = {"sport": "SOCCER", "lang": "en", "type": bet_type}
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10)",
        "X-Requested-With": "XMLHttpRequest"
    }
    r = requests.post(API_URL, data=payload, headers=headers, timeout=30)
    return r.json()


def clean_text(td):
    if not td:
        return ""
    txt = td.get_text(" ", strip=True)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def parse_signal_text(bet_text):
    """
    'Total Over 1.5 Goals' -> ('over', 2)
    'Over 0.5'             -> ('over', 1)
    'Total Under 2.5 Goals' -> ('under', 2)
    """
    if not bet_text:
        return None, None
    t = bet_text.strip().lower()
    m = re.search(r"(?:total\s+)?(over|under)\s+(\d+(?:\.\d+)?)", t)
    if not m:
        return None, None
    side = m.group(1)
    line = float(m.group(2))
    threshold = int(line + 0.5)
    return side, threshold


def parse_rows(html, table_class):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_=table_class)
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []

    rows = []
    for tr in tbody.find_all("tr"):
        try:
            classes = tr.get("class", []) or []
            data_id = tr.get("data-id", "") or ""
            data_code = tr.get("data-code", "") or ""

            match_id = data_id
            if not match_id:
                for c in classes:
                    if c.startswith("g") and c[1:].isdigit():
                        match_id = c
                        break

            # Пропускаем hot-тренды (нет td.game)
            if "hot" in classes and not tr.find("td", class_="game"):
                continue

            date_td = tr.find("td", class_="date")
            game_td = tr.find("td", class_="game")
            score_td = tr.find("td", class_="score")
            bet_td = tr.find("td", class_="bet")
            odd_td = tr.find("td", class_="odd")

            date_txt = clean_text(date_td)
            league = ""
            match = ""
            if game_td:
                strong = game_td.find("strong")
                if strong:
                    league = strong.get_text(" ", strip=True)
                match = clean_text(game_td)
                if league and match.startswith(league):
                    match = match[len(league):].strip()

            score = clean_text(score_td)
            bet = clean_text(bet_td)
            odd = clean_text(odd_td)

            if not match and not match_id:
                continue

            rows.append({
                "match_id": match_id,
                "data_code": data_code,
                "date": date_txt,
                "league": league,
                "match": match,
                "score": score,
                "bet": bet,
                "odd": odd,
            })
        except Exception as e:
            print(f"  [ZC parse row error] {e}", flush=True)
            continue
    return rows


def make_zc_key(row):
    base = row.get("match_id") or row.get("match") or ""
    raw = f"ZC|{base}|{row.get('bet','')}|{row.get('data_code','')}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def total_goals(score_str):
    try:
        main = score_str.split("(")[0].strip()
        parts = main.split(":")
        if len(parts) != 2:
            return 0
        return int(parts[0].strip()) + int(parts[1].strip())
    except Exception:
        return 0


def check_zc_once(seen, first_run):
    all_rows = []
    for bet_type in (0, 1, 2):
        try:
            data = get_data(bet_type=bet_type)
        except Exception as e:
            print(f"[ZC] type={bet_type} ошибка запроса: {e}", flush=True)
            continue

        inner = data.get("data", {}) if isinstance(data, dict) else {}
        poss = inner.get("poss_bets", "") or ""
        live = inner.get("live_bets", "") or ""

        if live:
            print(f"[ZC] type={bet_type} live_bets len={len(live)}", flush=True)

        for r in parse_rows(poss, "poss_bets"):
            r["_table"] = "poss"
            all_rows.append(r)
        for r in parse_rows(live, "livebets"):
            r["_table"] = "live"
            all_rows.append(r)

    unique = {}
    for r in all_rows:
        k = make_zc_key(r)
        if k not in unique:
            unique[k] = r
    all_rows = list(unique.values())

    published_open = 0
    published_grey = 0
    filtered = {"poss": 0, "dup": 0, "goals": 0, "first_run": 0, "no_bet": 0, "unlock": 0}

    print(f"[ZC] === Всего строк: {len(all_rows)} ===", flush=True)

    for row in all_rows:
        if row["_table"] == "poss":
            filtered["poss"] += 1
            continue

        bet_text = row.get("bet", "") or ""
        bet_low = bet_text.strip().lower()

        if "unlock" in bet_low or "unconfirmed" in bet_low:
            filtered["unlock"] += 1
            continue

        side, threshold = parse_signal_text(bet_text)
        if side is None:
            filtered["no_bet"] += 1
            print(f"    [ZC nobet] bet='{bet_text}' | {row.get('match','')}", flush=True)
            continue

        is_under = (side == "under")

        goals = total_goals(row.get("score", ""))
        if threshold is not None and goals >= threshold:
            filtered["goals"] += 1
            continue

        key = make_zc_key(row)
        if key in seen:
            filtered["dup"] += 1
            continue
        seen.add(key)

        if first_run:
            filtered["first_run"] += 1
            continue

        try:
            if is_under:
                img = make_card(row, mode="open", mb_color=MB_COLOR_ZC, show_odd=True)
                caption = f"{row['league']}\n{row['match']}\nОЖИДАЕТСЯ ГОЛ"
                code_send, _ = send_to_telegram(img, caption, CHANNEL_GREY)
                print(f"  ✅ [ZC-GREY Under] {row['match']} | TG: {code_send}", flush=True)
                published_grey += 1
            else:
                img1 = make_card(row, mode="open", mb_color=MB_COLOR_ZC, show_odd=False)
                caption1 = f"{row['league']}\n{row['match']}\nОЖИДАЕТСЯ ГОЛ"
                code1, _ = send_to_telegram(img1, caption1, CHANNEL_OPEN)
                print(f"  [ZC-OPEN] {row['match']} | {bet_text} | MB1: {code1}", flush=True)
                time.sleep(SEND_DELAY)

                img2 = make_card(row, mode="open", mb_color=MB_COLOR_ZC, show_odd=True)
                caption2 = f"{row['league']}\n{row['match']}\nОЖИДАЕТСЯ ГОЛ @ {row.get('odd','')}"
                code2, _ = send_to_telegram(img2, caption2, CHANNEL_GREY)
                print(f"  ✅ [ZC-OPEN] {row['match']} | {bet_text} | MB2: {code2}", flush=True)
                published_open += 1

            time.sleep(BET_DELAY)
        except Exception as e:
            print(f"    [ZC] Ошибка публикации: {e}", flush=True)

    save_seen(seen)
    print(
        f"[ZC] === Итог: open {published_open} | grey {published_grey} | "
        f"poss:{filtered['poss']} nogoals:{filtered['goals']} dup:{filtered['dup']} "
        f"unlock:{filtered['unlock']} nobet:{filtered['no_bet']} first:{filtered['first_run']} ===",
        flush=True
    )


# ========== ASIANBETSPORTS ==========

def parse_abs_message(text):
    if not text:
        return None
    result = {"status": None, "league": "", "match": "", "score": "", "signal": "Total Over 0.5", "strength": ""}

    score_match = re.search(r"(\d+-\d+)", text)
    if score_match:
        result["score"] = score_match.group(1)

    if "✅" in text and result["score"] and result["score"] != "0-0":
        result["status"] = "WIN"
    elif "❌" in text:
        result["status"] = "LOSS"
    elif "🔴" in text:
        result["status"] = "CONFIRMED"
    elif "🔵" in text:
        result["status"] = "ANNOUNCE"
    elif "⚠️" in text:
        result["status"] = "WARNING"

    if not result["status"]:
        return None

    for emoji, name in [("🔥", "strong"), ("💰", "confirmed"),
                         ("⭐", "preliminary"), ("💣", "medium"), ("🚀", "super")]:
        if emoji in text:
            result["strength"] = name
            break

    league_match = re.search(r"🏆\s*(.+)", text)
    if league_match:
        result["league"] = league_match.group(1).strip()

    match_match = re.search(r"⚽\s*(.+)", text)
    if match_match:
        result["match"] = match_match.group(1).strip()

    return result


def make_abs_key(data):
    raw = f"ABS|{data['match']}|{data['league']}|{data['status']}|{data['score']}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def check_abs_once(seen, first_run, abs_last_id):
    published = 0

    async def read():
        nonlocal published, abs_last_id
        try:
            async with TelegramClient(TG_SESSION_FILE, TG_API_ID, TG_API_HASH) as client:
                print("[ABS] Подключение...", flush=True)
                messages = []
                async for message in client.iter_messages(TG_CHANNEL_ID_ABS, limit=30):
                    messages.append(message)
                messages.reverse()

                for message in messages:
                    if message.id <= abs_last_id:
                        continue
                    abs_last_id = message.id
                    text = message.text or ""
                    data = parse_abs_message(text)
                    if not data:
                        continue
                    key = make_abs_key(data)
                    if key in seen:
                        continue
                    seen.add(key)

                    if first_run:
                        try:
                            age_min = (datetime.now(timezone.utc) - message.date).total_seconds() / 60
                        except Exception:
                            age_min = 999
                        if age_min < 10:
                            print(f"  [ABS-свежее] {data['status']} | {data['match']}", flush=True)
                        else:
                            print(f"  [ABS-калибровка] {data['status']} | {data['match']}", flush=True)
                            continue

                    try:
                        if data["status"] == "CONFIRMED":
                            img1 = make_card(data, mode="open", mb_color=MB_COLOR_ABS, show_odd=False)
                            caption1 = f"{data['league']}\n{data['match']}\nОЖИДАЕТСЯ ГОЛ"
                            code1, _ = send_to_telegram(img1, caption1, CHANNEL_OPEN)
                            print(f"  [ABS-CONFIRMED] {data['match']} | MB1: {code1}", flush=True)
                            time.sleep(SEND_DELAY)

                            img2 = make_card(data, mode="open", mb_color=MB_COLOR_ABS, show_odd=True)
                            caption2 = f"{data['league']}\n{data['match']}\nОЖИДАЕТСЯ ГОЛ"
                            code2, _ = send_to_telegram(img2, caption2, CHANNEL_GREY)
                            print(f"  ✅ [ABS-CONFIRMED] {data['match']} | MB2: {code2}", flush=True)
                            published += 1
                        elif data["status"] == "ANNOUNCE":
                            img = make_card(data, mode="grey", mb_color=MB_COLOR_ABS, show_odd=True)
                            caption = f"{data['league']}\n{data['match']}\nСчёт: {data['score']}\nОЖИДАЕТСЯ ГОЛ"
                            code, _ = send_to_telegram(img, caption, CHANNEL_GREY)
                            print(f"  ✅ [ABS-ANNOUNCE] {data['match']} | TG: {code}", flush=True)
                            published += 1
                        elif data["status"] == "WIN":
                            print(f"  [ABS-WIN] {data['match']} | {data['score']} — пропуск", flush=True)
                        elif data["status"] == "LOSS":
                            print(f"  [ABS-LOSS] {data['match']} — пропуск", flush=True)
                        time.sleep(BET_DELAY)
                    except Exception as e:
                        print(f"    [ABS] Ошибка: {e}", flush=True)

                save_seen(seen)
        except Exception as e:
            print(f"[ABS] Ошибка Telethon: {e}", flush=True)

    try:
        asyncio.run(read())
    except Exception as e:
        print(f"[ABS] Ошибка цикла: {e}", flush=True)

    return published


# ========== ГЛАВНЫЙ ЦИКЛ ==========

def worker():
    print("=== Бот запущен ===", flush=True)
    decode_session()

    seen = load_seen()
    first_run = (len(seen) == 0)
    abs_last_id = 0

    if first_run:
        print("Первый запуск: калибровка", flush=True)

    while True:
        try:
            check_zc_once(seen, first_run)
            time.sleep(LOOP_DELAY)

            if TG_API_ID and TG_API_HASH and TG_SESSION_B64:
                check_abs_once(seen, first_run, abs_last_id)
            else:
                print("[TG] Пропуск — нет ключей", flush=True)

            first_run = False
        except Exception as e:
            print("Ошибка главного цикла:", e, flush=True)

        time.sleep(CHECK_EVERY)


threading.Thread(target=worker, daemon=True).start()


@app.route("/")
def home():
    return "MB bot is running"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
