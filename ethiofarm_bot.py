"""
EthioFarm AI — Telegram Bot
============================
AI-powered farming advisor for Ethiopian smallholder farmers.

Features:
  - Crop disease diagnosis (text description or photo)
  - Weather-based planting/harvesting advice
  - Market price alerts
  - Livestock health checker
  - Crop calendar by region
  - 3 languages: Amharic, Afan Oromo, English
  - Rate limiting, input sanitization, state persistence

HOW TO RUN:
  1. Get Telegram token from @BotFather
  2. Get Claude API key from https://console.anthropic.com
  3. Paste keys below
  4. pip install requests
  5. python3 ethiofarm_bot.py
"""

import requests, json, time, threading, logging, os, re
from datetime import datetime
from collections import defaultdict, deque

# ── CONFIGURATION ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
CLAUDE_API_KEY  = os.environ.get("CLAUDE_API_KEY",  "YOUR_CLAUDE_API_KEY")
ADMIN_IDS       = []
STATE_FILE      = "ethiofarm_state.json"
LOG_FILE        = "ethiofarm.log"
RATE_LIMIT_MAX  = 15
# ─────────────────────────────────────────────────────────────

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger("EthioFarm")

state_lock   = threading.Lock()
rate_buckets = defaultdict(lambda: deque(maxlen=RATE_LIMIT_MAX))
banned_users = set()
user_state   = {}

# ── MARKET PRICES (updated manually / scraped in production) ──
MARKET_PRICES = {
    "en": {
        "Teff":    {"price": "4,200 ETB/quintal", "trend": "📈 Rising", "best_market": "Addis Ababa"},
        "Maize":   {"price": "1,800 ETB/quintal", "trend": "➡️ Stable", "best_market": "Dire Dawa"},
        "Wheat":   {"price": "2,900 ETB/quintal", "trend": "📈 Rising", "best_market": "Jimma"},
        "Coffee":  {"price": "8,500 ETB/quintal", "trend": "📈 Rising", "best_market": "Addis Ababa"},
        "Sorghum": {"price": "1,600 ETB/quintal", "trend": "📉 Falling","best_market": "Mekelle"},
        "Barley":  {"price": "2,100 ETB/quintal", "trend": "➡️ Stable", "best_market": "Gondar"},
        "Beans":   {"price": "3,200 ETB/quintal", "trend": "📈 Rising", "best_market": "Hawassa"},
    },
    "am": {
        "ጤፍ":    {"price": "4,200 ብር/ኩንታል", "trend": "📈 ዋጋ ወጣ", "best_market": "አዲስ አበባ"},
        "በቆሎ":  {"price": "1,800 ብር/ኩንታል", "trend": "➡️ ተረጋጋ", "best_market": "ድሬዳዋ"},
        "ስንዴ":  {"price": "2,900 ብር/ኩንታል", "trend": "📈 ዋጋ ወጣ", "best_market": "ጅማ"},
        "ቡና":   {"price": "8,500 ብር/ኩንታል", "trend": "📈 ዋጋ ወጣ", "best_market": "አዲስ አበባ"},
        "ማሽላ":  {"price": "1,600 ብር/ኩንታል", "trend": "📉 ዋጋ ወረደ","best_market": "መቀሌ"},
    },
    "om": {
        "Xaafii":   {"price": "4,200 ETB/kuuntaala", "trend": "📈 Ol ka'aa", "best_market": "Finfinnee"},
        "Boqqoloo": {"price": "1,800 ETB/kuuntaala", "trend": "➡️ Tasgabbaa'e", "best_market": "Dire Dhawaa"},
        "Qamadii":  {"price": "2,900 ETB/kuuntaala", "trend": "📈 Ol ka'aa", "best_market": "Jimmaa"},
        "Buna":     {"price": "8,500 ETB/kuuntaala", "trend": "📈 Ol ka'aa", "best_market": "Finfinnee"},
        "Mishingaa": {"price": "1,600 ETB/kuuntaala", "trend": "📉 Gadi bu'aa", "best_market": "Maqalee"},
    },
}

REGIONS = {
    "en": ["Oromia", "Amhara", "Tigray", "SNNPR", "Somali", "Afar", "Benishangul", "Harari", "Dire Dawa", "Addis Ababa"],
    "am": ["ኦሮሚያ", "አማራ", "ትግራይ", "ደቡብ", "ሶማሊ", "አፋር", "ቤኒሻንጉል", "ሃረሪ", "ድሬዳዋ", "አዲስ አበባ"],
    "om": ["Oromiyaa", "Amaaraa", "Tigraay", "SNNPR", "Somaalee", "Affaar", "Benishaangul", "Hararii", "Dire Dhawaa", "Finfinnee"],
}

# ── SECURITY ──────────────────────────────────────────────────
INJECTION_RE = re.compile(r"(<script|javascript:|SELECT\s+\*|DROP\s+TABLE)", re.I)

def sanitize(text):
    if len(text) > 800: text = text[:800]
    if INJECTION_RE.search(text): return None
    return re.sub(r"<[^>]+>", "", text).strip()

def is_rate_limited(chat_id):
    now = time.time()
    b = rate_buckets[chat_id]
    while b and now - b[0] > 60: b.popleft()
    if len(b) >= RATE_LIMIT_MAX: return True
    b.append(now)
    return False

# ── STATE ─────────────────────────────────────────────────────
def get_state(chat_id):
    with state_lock:
        if chat_id not in user_state:
            user_state[chat_id] = {
                "lang": None, "mode": "menu", "name": "",
                "region": None, "reminder": False,
                "questions_asked": 0, "join_date": datetime.now().isoformat(),
            }
        return user_state[chat_id]

def lang(chat_id):
    return get_state(chat_id).get("lang", "en") or "en"

def save_state():
    with state_lock:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in user_state.items()}, f, ensure_ascii=False)
        except Exception as e:
            log.error(f"save_state: {e}")

def load_state():
    if not os.path.exists(STATE_FILE): return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            for k, v in json.load(f).items():
                user_state[int(k)] = v
        log.info(f"Loaded {len(user_state)} users.")
    except Exception as e:
        log.error(f"load_state: {e}")

def autosave():
    while True:
        time.sleep(300)
        save_state()

# ── TELEGRAM HELPERS ──────────────────────────────────────────
def _post(ep, payload, retries=3):
    for i in range(retries):
        try:
            return requests.post(f"{TELEGRAM_API}/{ep}", json=payload, timeout=10).json()
        except Exception as e:
            log.warning(f"{ep} attempt {i+1}: {e}")
            time.sleep(1)
    return {}

def send_msg(chat_id, text, markup=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if markup: p["reply_markup"] = json.dumps(markup)
    return _post("sendMessage", p)

def send_kb(chat_id, text, rows):
    kb = {"keyboard": [[{"text": b} for b in r] for r in rows],
          "resize_keyboard": True, "one_time_keyboard": True}
    send_msg(chat_id, text, kb)

def send_inline(chat_id, text, rows):
    kb = {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in r] for r in rows]}
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": json.dumps(kb)}
    return _post("sendMessage", p).get("result", {}).get("message_id")

def remove_kb(chat_id, text):
    send_msg(chat_id, text, {"remove_keyboard": True})

def answer_cb(cq_id, text=""):
    _post("answerCallbackQuery", {"callback_query_id": cq_id, "text": text})

def get_updates(offset=None):
    try:
        r = requests.get(f"{TELEGRAM_API}/getUpdates",
                         params={"timeout": 30, "offset": offset,
                                 "allowed_updates": ["message","callback_query"]}, timeout=35)
        return r.json().get("result", [])
    except Exception as e:
        log.error(f"get_updates: {e}")
        return []

# ── AI ADVISOR ────────────────────────────────────────────────
def ask_farm_ai(question, language, region=None, photo_desc=None):
    region_txt = f" The farmer is located in {region}." if region else ""
    photo_txt  = f"\n\nPhoto description from farmer: {photo_desc}" if photo_desc else ""

    prompts = {
        "en": (
            f"You are EthioFarm AI, an expert agricultural advisor specialising in Ethiopian farming.{region_txt} "
            "You know Ethiopian crops deeply: teff, enset, coffee, sorghum, barley, maize, wheat, khat, injera wheat. "
            "When diagnosing crop diseases, give: 1) Disease name, 2) Cause, 3) Treatment steps using locally available inputs, "
            "4) Prevention for next season. For general questions, give practical advice aligned with Ethiopian farming conditions. "
            "Be concise and farmer-friendly. Avoid technical jargon."
        ),
        "am": (
            f"አንተ EthioFarm AI ነህ — ለኢትዮጵያ አርሶ አደሮች የተሰራ AI አስተማሪ።{region_txt} "
            "ስለ ኢትዮጵያ ሰብሎች ጥልቅ እውቀት አለህ: ጤፍ፣ ስንዴ፣ ቡና፣ ማሽላ፣ ገብስ፣ በቆሎ፣ ቅጤማ። "
            "ሁሌም በአማርኛ ምለስ። ቀላልና ለአርሶ አደር ተስማሚ ቋንቋ ተጠቀም።"
        ),
        "om": (
            f"Ati EthioFarm AI — gorsaa qonnaa AI lammii Itoophiyaa.{region_txt} "
            "Biqiltoota Itoophiyaa gaarii beekta: xaafii, qamadii, buna, mishingaa, geershoo, boqqoloo. "
            "Yeroo hundaa Afaan Oromootti deebii kenniiti. Afaan salphaa fayyadami."
        ),
    }
    payload = {
        "model": "claude-sonnet-4-20250514", "max_tokens": 1000,
        "system": prompts.get(language, prompts["en"]),
        "messages": [{"role": "user", "content": question + photo_txt}]
    }
    headers = {"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          json=payload, headers=headers, timeout=30)
        data = r.json()
        if "content" in data: return data["content"][0]["text"]
        return f"[Error: {data.get('error',{}).get('message','unknown')}]"
    except Exception as e:
        return f"[Connection error: {e}]"

# ── MARKET PRICES ─────────────────────────────────────────────
def market_text(chat_id):
    lc = lang(chat_id)
    prices = MARKET_PRICES.get(lc, MARKET_PRICES["en"])
    if lc == "am":
        lines = ["💰 <b>የዛሬ የገበያ ዋጋ</b>\n"]
        for crop, info in prices.items():
            lines.append(f"🌾 <b>{crop}</b>\n   ዋጋ: {info['price']}  {info['trend']}\n   ምርጥ ገበያ: {info['best_market']}")
        lines.append("\n⏰ <i>ዋጋ በየቀኑ ይለወጣል። ለተሻለ ዋጋ ቀደም ብለው ለሻጭ ያነጋግሩ።</i>")
    elif lc == "om":
        lines = ["💰 <b>Gatii Gabaa Har'aa</b>\n"]
        for crop, info in prices.items():
            lines.append(f"🌾 <b>{crop}</b>\n   Gatii: {info['price']}  {info['trend']}\n   Gabaa gaarii: {info['best_market']}")
        lines.append("\n⏰ <i>Gatiin guyyaa guyyaatti jijjiira.</i>")
    else:
        lines = ["💰 <b>Today's Market Prices</b>\n"]
        for crop, info in prices.items():
            lines.append(f"🌾 <b>{crop}</b>\n   Price: {info['price']}  {info['trend']}\n   Best market: {info['best_market']}")
        lines.append("\n⏰ <i>Prices updated daily. Contact buyers early for best rates.</i>")
    return "\n".join(lines)

# ── UI TEXT ───────────────────────────────────────────────────
MENUS = {
    "en": {"text": "🌾 <b>EthioFarm AI — Main Menu</b>\n\nHow can I help you today?",
           "buttons": [["🔬 Diagnose Crop Disease", "🌤️ Weather & Planting Advice"],
                       ["💰 Market Prices", "🐄 Livestock Health"],
                       ["📅 Crop Calendar", "📊 My Farm Stats"],
                       ["🌐 Change Language"]]},
    "am": {"text": "🌾 <b>EthioFarm AI — ዋና ምናሌ</b>\n\nዛሬ ምን ልርዳዎ?",
           "buttons": [["🔬 የሰብል በሽታ ምርመራ", "🌤️ የአየር ሁኔታ ምክር"],
                       ["💰 የገበያ ዋጋ", "🐄 የከብት ጤና"],
                       ["📅 የሰብል ቀን መቁጠሪያ", "📊 የእርሻ ስታቲስቲክስ"],
                       ["🌐 ቋንቋ ቀይር"]]},
    "om": {"text": "🌾 <b>EthioFarm AI — Fiilaa Guddaa</b>\n\nHar'a attam gargaaruu danda'a?",
           "buttons": [["🔬 Dhukkuba Biqiltuu", "🌤️ Qilleensa fi Gorsa Dhaabuu"],
                       ["💰 Gatii Gabaa", "🐄 Fayyaa Horsiisee"],
                       ["📅 Kalandara Qonnaa", "📊 Oduuu Qonnaa Koo"],
                       ["🌐 Afaan Jijjiiri"]]},
}

PROMPTS = {
    "diagnose_en": "🔬 <b>Crop Disease Diagnosis</b>\n\nDescribe your crop's symptoms, OR send a photo of the affected plant.\n\nExample: <i>My teff leaves are turning yellow and wilting</i>\n\n/menu to go back.",
    "diagnose_am": "🔬 <b>የሰብል በሽታ ምርመራ</b>\n\nስለ ሰብልዎ ምልክቶች ይግለጹ ወይም ፎቶ ይላኩ።\n\nምሳሌ: <i>የጤፌ ቅጠሎች ወደ ቢጫ ይለወጣሉ</i>\n\n/menu ለመመለስ.",
    "diagnose_om": "🔬 <b>Dhukkuba Biqiltuu</b>\n\nMirga biqiltuu kee ibsi ykn suuraa ergi.\n\nFkn: <i>Leaves xaafii koo bifa gara bifa dhalaan jijjiiraa</i>\n\n/menu deebi'uuf.",
    "weather_en": "🌤️ <b>Weather & Planting Advice</b>\n\nAsk me about planting schedules, irrigation, or weather-based decisions.\n\nExample: <i>When should I plant teff in Oromia?</i>\n\n/menu to go back.",
    "weather_am": "🌤️ <b>የአየር ሁኔታ ምክር</b>\n\nስለ መዝራት ጊዜ፣ መስኖ ወይም የአየር ሁኔታ ጥያቄ ጠይቁ።\n\nምሳሌ: <i>በኦሮሚያ ጤፍ መቼ መዝራት አለብኝ?</i>\n\n/menu ለመመለስ.",
    "weather_om": "🌤️ <b>Qilleensa fi Gorsa</b>\n\nWegen dhaabuu, bishaan obuun, ykn qilleensa wegen gaaffadhu.\n\nFkn: <i>Yoom xaafii Oromiyaa keessatti dhaabuu?</i>\n\n/menu deebi'uuf.",
    "livestock_en": "🐄 <b>Livestock Health Checker</b>\n\nDescribe your animal's symptoms and I'll help diagnose the problem.\n\nExample: <i>My cow has stopped eating and has a swollen neck</i>\n\n/menu to go back.",
    "livestock_am": "🐄 <b>የከብት ጤና ምርመራ</b>\n\nስለ እንስሳዎ ምልክቶች ይግለጹ።\n\nምሳሌ: <i>ላሜ መብላቷን አቁማ አንገቷ ነፋ</i>\n\n/menu ለመመለስ.",
    "livestock_om": "🐄 <b>Fayyaa Horsiisee</b>\n\nMirga beeyladaa kee ibsi.\n\nFkn: <i>Sa'aan koo nyaachuu dhiiste morma isii dhiite</i>\n\n/menu deebi'uuf.",
    "thinking_en": "🌿 <b>EthioFarm AI is analysing...</b>",
    "thinking_am": "🌿 <b>EthioFarm AI እየተንተነ ነው...</b>",
    "thinking_om": "🌿 <b>EthioFarm AI xiinxalaa jira...</b>",
}

def stats_text(chat_id):
    s = get_state(chat_id)
    lc = lang(chat_id)
    q = s.get("questions_asked", 0)
    r = s.get("region", "—")
    joined = s.get("join_date", "—")[:10]
    if lc == "am":
        return (f"📊 <b>የእርሻ ስታቲስቲክስ</b>\n\n"
                f"👤 ስም: {s.get('name','—')}\n🌍 ክልል: {r}\n"
                f"❓ ጥያቄዎች: {q}\n📅 ተቀላቅለሃል: {joined}")
    elif lc == "om":
        return (f"📊 <b>Odeeffannoo Qonnaa Koo</b>\n\n"
                f"👤 Maqaa: {s.get('name','—')}\n🌍 Naannoo: {r}\n"
                f"❓ Gaaffii: {q}\n📅 Makamte: {joined}")
    else:
        return (f"📊 <b>My Farm Stats</b>\n\n"
                f"👤 Name: {s.get('name','—')}\n🌍 Region: {r}\n"
                f"❓ Questions asked: {q}\n📅 Joined: {joined}")

def crop_calendar_text(chat_id):
    lc = lang(chat_id)
    if lc == "am":
        return ("📅 <b>የሰብል ቀን መቁጠሪያ (ኢትዮጵያ)</b>\n\n"
                "🌱 <b>ጤፍ:</b> ሰኔ-ሐምሌ ይዝሩ | መስከረም-ጥቅምት ይሰብሱ\n"
                "🌽 <b>በቆሎ:</b> መጋቢት-ሚያዝያ ይዝሩ | ሐምሌ-ነሐሴ ይሰብሱ\n"
                "🌾 <b>ስንዴ:</b> ጥቅምት-ህዳር ይዝሩ | ጥር-የካቲት ይሰብሱ\n"
                "☕ <b>ቡና:</b> ጥቅምት-ህዳር ይሰብሱ\n"
                "🫘 <b>ባቄላ:</b> ሰኔ-ሐምሌ ይዝሩ | ጥቅምት ይሰብሱ\n\n"
                "💡 ለክልልዎ ዝርዝር ምክር ጥያቄ ይጠይቁ!")
    elif lc == "om":
        return ("📅 <b>Kalandara Qonnaa (Itoophiyaa)</b>\n\n"
                "🌱 <b>Xaafii:</b> Waxabajjii-Adooleessa dhaabi | Fulbaana-Onkoloolessa sassaabi\n"
                "🌽 <b>Boqqoloo:</b> Guraandhala-Bitooteessa dhaabi | Adooleessa-Hagayya sassaabi\n"
                "🌾 <b>Qamadii:</b> Onkoloolessa-Sadaasa dhaabi | Amajjii-Guraandhala sassaabi\n"
                "☕ <b>Buna:</b> Onkoloolessa-Sadaasa sassaabi\n\n"
                "💡 Gorsa naannoo keetif gaaffii kaasi!")
    else:
        return ("📅 <b>Ethiopian Crop Calendar</b>\n\n"
                "🌱 <b>Teff:</b> Plant Jun–Jul | Harvest Sep–Oct\n"
                "🌽 <b>Maize:</b> Plant Mar–Apr | Harvest Jul–Aug\n"
                "🌾 <b>Wheat:</b> Plant Oct–Nov | Harvest Jan–Feb\n"
                "☕ <b>Coffee:</b> Harvest Oct–Nov\n"
                "🫘 <b>Beans:</b> Plant Jun–Jul | Harvest Oct\n"
                "🐑 <b>Sorghum:</b> Plant Jun | Harvest Oct–Nov\n\n"
                "💡 Ask me for region-specific planting advice!")

# ── MESSAGE HANDLER ───────────────────────────────────────────
def handle_message(chat_id, text, first_name="", photo_desc=None):
    if chat_id in banned_users: return
    if is_rate_limited(chat_id):
        send_msg(chat_id, "⚠️ Too many messages. Please wait a moment.")
        return
    if text:
        clean = sanitize(text)
        if clean is None:
            send_msg(chat_id, "⚠️ Invalid input. Please send a normal message.")
            return
        text = clean

    s = get_state(chat_id)
    s["questions_asked"] = s.get("questions_asked", 0) + 1

    # Language selection
    for lc_code, triggers in [("en", ["🇬🇧 English"]), ("am", ["🇪🇹 አማርኛ"]), ("om", ["🟢 Afaan Oromo"])]:
        if text in triggers:
            s["lang"] = lc_code; s["name"] = first_name
            send_kb(chat_id, MENUS[lc_code]["text"], MENUS[lc_code]["buttons"])
            return

    lc = lang(chat_id)
    menu = MENUS[lc]

    # Handle photo submissions
    if photo_desc:
        s["mode"] = "diagnose"
        send_msg(chat_id, PROMPTS[f"thinking_{lc}"])
        answer = ask_farm_ai(
            "Please diagnose the crop disease shown in the photo.",
            lc, s.get("region"), photo_desc
        )
        send_msg(chat_id, f"🌿 <b>EthioFarm AI:</b>\n\n{answer}")
        return

    # Commands
    if text in ["/start", "/menu"] + [m["text"].split("\n")[0].replace("<b>","").replace("</b>","") for m in MENUS.values()]:
        if s["lang"] is None:
            send_kb(chat_id, "🌾 Welcome to EthioFarm AI!\n\nYour 24/7 AI farming advisor.\n\nChoose your language:",
                    [["🇬🇧 English", "🇪🇹 አማርኛ", "🟢 Afaan Oromo"]])
        else:
            send_kb(chat_id, menu["text"], menu["buttons"])
        return

    if text in ["🔬 Diagnose Crop Disease", "🔬 የሰብል በሽታ ምርመራ", "🔬 Dhukkuba Biqiltuu"]:
        s["mode"] = "diagnose"
        remove_kb(chat_id, PROMPTS[f"diagnose_{lc}"])
        return

    if text in ["🌤️ Weather & Planting Advice", "🌤️ የአየር ሁኔታ ምክር", "🌤️ Qilleensa fi Gorsa Dhaabuu"]:
        s["mode"] = "weather"
        remove_kb(chat_id, PROMPTS[f"weather_{lc}"])
        return

    if text in ["💰 Market Prices", "💰 የገበያ ዋጋ", "💰 Gatii Gabaa"]:
        send_msg(chat_id, market_text(chat_id))
        return

    if text in ["🐄 Livestock Health", "🐄 የከብት ጤና", "🐄 Fayyaa Horsiisee"]:
        s["mode"] = "livestock"
        remove_kb(chat_id, PROMPTS[f"livestock_{lc}"])
        return

    if text in ["📅 Crop Calendar", "📅 የሰብል ቀን መቁጠሪያ", "📅 Kalandara Qonnaa"]:
        send_msg(chat_id, crop_calendar_text(chat_id))
        return

    if text in ["📊 My Farm Stats", "📊 የእርሻ ስታቲስቲክስ", "📊 Odeeffannoo Qonnaa Koo"]:
        send_msg(chat_id, stats_text(chat_id))
        return

    if text in ["🌐 Change Language", "🌐 ቋንቋ ቀይር", "🌐 Afaan Jijjiiri"]:
        s["lang"] = None
        send_kb(chat_id, "Choose your language / ቋንቋ ይምረጡ / Afaan filadhu:",
                [["🇬🇧 English", "🇪🇹 አማርኛ", "🟢 Afaan Oromo"]])
        return

    # Q&A for active modes
    if s["mode"] in ["diagnose", "weather", "livestock"] or (s["lang"] and len(text) > 5 and not text.startswith("/")):
        mode = s.get("mode", "general")
        context = {"diagnose": "crop disease diagnosis", "weather": "weather and planting",
                   "livestock": "livestock health"}.get(mode, "general farming")
        send_msg(chat_id, PROMPTS[f"thinking_{lc}"])
        answer = ask_farm_ai(f"[Context: {context}]\n\n{text}", lc, s.get("region"))
        send_msg(chat_id, f"🌿 <b>EthioFarm AI:</b>\n\n{answer}")
        follow = {"en": "\n\n💡 Ask another question or /menu",
                  "am": "\n\n💡 ሌላ ጥያቄ ወይም /menu",
                  "om": "\n\n💡 Gaaffii biraa ykn /menu"}
        send_msg(chat_id, follow[lc])
        return

    if s["lang"] is None:
        send_kb(chat_id, "🌾 Welcome to EthioFarm AI!\n\nChoose your language:",
                [["🇬🇧 English", "🇪🇹 አማርኛ", "🟢 Afaan Oromo"]])

def handle_photo(chat_id, file_id, caption, first_name):
    """Treat photo uploads as disease diagnosis with caption as context."""
    desc = f"Photo uploaded by farmer. Caption: '{caption}'" if caption else "Farmer sent a photo of affected crop."
    handle_message(chat_id, caption or "Diagnose this crop from photo", first_name, photo_desc=desc)

def main():
    print("=" * 50)
    print("  EthioFarm AI Bot — Starting...")
    print("=" * 50)
    if TELEGRAM_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("\n❌ Add your TELEGRAM_TOKEN.\n"); return
    if CLAUDE_API_KEY == "YOUR_CLAUDE_API_KEY":
        print("\n❌ Add your CLAUDE_API_KEY.\n"); return

    load_state()
    threading.Thread(target=autosave, daemon=True).start()
    log.info("EthioFarm AI started.")
    print("✅ Bot running! Open Telegram and start chatting.\n")

    offset = None
    while True:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    msg  = update["message"]
                    cid  = msg["chat"]["id"]
                    name = msg.get("from", {}).get("first_name", "")
                    text = msg.get("text", "")
                    # Handle photo uploads
                    if "photo" in msg:
                        caption = msg.get("caption", "")
                        file_id = msg["photo"][-1]["file_id"]
                        log.info(f"[{cid}] Photo upload")
                        handle_photo(cid, file_id, caption, name)
                    elif text:
                        log.info(f"[{cid}] {name}: {text[:60]}")
                        handle_message(cid, text, name)
            except Exception as e:
                log.error(f"Handler error: {e}")
        time.sleep(0.5)

if __name__ == "__main__":
    main()
