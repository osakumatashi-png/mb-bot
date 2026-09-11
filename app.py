import os
import json
import time
import hashlib
import threading

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from flask import Flask

# ====== НАСТРОЙКИ ======
BOT_TOKEN = "8392847779:AAGCkdGjL7iq2Zy5ZqPKPUJn8W0Qm0TF8Ks"
CHANNEL = "@MBmybetting"
API_URL = "https://zcodesystem.com/livebettingbot/get_sport_data.php"
SEEN_FILE = "seen.json"
CHECK_EVERY = 60
# =======================

app = Flask(__name__)


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

        # Пропускаем прогнозы, у которых уже есть результат (Win/Loss/Void)
        result_cell = row.find("td", class_="result")
        if result_cell:
            result_text = result_cell.get_text(strip=True).lower()
            if result_text in ("win", "loss", "void", "push", "half win", "half loss"):
                continue

        date_parts = cells[0].find_all("p")
        date = " ".join(p.get_text(strip=True) for p in date_parts)
        strong = cells[1].find("strong")
        league = strong.get_text(strip=True) if strong else ""
        if strong:
            strong.extract()
        match = cells[1].get_text(separator=" ", strip=True)
        signal = cells[4].get_text(strip=True)
        odd = cells[6].get_text(strip=True)
        results.append({
            "league": league, "match": match, "date": date,
            "signal": signal, "odd": odd
        })
    return results


def make_key(bet):
    raw = f"{bet['league']}|{bet['match']}|{bet['signal']}|{bet['date']}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


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


def make_card(pred, output="card.png"):
    W, H = 900, 900
    BG, ACCENT, WHITE, GREY = (15, 32, 55), (255, 200, 0), (255, 255, 255), (180, 190, 200)
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    fp = find_font()
    if fp:
        f_mb = ImageFont.truetype(fp, 200)
        f_league = ImageFont.truetype(fp, 40)
        f_match = ImageFont.truetype(fp, 52)
        f_signal = ImageFont.truetype(fp, 46)
        f_odd = ImageFont.truetype(fp, 64)
    else:
        f_mb = f_league = f_match = f_signal = f_odd = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), "MB", font=f_mb)
    draw.text(((W - (bbox[2] - bbox[0])) // 2 - bbox[0], 80), "MB", fill=ACCENT, font=f_mb)
    line_y = 80 + (bbox[3] - bbox[1]) + 80
    draw.line([(100, line_y), (W - 100, line_y)], fill=ACCENT, width=4)

    y = line_y + 60
    league_t = pred["league"]
    bbox = draw.textbbox((0, 0), league_t, font=f_league)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, y), league_t, fill=GREY, font=f_league)

    y += 110
    match_t = pred["match"]
    bbox = draw.textbbox((0, 0), match_t, font=f_match)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, y), match_t, fill=WHITE, font=f_match)

    y += 190
    signal_t = pred["signal"]
    bbox = draw.textbbox((0, 0), signal_t, font=f_signal)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, y), signal_t, fill=WHITE, font=f_signal)

    y += 130
    odd_t = f"@{pred['odd']}"
    bbox = draw.textbbox((0, 0), odd_t, font=f_odd)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, y), odd_t, fill=ACCENT, font=f_odd)

    img.save(output)
    return output


def send_to_telegram(image_path, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as f:
        files = {"photo": f}
        data = {"chat_id": CHANNEL, "caption": caption}
        r = requests.post(url, files=files, data=data, timeout=60)
    return r.status_code, r.text[:200]


def check_once(seen, first_run):
    data = get_data()
    inner = data.get("data", {})
    all_bets = (
        parse_table(inner.get("poss_bets", ""), "poss_bets") +
        parse_table(inner.get("live_bets", ""), "livebets") +
        parse_table(inner.get("last_bets", ""), "lastbets")
    )
    print(f"Найдено прогнозов: {len(all_bets)}", flush=True)
    published = 0
    for bet in all_bets:
        key = make_key(bet)
        if key in seen:
            continue
        seen.add(key)
        if first_run:
            print("  [калибровка] запомнил:", bet["match"], bet["signal"], flush=True)
            continue
        print("  публикую:", bet["league"], "|", bet["match"], "|", bet["signal"], "|", bet["odd"], flush=True)
        try:
            img_path = make_card(bet)
            caption = f"{bet['league']}\n{bet['match']}\n{bet['signal']} @ {bet['odd']}"
            code, resp = send_to_telegram(img_path, caption)
            print(f"    Telegram: {code} {resp}", flush=True)
            published += 1
            time.sleep(2)
        except Exception as e:
            print("    Ошибка публикации:", e, flush=True)
    save_seen(seen)
    return published


def worker():
    print("=== Бот запущен ===", flush=True)
    seen = load_seen()
    first_run = (len(seen) == 0)
    if first_run:
        print("Первый запуск: калибровка, публиковать не будем", flush=True)
    while True:
        try:
            pub = check_once(seen, first_run)
            first_run = False
            print(f"Опубликовано новых: {pub}. В памяти: {len(seen)}", flush=True)
        except Exception as e:
            print("Ошибка цикла:", e, flush=True)
        time.sleep(CHECK_EVERY)


threading.Thread(target=worker, daemon=True).start()


@app.route("/")
def home():
    return "MB bot is running"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
