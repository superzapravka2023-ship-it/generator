# -*- coding: utf-8 -*-
"""
Pinterest Pin Generator — локально И в облаке (Render).

Генерация идёт ФОНОВОЙ задачей на сервере: запустил из браузера → можно закрыть
ноутбук/вкладку, задача продолжает крутиться на сервере. Прогресс и готовый CSV
можно посмотреть/скачать в любой момент, зайдя на тот же адрес.

Локально:  python app.py            → http://127.0.0.1:5050
В облаке:  gunicorn -w 1 -k gthread --threads 16 -t 0 --bind 0.0.0.0:$PORT app:app

Ключи — в .env (локально) или в переменных окружения сервиса (в облаке). См. .env.example.
"""
import os
import io
import re
import csv as csvmod
import json
import time
import uuid
import queue
import base64
import threading
import pathlib

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, request, Response, send_file, jsonify
from dotenv import load_dotenv

HERE = pathlib.Path(__file__).parent
load_dotenv(HERE / ".env")

# ---------------- Конфиг ----------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
TEXT_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-5-5").strip()
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "google/gemini-2.5-flash-image").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()  # пароль на вход (обязателен в облаке!)

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "").strip()
def _clean_public_base(v):
    """Защита от мусора в значении (например, прилипшей строки терминала):
    берём первый токен и обрезаем всё после .r2.dev (или после хоста)."""
    v = (v or "").strip()
    if not v:
        return ""
    v = v.split()[0]                      # отрезать всё после пробела
    m = re.search(r"^(https?://[^\s/]+\.r2\.dev)", v)   # случай r2.dev
    if m:
        return m.group(1).rstrip("/")
    m = re.search(r"^(https?://[^\s/]+)", v)            # общий случай: схема+хост
    return (m.group(1) if m else v).rstrip("/")


R2_PUBLIC_BASE = _clean_public_base(os.getenv("R2_PUBLIC_BASE", ""))

OR_BASE = "https://openrouter.ai/api/v1"
HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "http://localhost",
    "X-Title": "Pin Generator",
}

USE_PROXY = os.getenv("USE_SYSTEM_PROXY", "1") != "0"
HTTP = requests.Session()
HTTP.trust_env = USE_PROXY
# Большой пул соединений — иначе при высокой параллельности запросы ждут свободный коннект (по умолчанию всего 10).
_adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0)
HTTP.mount("https://", _adapter)
HTTP.mount("http://", _adapter)

WORKERS = int(os.getenv("WORKERS", "12"))  # конвейер + Gemini хорошо тянут; для gpt-image снизь до 2-3
PLAN_BATCH = 15

IMAGE_PRESETS = [
    {"id": "google/gemini-2.5-flash-image", "label": "Gemini Flash (реалистично, текст+руки) ~$39/1000"},
    {"id": "bytedance-seed/seedream-4.5",   "label": "Seedream 4.5 (фотореализм еды) ~$30–50/1000"},
    {"id": "openai/gpt-image-2",            "label": "GPT Image 2 (дёшево, нужен VPN) ~$6/1000"},
]
PRESET_IDS = {p["id"] for p in IMAGE_PRESETS}

CSV_COLS = ["Title", "Media URL", "Pinterest board", "Thumbnail",
            "Description", "Link", "Publish date", "Keywords", "Status"]

app = Flask(__name__)


@app.after_request
def no_cache(resp):
    # HTML и API не кэшируем — чтобы обновления интерфейса всегда применялись
    # (иначе браузер/Render отдаёт старую страницу, и правки «не срабатывают»).
    if resp.mimetype in ("text/html", "application/json"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# ---------------- R2 (S3) ----------------
_S3 = None
_S3_LOCK = threading.Lock()


def r2_client():
    # Один переиспользуемый клиент (boto3 потокобезопасен) — не создаём его на каждую картинку.
    global _S3
    if _S3 is None:
        with _S3_LOCK:
            if _S3 is None:
                import boto3
                from botocore.config import Config
                endpoint = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
                _S3 = boto3.client(
                    "s3", endpoint_url=endpoint,
                    aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                    region_name="auto",
                    config=Config(signature_version="s3v4", proxies=(None if USE_PROXY else {}),
                                  max_pool_connections=64, retries={"max_attempts": 3}),
                )
    return _S3


def upload_to_r2(key, data, content_type="image/jpeg"):
    s3 = r2_client()
    s3.put_object(Bucket=R2_BUCKET, Key=key, Body=data,
                  ContentType=content_type, CacheControl="public, max-age=31536000")
    return f"{R2_PUBLIC_BASE}/{key}"


# ---------------- Утилиты ----------------
def slugify(s):
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9а-яё\- ]+", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s or "session"


def cap(text, limit):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.strip().rstrip(",.;:")


def png_to_jpg(raw, quality=85):
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def build_csv_text(rows):
    buf = io.StringIO()
    w = csvmod.writer(buf)
    w.writerow(CSV_COLS)
    for r in rows:
        w.writerow([r.get(c, "") for c in CSV_COLS])
    return buf.getvalue()


# ---------------- OpenRouter: текст ----------------
def chat_json(system, user, max_tokens=4000):
    r = HTTP.post(f"{OR_BASE}/chat/completions", headers=HEADERS, json={
        "model": TEXT_MODEL, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }, timeout=120)
    if not r.ok:
        raise RuntimeError(f"OpenRouter text {r.status_code}: {r.text[:300]}")
    return parse_plan_json(r.json()["choices"][0]["message"]["content"])


def parse_plan_json(content):
    """Устойчивый разбор: собираем ВСЕ целые объекты между [ ... ] даже при обрыве."""
    s = content or ""
    i = s.find("[")
    if i == -1:
        raise RuntimeError("нет JSON-массива в ответе: " + s[:150])
    objs, depth, in_str, esc, start_obj = [], 0, False, False, None
    j = i
    while j < len(s):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                if depth == 0:
                    start_obj = j
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and start_obj is not None:
                    try:
                        objs.append(json.loads(s[start_obj:j + 1]))
                    except Exception:
                        pass
                    start_obj = None
            elif c == "]" and depth == 0:
                break
        j += 1
    if not objs:
        raise RuntimeError("не удалось разобрать объекты JSON: " + s[:150])
    return objs


def plan_batch(keywords, brief, n, avoid):
    system = ("You are a senior Pinterest content strategist for the US market. You produce UNIQUE pin "
              "concepts and their copy for ANY niche. You output ONLY valid JSON. No markdown, no commentary.")
    avoid_block = ""
    if avoid:
        avoid_block = "\n\nDO NOT repeat or closely resemble these already-used titles:\n- " + "\n- ".join(avoid)
    kw_line = keywords.strip() if keywords and keywords.strip() else \
        "(none given — infer suitable, varied unique subjects that fit the design brief)"
    user = f"""Create {n} COMPLETELY UNIQUE Pinterest pins.

TOPIC / NICHE KEYWORDS (use these for the VARIETY of subjects):
{kw_line}

DESIGN BRIEF (the visual style EVERY pin must follow exactly — only the subject and typography change between pins):
{brief}{avoid_block}

Return a JSON array of exactly {n} objects. Each object:
{{
  "subject": "a short, SPECIFIC and UNIQUE subject for THIS pin (the exact dish / item / topic), clearly different from every other pin and from the avoid list",
  "style": "a DISTINCT typography + design style for THIS pin — a specific font pairing and overall layout flavor, clearly DIFFERENT from the other pins. Vary widely: 'bold condensed modern sans-serif, minimalist'; 'elegant vintage serif with flowing script accent'; 'playful rounded handwritten'; 'rustic farmhouse slab serif'; 'bright editorial magazine serif'; 'chunky retro display'; 'delicate thin serif, lots of whitespace'; 'bold all-caps poster'. Invent fresh combinations.",
  "title": "catchy Pinterest title, MAX 100 characters, unique",
  "description": "engaging Pinterest description, MAX 400 characters, keyword-rich, unique, natural US English",
  "keywords": "2-4 comma-separated Pinterest search phrases"
}}

RULES:
- Every pin must be UNIQUE — different subject, title, description AND typography style. No repeats or near-duplicates.
- The "style" must differ from pin to pin — never reuse the same font/layout style back to back.
- Titles <= 100 chars, descriptions <= 400 chars.
- Language: English (US).
Return ONLY the JSON array."""
    plans = chat_json(system, user, max_tokens=4000)
    for p in plans:
        p["subject"] = (p.get("subject", "") or "").strip()
        p["style"] = (p.get("style", "") or "").strip()
        p["title"] = cap(p.get("title", ""), 100)
        p["description"] = cap(p.get("description", ""), 400)
        p["keywords"] = (p.get("keywords", "") or "").strip()
    return plans[:n]


def build_image_prompt(brief, p):
    parts = [brief]
    subj = (p.get("subject") or "").strip()
    if subj:
        parts.append(f"The specific subject of THIS pin: {subj}. "
                     "Make it completely unique and visually different from the other pins.")
    style = (p.get("style") or "").strip()
    if style:
        parts.append("Typography & design style for THIS pin (the fonts, title treatment and layout "
                     f"must be clearly DIFFERENT from other pins): {style}.")
    return "\n\n".join(parts)


# ---------------- OpenRouter: картинка ----------------
def image_params(model, quality):
    p = {"aspect_ratio": "2:3", "n": 1}
    if model.startswith("openai/gpt-image"):
        p["quality"] = quality
    elif "seedream" in model:
        p["resolution"] = "1K" if quality == "low" else "2K"
    return p


def generate_image(prompt, model, quality="low"):
    """С авто-повтором при временных сбоях (429 rate limit / 5xx / обрыв сети)."""
    payload = {"model": model, "prompt": prompt}
    payload.update(image_params(model, quality))
    err = None
    for attempt in range(3):
        try:
            r = HTTP.post(f"{OR_BASE}/images", headers=HEADERS, json=payload, timeout=240)
        except requests.RequestException as e:
            err = RuntimeError("сеть: " + str(e)[:120])
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            err = RuntimeError(f"OpenRouter image {r.status_code} (повтор)")
            time.sleep(1 + attempt)  # короткий бэкофф, чтобы не тормозить весь поток
            continue
        if not r.ok:
            # не временная ошибка (напр. 403 гео) — повторять бессмысленно
            raise RuntimeError(f"OpenRouter image {r.status_code}: {r.text[:300]}")
        data = r.json().get("data", [])
        if not data:
            raise RuntimeError("Пустой ответ image API: " + r.text[:200])
        item = data[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        url = item.get("url") or (item.get("image_url") or {}).get("url")
        if url:
            ir = HTTP.get(url, timeout=120)
            ir.raise_for_status()
            return ir.content
        raise RuntimeError("Не нашёл картинку в ответе: " + r.text[:200])
    raise err or RuntimeError("image failed")


def make_one(p, idx, P):
    raw = generate_image(build_image_prompt(P["brief"], p), P["image_model"], quality=P["quality"])
    jpg = png_to_jpg(raw)
    url = upload_to_r2(f"{P['folder']}/{idx:04d}.jpg", jpg, "image/jpeg")
    return {
        "Title": p["title"], "Media URL": url, "Pinterest board": "",
        "Thumbnail": "", "Description": p["description"], "Link": P["link"],
        "Publish date": "", "Keywords": p["keywords"], "Status": "",
    }


# ---------------- Фоновые задачи ----------------
JOBS = {}
JOBS_LOCK = threading.Lock()


def _save_csv(job, folder):
    with JOBS_LOCK:
        rows = list(job["rows"])
    try:
        upload_to_r2(f"{folder}/_pins.csv", build_csv_text(rows).encode("utf-8"), "text/csv")
        job["csv_url"] = f"{R2_PUBLIC_BASE}/{folder}/_pins.csv"
    except Exception:
        pass


def run_job(job_id, P):
    """Конвейер: отдельный поток ПЛАНИРУЕТ пины и кладёт их в очередь,
    а пул воркеров ОДНОВРЕМЕННО генерит картинки. Планирование и генерация идут
    параллельно — воркеры не простаивают, скорость заметно выше."""
    job = JOBS[job_id]
    count, boards, start = P["count"], P["boards"], P["start"]
    folder = P["folder"]
    q = queue.Queue(maxsize=max(4, WORKERS * 3))
    plan_done = threading.Event()
    used = []

    def planner():
        base, guard = 0, 0
        while base < count and guard < count + 40 and not job.get("cancel"):
            guard += 1
            n = min(PLAN_BATCH, count - base)
            try:
                batch = plan_batch(P["keywords"], P["brief"], n, used[-80:])
            except Exception:
                try:
                    batch = plan_batch(P["keywords"], P["brief"], n, used[-80:])
                except Exception:
                    continue
            if not batch:
                continue
            batch = batch[:n]
            for k, p in enumerate(batch):
                if job.get("cancel"):
                    break
                idx = start + base + k
                board = boards[(base + k) % len(boards)]
                while not job.get("cancel"):
                    try:
                        q.put((p, idx, board), timeout=0.5)
                        break
                    except queue.Full:
                        continue
            base += len(batch)
            used.extend([p.get("title", "") for p in batch])
        plan_done.set()

    def worker():
        while not job.get("cancel"):
            try:
                p, idx, board = q.get(timeout=0.5)
            except queue.Empty:
                if plan_done.is_set():
                    break
                continue
            try:
                row = make_one(p, idx, P)
                row["Pinterest board"] = board
                with JOBS_LOCK:
                    job["rows"].append(row)
            except Exception as e:
                with JOBS_LOCK:
                    job["errors"].append({"title": p.get("title", ""), "msg": str(e)[:200]})
            with JOBS_LOCK:
                job["done"] += 1

    try:
        pth = threading.Thread(target=planner, daemon=True)
        pth.start()
        workers = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, WORKERS))]
        for w in workers:
            w.start()

        # Монитор: периодически сохраняем CSV на R2 и обновляем статус.
        last_saved = -1
        while True:
            alive = any(w.is_alive() for w in workers)
            with JOBS_LOCK:
                n_rows = len(job["rows"])
                n_err = len(job["errors"])
                done = job["done"]
            if n_rows and n_rows != last_saved:
                _save_csv(job, folder)
                last_saved = n_rows
            job["stage"] = (f"Генерация… {done}/{count}" + (f" · ошибок {n_err}" if n_err else "")
                            + ("" if plan_done.is_set() else " · планирую…"))
            if not alive and (plan_done.is_set() or job.get("cancel")):
                break
            time.sleep(2)

        for w in workers:
            w.join(timeout=5)
        _save_csv(job, folder)  # финальное сохранение

        if job.get("cancel"):
            job["status"] = "stopped"
            job["stage"] = f"Остановлено. Готово: {len(job['rows'])}. Ошибок: {len(job['errors'])}."
        else:
            job["status"] = "done"
            job["stage"] = f"Готово. Успешно: {len(job['rows'])} из {count}. Ошибок: {len(job['errors'])}."
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["stage"] = "Сбой: " + str(e)[:200]


# ---------------- Авторизация ----------------
def authed(req):
    if not ADMIN_PASSWORD:
        return True
    key = req.headers.get("X-Admin-Key") or req.args.get("key") or ""
    return key == ADMIN_PASSWORD


# ---------------- Маршруты ----------------
@app.get("/")
def index():
    return send_file(HERE / "web" / "index.html")


@app.get("/api/config-check")
def config_check():
    missing = [k for k, v in {
        "OPENROUTER_API_KEY": OPENROUTER_API_KEY, "R2_ACCOUNT_ID": R2_ACCOUNT_ID,
        "R2_ACCESS_KEY_ID": R2_ACCESS_KEY_ID, "R2_SECRET_ACCESS_KEY": R2_SECRET_ACCESS_KEY,
        "R2_BUCKET": R2_BUCKET, "R2_PUBLIC_BASE": R2_PUBLIC_BASE,
    }.items() if not v]
    return jsonify({"ok": not missing, "missing": missing,
                    "models": IMAGE_PRESETS, "default_model": IMAGE_PRESETS[0]["id"],
                    "public_base": R2_PUBLIC_BASE,
                    "auth_required": bool(ADMIN_PASSWORD)})


@app.post("/api/login")
def login():
    pw = (request.get_json(force=True) or {}).get("password", "")
    return jsonify({"ok": (not ADMIN_PASSWORD) or pw == ADMIN_PASSWORD})


@app.post("/api/start")
def start():
    if not authed(request):
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(force=True) or {}
    brief = (body.get("base_prompt") or "").strip()
    if not brief:
        return jsonify({"error": "Поле «Промпт» обязательно."}), 400
    folder = slugify(body.get("folder") or "")
    image_model = (body.get("image_model") or "").strip()
    if image_model not in PRESET_IDS:
        image_model = IMAGE_PRESETS[0]["id"]
    P = {
        "brief": brief,
        "keywords": (body.get("keywords") or "").strip(),
        "count": max(1, min(int(body.get("count") or 1), 2000)),
        "quality": (body.get("quality") or "low").strip(),
        "image_model": image_model,
        "folder": folder,
        "boards": [b.strip() for b in (body.get("boards") or "").splitlines() if b.strip()] or [""],
        "link": (body.get("link") or "").strip(),
        "start": max(1, int(body.get("start") or 1)),
    }
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "stage": "Запуск…", "done": 0,
                        "total": P["count"], "rows": [], "errors": [],
                        "csv_url": "", "error": "", "folder": folder,
                        "cancel": False, "created": time.time()}
    threading.Thread(target=run_job, args=(job_id, P), daemon=True).start()
    return jsonify({"job_id": job_id, "total": P["count"], "folder": folder})


@app.get("/api/status")
def status():
    if not authed(request):
        return jsonify({"error": "unauthorized"}), 401
    job = JOBS.get(request.args.get("job_id", ""))
    if not job:
        return jsonify({"error": "not found"}), 404
    after = int(request.args.get("after", 0))
    return jsonify({
        "status": job["status"], "stage": job["stage"],
        "done": job["done"], "total": job["total"],
        "errors": len(job["errors"]), "csv_url": job["csv_url"],
        "folder": job["folder"],
        "rows": job["rows"][after:],   # только новые строки
        "rows_total": len(job["rows"]),
    })


@app.post("/api/stop")
def stop():
    if not authed(request):
        return jsonify({"error": "unauthorized"}), 401
    jid = request.args.get("job_id", "") or (request.get_json(silent=True) or {}).get("job_id", "")
    job = JOBS.get(jid)
    if not job:
        return jsonify({"error": "not found"}), 404
    job["cancel"] = True
    return jsonify({"ok": True})


@app.get("/api/jobs")
def jobs():
    if not authed(request):
        return jsonify({"error": "unauthorized"}), 401
    items = sorted(JOBS.items(), key=lambda kv: kv[1]["created"], reverse=True)[:20]
    return jsonify({"jobs": [
        {"job_id": jid, "folder": j["folder"], "status": j["status"],
         "done": j["done"], "total": j["total"], "created": j["created"]}
        for jid, j in items]})


@app.get("/api/download")
def download():
    if not authed(request):
        return jsonify({"error": "unauthorized"}), 401
    job = JOBS.get(request.args.get("job_id", ""))
    if not job:
        return jsonify({"error": "not found"}), 404
    text = build_csv_text(job["rows"])
    return Response(text, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{job["folder"]}.csv"'})


if __name__ == "__main__":
    PORT = int(os.getenv("PORT", "5050"))  # 5050: на macOS порт 5000 занят AirPlay
    print(f"\n  Pin Generator →  http://127.0.0.1:{PORT}\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
