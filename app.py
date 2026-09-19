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
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # только из Environment, без дефолта
CHANNEL_OPEN = -1003982891138       # @MBmybetting
CHANNEL_GREY = -1003720979095       # @MBmybetting2
API_URL = "https://zcodesystem.com/livebettingbot/get_sport_data.php"
SEEN_FILE = "seen.json"
CHECK_EVERY = 60

TG_API_ID = int(os.environ.get("API_ID", "0"))
TG_API_HASH = os.environ.get("API_HASH", "")
TG_SESSION_B64 = os.environ.get("SESSION_BASE64", "")
TG_SESSION_FILE = "mb_session.session"

TG_CHANNEL_ID_ABS = -1001978715517      # AsianBetSports

MB_COLOR_ZC = (180, 255, 100)           # салатовый
MB_COLOR_ABS = (100, 180, 255)          # голубой
# =======================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Добавь переменную окружения BOT_TOKEN на Render.")

app = Flask(__name__)


# ========== ZCODESYSTEM ==========

def get_data():
    payload = {"sport": "SOCCER", "lang": "en", "type": 0}
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10)",
        "X-Requested-With": "XMLHttpRequest"
    }
    r = requests.post(API_URL, data=payload, headers=headers, timeout=30)
    return r.json()


def parse_table(html, table_class):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_=table_class)
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []
    results = []
    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
        try:
            date_parts = cells[0].find_all("p")
            date = " ".join(p.get_text(strip=True) for p in date_parts)
            strong = cells[1].find("strong")
            league = strong.get_text(strip=True) if strong else ""
            if strong:
                strong.extract()
            match = cells[1].get_text(separator=" ", strip=True)
            score_raw = cells[3].get_text(strip=True) if len(cells) > 3 else ""
            score_short = score_raw.split("(")[0].strip() if "(" in score_raw else score_raw.strip()
            signal = cells[4].get_text(strip=True)
            odd = cells[6].get_text(strip=True)
            result_cell = row.find("td", class_="result")
            result_text = result_cell.get_text(strip=True) if result_cell else ""
            results.append({
                "league": league, "match": match, "date": date,
                "score": score_short, "signal": signal, "odd": odd,
                "result": result_text,
            })
        except Exception as e:
            print(f"  [ZC parse error] {e}", flush=True)
            continue
    return results


def make_zc_key(bet):
    raw = f"ZC|{bet['league']}|{bet['match']}|{bet['date']}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def check_zc_once(seen, first_run):
    try:
        data = get_data()
    except Exception as e:
        print(f"[ZC] Ошибка запроса: {e}", flush=True)
        return

    inner = data.get("data", {})
    all_bets = (
        parse_table(inner.get("poss_bets", ""), "poss_bets") +
        parse_table(inner.get("live_bets", ""), "livebets") +
        parse_table(inner.get("last_bets", ""), "lastbets")
    )

    total = len(all_bets)
    published_open = 0
    published_grey = 0
    filtered = {"result": 0, "duplicate": 0, "noscore": 0, "first_run": 0}

    print(f"[ZC] === Найдено прогнозов: {total} ===", flush=True)

    for bet in all_bets:
        if bet["result"]:
            r = bet["result"].strip().lower()
            if r.startswith(("win", "loss", "void", "push", "half win", "half loss")):
                filtered["result"] += 1
                continue

        key = make_zc_key(bet)
        if key in seen:
            filtered["duplicate"] += 1
            continue
        seen.add(key)

        signal_low = bet["signal"].lower()
        odd_low = bet["odd"].lower()
        is_grey = ("unlock" in signal_low) or ("unlock" in odd_low)

        if first_run:
            filtered["first_run"] += 1
            continue

        try:
            if is_grey:
                if not bet.get("score") or "unlock" in bet["score"].lower():
                    filtered["noscore"] += 1
                    continue
                img = make_card(bet, mode="grey", mb_color=MB_COLOR_ZC)
                caption = f"{bet['league']}\n{bet['match']}\nСчёт: {bet['score']}\nБУДЕТ ГОЛ"
                code, resp = send_to_telegram(img, caption, CHANNEL_GREY)
                print(f"  ✅ [ZC-GREY] {bet['match']} | {bet['score']} | TG: {code}", flush=True)
                published_grey += 1
            else:
                img = make_card(bet, mode="open", mb_color=MB_COLOR_ZC)
                caption = f"{bet['league']}\n{bet['match']}\n{bet['signal']} @ {bet['odd']}"
                code, resp = send_to_telegram(img, caption, CHANNEL_OPEN)
                print(f"  ✅ [ZC-OPEN] {bet['match']} | {bet['signal']} | TG: {code}", flush=True)
                published_open += 1
            time.sleep(1)
        except Exception as e:
            print(f"    [ZC] Ошибка публикации: {e}", flush=True)

    save_seen(seen)
    print(f"[ZC] === Итог: открытых {published_open} | серых {published_grey} | отсеяно result:{filtered['result']} дубликатов:{filtered['duplicate']} ===", flush=True)


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
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)


def find_font():
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        if os.path.exists(p):
            return p
    return None


def make_card(pred, mode="open", mb_color=(255, 200, 0), output="card.png"):
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
    league_t = pred.get("league", "")
    bbox = draw.textbbox((0, 0), league_t, font=f_league)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, y), league_t, fill=GREY, font=f_league)

    y += 110
    match_t = pred.get("match", "")
    bbox = draw.textbbox((0, 0), match_t, font=f_match)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, y), match_t, fill=WHITE, font=f_match)

    if mode == "grey":
        y += 130
        score_t = pred.get("score", "") or "0:0"
        bbox = draw.textbbox((0, 0), score_t, font=f_score)
        draw.text(((W - (bbox[2] - bbox[0])) // 2, y), score_t, fill=WHITE, font=f_score)

        y += 160
        msg = "БУДЕТ ГОЛ"
        bbox = draw.textbbox((0, 0), msg, font=f_odd)
        draw.text(((W - (bbox[2] - bbox[0])) // 2, y), msg, fill=ACCENT, font=f_odd)
    else:
        y += 190
        signal_t = pred.get("signal", "Total Over 0.5")
        bbox = draw.textbbox((0, 0), signal_t, font=f_signal)
        draw.text(((W - (bbox[2] - bbox[0])) // 2, y), signal_t, fill=WHITE, font=f_signal)

        y += 130
        odd_t = f"@{pred.get('odd', '')}"
        bbox = draw.textbbox((0, 0), odd_t, font=f_odd)
        draw.text(((W - (bbox[2] - bbox[0])) // 2, y), odd_t, fill=ACCENT, font=f_odd)

    img.save(output)
    return output


def send_to_telegram(image_path, caption, channel):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as f:
        files = {"photo": f}
        data = {"chat_id": channel, "caption": caption}
        r = requests.post(url, files=files, data=data, timeout=60)
    return r.status_code, r.text[:200]


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
                        img = make_card(data, mode="open", mb_color=MB_COLOR_ABS)
                        caption = f"{data['league']}\n{data['match']}\n{data['signal']}"
                        code1, _ = send_to_telegram(img, caption, CHANNEL_OPEN)
                        code2, _ = send_to_telegram(img, caption, CHANNEL_GREY)
                        print(f"  ✅ [ABS-CONFIRMED] {data['match']} | TG: {code1}/{code2}", flush=True)
                        published += 1
                    elif data["status"] == "ANNOUNCE":
                        img = make_card(data, mode="grey", mb_color=MB_COLOR_ABS)
                        caption = f"{data['league']}\n{data['match']}\nСчёт: {data['score']}\nБУДЕТ ГОЛ"
                        code, _ = send_to_telegram(img, caption, CHANNEL_GREY)
                        print(f"  ✅ [ABS-ANNOUNCE] {data['match']} | TG: {code}", flush=True)
                        published += 1
                    elif data["status"] == "WIN":
                        print(f"  [ABS-WIN] {data['match']} | {data['score']} — пропуск", flush=True)
                    elif data["status"] == "LOSS":
                        print(f"  [ABS-LOSS] {data['match']} — пропуск", flush=True)
                    time.sleep(1)
                except Exception as e:
                    print(f"    [ABS] Ошибка: {e}", flush=True)

            save_seen(seen)

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
