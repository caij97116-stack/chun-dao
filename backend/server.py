import sqlite3
import secrets
import string
import os
import json
import logging
import asyncio
import re
import tempfile
import subprocess
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, quote
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import contextmanager
import httpx
from bs4 import BeautifulSoup

# OCR 手写识别
try:
    import pytesseract
    from PIL import Image
    import io
    HAS_OCR = True
except ImportError:
    HAS_OCR = False
    logging.warning("pytesseract/Pillow 未安装，OCR 手写识别不可用。pip install pytesseract Pillow")

# Emoji 搜索
try:
    import emoji as emoji_lib
    HAS_EMOJI = True
except ImportError:
    HAS_EMOJI = False
    logging.warning("emoji 库未安装，Emoji API 不可用")

# Scrapling 轻量爬虫
try:
    from scrapling import PlaywrightScraper
    HAS_SCRAPLING = True
except ImportError:
    HAS_SCRAPLING = False
    logging.warning("scrapling 未安装，将使用 httpx+BeautifulSoup 作为爬虫方案")

# Web Push 依赖
try:
    from pywebpush import webpush, WebPushException
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    HAS_WEB_PUSH = True
except ImportError:
    HAS_WEB_PUSH = False
    logging.warning("pywebpush 未安装，Web Push 功能不可用。请运行: pip install pywebpush")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chundao_codes.db")
TZ = timezone(timedelta(hours=8))

app = FastAPI(title="椿岛测试码管理")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                is_activated INTEGER DEFAULT 0,
                activated_at TEXT,
                created_at TEXT NOT NULL,
                notes TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_apps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                full_description TEXT DEFAULT '',
                category TEXT DEFAULT '工具',
                icon_emoji TEXT DEFAULT '⊡',
                color TEXT DEFAULT '#607d8b',
                hero_color TEXT DEFAULT '#455a64',
                author_id TEXT DEFAULT 'anonymous',
                author_name TEXT DEFAULT '匿名开发者',
                status TEXT DEFAULT 'pending',
                source_code TEXT DEFAULT '',
                screenshots TEXT DEFAULT '[]',
                rating REAL DEFAULT 0,
                downloads TEXT DEFAULT '0',
                review_notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT UNIQUE NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

init_db()

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

# ══════════ VAPID Key Management (Web Push) ══════════
VAPID_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vapid_keys.json")

def _generate_vapid_keys():
    """生成 ECDSA P-256 VAPID 密钥对"""
    if not HAS_WEB_PUSH:
        return None, None
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    # 导出为 raw bytes (未压缩格式, 65 bytes: 04 + x + y)
    private_raw = private_key.private_numbers().private_value.to_bytes(32, 'big')
    public_raw = public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint
    )

    # Base64 URL-safe 编码
    import base64
    private_b64 = base64.urlsafe_b64encode(private_raw).rstrip(b'=').decode('ascii')
    public_b64 = base64.urlsafe_b64encode(public_raw).rstrip(b'=').decode('ascii')

    return private_b64, public_b64

def _load_or_create_vapid_keys():
    """加载或创建 VAPID 密钥对"""
    if os.path.exists(VAPID_KEY_FILE):
        with open(VAPID_KEY_FILE, 'r') as f:
            data = json.load(f)
            if data.get('private_key') and data.get('public_key'):
                return data['private_key'], data['public_key']

    private_key, public_key = _generate_vapid_keys()
    if private_key and public_key:
        with open(VAPID_KEY_FILE, 'w') as f:
            json.dump({'private_key': private_key, 'public_key': public_key}, f)
        logging.info("VAPID 密钥对已生成并保存")
    return private_key, public_key

VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY = _load_or_create_vapid_keys()
VAPID_CLAIMS = {"sub": "mailto:admin@chundao.app"}

# ══════════ Push Subscription Models ══════════
class PushSubscribeRequest(BaseModel):
    endpoint: str
    keys: dict

class PushUnsubscribeRequest(BaseModel):
    endpoint: str

class PushSendRequest(BaseModel):
    title: str
    body: str = ""
    icon: str = "/icon-192.png"
    image: str = None
    tag: str = "tsubaki-msg"
    charId: str = None
    requireInteraction: bool = False
    timestamp: int = 0

@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


def generate_random_code(length=8):
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


class GenerateRequest(BaseModel):
    codes: list[str] = []
    count: int = 0
    prefix: str = ""
    notes: str = ""

class ActivateRequest(BaseModel):
    pass


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(content=ADMIN_HTML)

@app.get("/api/stats")
async def get_stats():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM invite_codes").fetchone()["c"]
        activated = conn.execute("SELECT COUNT(*) as c FROM invite_codes WHERE is_activated=1").fetchone()["c"]
    return {"total": total, "activated": activated, "unactivated": total - activated}

@app.get("/api/codes")
async def list_codes(
    status: str = Query("all"),
    search: str = Query(""),
    page: int = Query(1),
    per_page: int = Query(50)
):
    query = "SELECT * FROM invite_codes WHERE 1=1"
    params = []
    if status == "activated":
        query += " AND is_activated=1"
    elif status == "unactivated":
        query += " AND is_activated=0"
    if search:
        query += " AND code LIKE ?"
        params.append(f"%{search}%")
    query += " ORDER BY id DESC"
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    codes = [dict(r) for r in rows]
    total = len(codes)
    start = (page - 1) * per_page
    end = start + per_page
    return {"codes": codes[start:end], "total": total, "page": page, "per_page": per_page}

@app.post("/api/codes/generate")
async def generate_codes(req: GenerateRequest):
    codes_to_insert = []
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

    if req.codes:
        codes_to_insert = [(c.strip(), now, req.notes) for c in req.codes if c.strip()]
    elif req.count > 0:
        existing = set()
        with get_db() as conn:
            rows = conn.execute("SELECT code FROM invite_codes").fetchall()
            existing = {r["code"] for r in rows}
        generated = []
        for _ in range(req.count):
            while True:
                code = req.prefix + generate_random_code()
                if code not in existing and code not in generated:
                    generated.append(code)
                    break
            codes_to_insert.append((code, now, req.notes))
    else:
        raise HTTPException(status_code=400, detail="请提供 codes 列表或 count 数量")

    with get_db() as conn:
        inserted = []
        for code, created_at, notes in codes_to_insert:
            try:
                conn.execute(
                    "INSERT INTO invite_codes (code, created_at, notes) VALUES (?, ?, ?)",
                    (code, created_at, notes)
                )
                inserted.append(code)
            except sqlite3.IntegrityError:
                pass
    return {"success": True, "codes": inserted, "count": len(inserted)}

@app.post("/api/codes/{code}/activate")
async def activate_code(code: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="测试码不存在")
        if row["is_activated"]:
            return {"success": False, "message": "该测试码已被激活"}
        now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE invite_codes SET is_activated=1, activated_at=? WHERE code=?",
            (now, code)
        )
    return {"success": True, "code": code, "activated_at": now}

@app.get("/api/codes/{code}/status")
async def check_code(code: str):
    with get_db() as conn:
        row = conn.execute("SELECT code, is_activated, activated_at FROM invite_codes WHERE code=?", (code,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="测试码不存在")
    return dict(row)

@app.delete("/api/codes/{code}")
async def delete_code(code: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="测试码不存在")
        conn.execute("DELETE FROM invite_codes WHERE code=?", (code,))
    return {"success": True}

# ══════════ Store App APIs ══════════

class StoreAppUpload(BaseModel):
    name: str
    description: str = ""
    full_description: str = ""
    category: str = "工具"
    icon_emoji: str = "⊡"
    color: str = "#607d8b"
    hero_color: str = "#455a64"
    author_id: str = "anonymous"
    author_name: str = "匿名开发者"
    source_code: str = ""

class StoreAppReview(BaseModel):
    review_notes: str = ""

@app.post("/api/store/apps/upload")
async def upload_store_app(req: StoreAppUpload):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="应用名称不能为空")
    if not req.source_code.strip():
        raise HTTPException(status_code=400, detail="源代码不能为空")
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO store_apps (name, description, full_description, category, icon_emoji, color, hero_color,
            author_id, author_name, status, source_code, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (req.name.strip(), req.description.strip(), req.full_description.strip(), req.category.strip(),
              req.icon_emoji.strip() or '⊡', req.color.strip(), req.hero_color.strip(),
              req.author_id.strip(), req.author_name.strip(), req.source_code, now))
        new_id = cursor.lastrowid
    return {"success": True, "id": new_id, "message": "提交成功，等待审核"}

@app.get("/api/store/apps")
async def list_store_apps(category: str = Query("全部"), search: str = Query("")):
    query = "SELECT id, name, description, category, icon_emoji, color, hero_color, author_name, rating, downloads, created_at FROM store_apps WHERE status='approved'"
    params = []
    if category and category != "全部":
        query += " AND category=?"
        params.append(category)
    if search:
        query += " AND (name LIKE ? OR description LIKE ? OR category LIKE ?)"
        params.extend([f"%{search}%"] * 3)
    query += " ORDER BY id DESC"
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    return {"apps": [dict(r) for r in rows]}

@app.get("/api/store/apps/my")
async def my_store_apps(author_id: str = Query("anonymous")):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, description, category, icon_emoji, color, status, review_notes, created_at, downloads FROM store_apps WHERE author_id=? ORDER BY id DESC",
            (author_id,)
        ).fetchall()
    return {"apps": [dict(r) for r in rows]}

@app.get("/api/store/apps/review")
async def review_store_apps():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM store_apps ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, id DESC"
        ).fetchall()
    return {"apps": [dict(r) for r in rows]}

@app.get("/api/store/apps/{app_id}")
async def get_store_app(app_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM store_apps WHERE id=?", (app_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="应用不存在")
    app = dict(row)
    app["screenshots"] = json.loads(app.get("screenshots", "[]"))
    return app

@app.post("/api/store/apps/{app_id}/approve")
async def approve_store_app(app_id: int, req: StoreAppReview = StoreAppReview()):
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        row = conn.execute("SELECT id, status FROM store_apps WHERE id=?", (app_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="应用不存在")
        conn.execute(
            "UPDATE store_apps SET status='approved', review_notes=?, updated_at=? WHERE id=?",
            (req.review_notes, now, app_id)
        )
    return {"success": True, "message": "已通过审核"}

@app.post("/api/store/apps/{app_id}/reject")
async def reject_store_app(app_id: int, req: StoreAppReview):
    if not req.review_notes.strip():
        raise HTTPException(status_code=400, detail="请填写拒绝原因")
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        row = conn.execute("SELECT id, status FROM store_apps WHERE id=?", (app_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="应用不存在")
        conn.execute(
            "UPDATE store_apps SET status='rejected', review_notes=?, updated_at=? WHERE id=?",
            (req.review_notes, now, app_id)
        )
    return {"success": True, "message": "已拒绝"}

@app.delete("/api/store/apps/{app_id}")
async def delete_store_app(app_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT id FROM store_apps WHERE id=?", (app_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="应用不存在")
        conn.execute("DELETE FROM store_apps WHERE id=?", (app_id,))
    return {"success": True}

# ══════════ Translation API (DeepLX 双引擎) ══════════
# 引擎1: DeepL — 36种语言，中日英质量最高，优先使用
# 引擎2: Google — 100+语言含方言，DeepL不支持时自动回退
# 公共实例: https://dplx.xi-xu.me (Cloudflare全球加速，免费零部署)

DEEPLX_BASE = os.environ.get("DEEPLX_BASE", "https://dplx.xi-xu.me")

class TranslateRequest(BaseModel):
    text: str
    target_lang: str = "zh"          # 目标语言代码，如 zh/en/ja/ko/fr/de/...
    source_lang: str = "auto"        # 源语言，auto=自动检测

async def _call_deeplx(client: httpx.AsyncClient, engine: str, text: str, source: str, target: str):
    """调用 DeepLX 翻译引擎 (engine: 'deepl' 或 'google')"""
    url = f"{DEEPLX_BASE}/{engine}"
    resp = await client.post(
        url,
        json={"text": text, "source_lang": source, "target_lang": target},
        headers={"Content-Type": "application/json"}
    )
    if resp.status_code == 200:
        data = resp.json()
        return data.get("data", ""), data.get("source_lang", "")
    return None, None

@app.post("/api/translate")
async def translate_text(req: TranslateRequest):
    """双引擎翻译：DeepL优先 → Google兜底"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 引擎1: DeepL — 质量最高，中日英互译首选
            result, detected = await _call_deeplx(
                client, "deepl", req.text, req.source_lang, req.target_lang
            )
            engine = "deepl"
            
            # 引擎2: Google — DeepL失败时自动回退（方言/小语种）
            if result is None:
                logging.info(f"DeepL 翻译失败，回退到 Google 引擎")
                result, detected = await _call_deeplx(
                    client, "google", req.text, req.source_lang, req.target_lang
                )
                engine = "google"
            
            if result is None:
                raise HTTPException(status_code=502, detail="翻译服务暂时不可用")
            
            return {
                "translation": result,
                "detected_lang": detected or "",
                "engine": engine,
                "source_text": req.text
            }
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="翻译服务不可达")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"翻译异常: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ══════════ Push Notification APIs ══════════

@app.get("/api/push/vapid-public-key")
async def get_vapid_public_key():
    """返回 VAPID 公钥，供前端订阅推送"""
    if not HAS_WEB_PUSH or not VAPID_PUBLIC_KEY:
        raise HTTPException(status_code=501, detail="Web Push 功能未启用")
    return {"publicKey": VAPID_PUBLIC_KEY}

@app.post("/api/push/subscribe")
async def push_subscribe(req: PushSubscribeRequest):
    """保存推送订阅"""
    if not HAS_WEB_PUSH:
        raise HTTPException(status_code=501, detail="Web Push 功能未启用")
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO push_subscriptions (endpoint, p256dh, auth, created_at) VALUES (?, ?, ?, ?)",
            (req.endpoint, req.keys.get('p256dh', ''), req.keys.get('auth', ''), now)
        )
    logging.info(f"推送订阅已保存: {req.endpoint[:50]}...")
    return {"success": True}

@app.post("/api/push/unsubscribe")
async def push_unsubscribe(req: PushUnsubscribeRequest):
    """删除推送订阅"""
    with get_db() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (req.endpoint,))
    logging.info(f"推送订阅已删除: {req.endpoint[:50]}...")
    return {"success": True}

@app.post("/api/push/send")
async def push_send(req: PushSendRequest):
    """向所有已订阅设备发送推送通知"""
    if not HAS_WEB_PUSH:
        raise HTTPException(status_code=501, detail="Web Push 功能未启用")

    with get_db() as conn:
        rows = conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()

    if not rows:
        return {"success": True, "sent": 0, "message": "无订阅设备"}

    payload = {
        "title": req.title,
        "body": req.body,
        "icon": req.icon,
        "image": req.image,
        "tag": req.tag,
        "url": "/",
        "charId": req.charId,
        "requireInteraction": req.requireInteraction,
        "timestamp": req.timestamp,
        "vibrate": [200, 100, 200]
    }

    sent_count = 0
    failed_count = 0
    removed_endpoints = []

    for row in rows:
        try:
            subscription_info = {
                "endpoint": row["endpoint"],
                "keys": {
                    "p256dh": row["p256dh"],
                    "auth": row["auth"]
                }
            }
            webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS
            )
            sent_count += 1
        except WebPushException as e:
            logging.warning(f"推送失败: {e}")
            # 410/404 表示订阅已失效，清理
            if hasattr(e, 'response') and e.response is not None:
                if e.response.status_code in (410, 404):
                    removed_endpoints.append(row["endpoint"])
            failed_count += 1
        except Exception as e:
            logging.error(f"推送异常: {e}")
            failed_count += 1

    # 清理失效订阅
    if removed_endpoints:
        with get_db() as conn:
            for ep in removed_endpoints:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (ep,))
        logging.info(f"已清理 {len(removed_endpoints)} 个失效订阅")

    return {"success": True, "sent": sent_count, "failed": failed_count}

# ══════════ Web Scraping API (Scrapling → httpx+BS4 → Jina Reader 三层降级) ══════════

class ScrapeRequest(BaseModel):
    url: str
    timeout_ms: int = 8000

class ScrapeResponse(BaseModel):
    url: str
    content: str
    engine: str  # scrapling / httpx / jina / firecrawl

async def _scrape_via_httpx(url: str, timeout: float) -> tuple:
    """使用 httpx + BeautifulSoup 抓取网页文本"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, "lxml")
        # 移除脚本和样式
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        # 提取正文
        body = soup.find("body")
        if not body:
            body = soup
        text = body.get_text(separator="\n", strip=True)
        # 清理多余空行
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        content = "\n".join(lines)
        return content, resp.headers.get("content-type", "")

async def _scrape_via_scrapling(url: str, timeout: float) -> tuple:
    """使用 Scrapling 抓取网页（支持JS渲染）"""
    if not HAS_SCRAPLING:
        raise RuntimeError("Scrapling 未安装")
    # Scrapling 的 PlaywrightScraper 需要浏览器
    # 在 Render 上可能没有浏览器，降级使用普通模式
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _scrapling_sync, url, timeout)
    return result, "text/html"

def _scrapling_sync(url: str, timeout: float):
    """Scrapling 同步抓取（在 executor 中运行）"""
    from scrapling import Adaptor
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    page = Adaptor(resp.text, url=url)
    # 提取正文
    content = page.get_page_text()
    return content

async def _scrape_via_jina(url: str, timeout: float) -> tuple:
    """使用 Jina AI Reader 抓取（外部服务，无需本地依赖）"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"https://r.jina.ai/{quote(url, safe='')}",
            headers={"Accept": "text/markdown", "X-No-Cache": "true"}
        )
        resp.raise_for_status()
        return resp.text, "text/markdown"

async def _scrape_via_firecrawl(url: str, timeout: float, api_key: str = "") -> tuple:
    """使用 Firecrawl API 抓取（支持JS渲染）"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            "https://api.firecrawl.dev/v1/scrape",
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True, "timeout": min(int(timeout * 1000), 30000)},
            headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") and data.get("data", {}).get("markdown"):
            return data["data"]["markdown"], "text/markdown"
        raise RuntimeError("Firecrawl 返回无内容")

@app.post("/api/scrape")
async def scrape_url(req: ScrapeRequest):
    """
    抓取链接内容，三层降级策略：
    1. Scrapling（Python原生，轻量快速）
    2. httpx + BeautifulSoup（纯Python，零额外依赖）
    3. Jina AI Reader（外部服务，Markdown格式）
    4. Firecrawl（外部服务，JS渲染兜底）
    """
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL 不能为空")
    
    timeout = req.timeout_ms / 1000.0
    content_limit = 5000
    engines_tried = []
    last_error = None
    
    # 引擎1: Scrapling
    if HAS_SCRAPLING:
        try:
            content, _ = await _scrape_via_scrapling(url, timeout)
            if content and len(content.strip()) > 50:
                truncated = content[:content_limit] + ("\n\n…(内容过长已截断)" if len(content) > content_limit else "")
                return {"url": url, "content": truncated, "engine": "scrapling"}
            engines_tried.append("scrapling(内容过短)")
        except Exception as e:
            engines_tried.append(f"scrapling({str(e)[:50]})")
            last_error = e
    
    # 引擎2: httpx + BeautifulSoup
    try:
        content, _ = await _scrape_via_httpx(url, timeout)
        if content and len(content.strip()) > 50:
            truncated = content[:content_limit] + ("\n\n…(内容过长已截断)" if len(content) > content_limit else "")
            return {"url": url, "content": truncated, "engine": "httpx+bs4"}
        engines_tried.append("httpx+bs4(内容过短)")
    except Exception as e:
        engines_tried.append(f"httpx+bs4({str(e)[:50]})")
        last_error = e
    
    # 引擎3: Jina AI Reader
    try:
        content, _ = await _scrape_via_jina(url, timeout)
        if content and len(content.strip()) > 50:
            truncated = content[:content_limit] + ("\n\n…(内容过长已截断)" if len(content) > content_limit else "")
            return {"url": url, "content": truncated, "engine": "jina"}
        engines_tried.append("jina(内容过短)")
    except Exception as e:
        engines_tried.append(f"jina({str(e)[:50]})")
        last_error = e
    
    # 引擎4: Firecrawl
    try:
        content, _ = await _scrape_via_firecrawl(url, timeout)
        if content and len(content.strip()) > 50:
            truncated = content[:content_limit] + ("\n\n…(内容过长已截断)" if len(content) > content_limit else "")
            return {"url": url, "content": truncated, "engine": "firecrawl"}
        engines_tried.append("firecrawl(内容过短)")
    except Exception as e:
        engines_tried.append(f"firecrawl({str(e)[:50]})")
        last_error = e
    
    # 全部失败
    raise HTTPException(
        status_code=502,
        detail=f"所有抓取引擎均失败: {' → '.join(engines_tried)}"
    )

# ══════════ Video Subtitle Extraction API (yt-dlp) ══════════

class VideoSubtitlesRequest(BaseModel):
    url: str
    lang: str = "zh"  # 字幕语言，zh/en/ja 等

@app.get("/api/video/subtitles")
async def extract_video_subtitles(
    url: str = Query(..., description="视频链接"),
    lang: str = Query("zh", description="字幕语言代码")
):
    """
    提取视频字幕/字幕信息
    支持 YouTube、Bilibili 等主流平台
    使用 yt-dlp 提取字幕
    """
    if not url.strip():
        raise HTTPException(status_code=400, detail="URL 不能为空")
    
    try:
        # 使用 yt-dlp 提取字幕
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-lang", lang,
            "--convert-subs", "srt",
            "--print", "filename",
            "--print", "title",
            "--print", "duration_string",
            "-o", "-",
            url
        ]
        
        loop = asyncio.get_event_loop()
        
        # 先获取视频信息
        info_cmd = [
            "yt-dlp",
            "--dump-json",
            "--no-playlist",
            url
        ]
        
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(info_cmd, capture_output=True, text=True, timeout=30)
            )
            if result.returncode == 0 and result.stdout.strip():
                info = json.loads(result.stdout)
                
                # 提取字幕
                subtitles_list = []
                subs = info.get("subtitles", {})
                auto_subs = info.get("automatic_captions", {})
                
                # 合并手动和自动字幕
                all_subs = {**auto_subs, **subs}
                
                for sub_lang, sub_entries in all_subs.items():
                    for entry in sub_entries:
                        if entry.get("ext") in ("vtt", "srt", "json3"):
                            subtitles_list.append({
                                "lang": sub_lang,
                                "name": entry.get("name", sub_lang),
                                "ext": entry.get("ext", ""),
                                "url": entry.get("url", ""),
                                "auto": sub_lang in auto_subs
                            })
                
                return {
                    "success": True,
                    "title": info.get("title", ""),
                    "duration": info.get("duration_string", ""),
                    "thumbnail": info.get("thumbnail", ""),
                    "uploader": info.get("uploader", ""),
                    "webpage_url": info.get("webpage_url", url),
                    "subtitles_available": subtitles_list,
                    "subtitles_count": len(subtitles_list)
                }
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="获取视频信息超时")
        except json.JSONDecodeError:
            raise HTTPException(status_code=502, detail="解析视频信息失败")
            
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=501, detail="yt-dlp 未安装，请联系管理员")
    except Exception as e:
        logging.error(f"视频字幕提取失败: {e}")
        raise HTTPException(status_code=500, detail=f"字幕提取失败: {str(e)}")

@app.get("/api/video/subtitles/download")
async def download_video_subtitles(
    url: str = Query(..., description="视频链接"),
    lang: str = Query("zh", description="字幕语言"),
    sub_url: str = Query("", description="字幕文件URL（从 /api/video/subtitles 获取）")
):
    """
    下载并解析字幕文件内容
    返回 SRT/VTT 格式的字幕文本
    """
    if not url.strip():
        raise HTTPException(status_code=400, detail="URL 不能为空")
    
    try:
        if sub_url:
            # 直接下载字幕URL
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(sub_url)
                resp.raise_for_status()
                raw_text = resp.text
        else:
            # 使用 yt-dlp 下载字幕
            with tempfile.TemporaryDirectory() as tmpdir:
                cmd = [
                    "yt-dlp",
                    "--skip-download",
                    "--write-subs",
                    "--write-auto-subs",
                    "--sub-lang", lang,
                    "--convert-subs", "srt",
                    "-o", f"{tmpdir}/%(title)s.%(ext)s",
                    "--no-playlist",
                    url
                ]
                
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                )
                
                # 查找生成的字幕文件
                srt_files = []
                for root, dirs, files in os.walk(tmpdir):
                    for f in files:
                        if f.endswith((".srt", ".vtt", ".lrc")):
                            srt_files.append(os.path.join(root, f))
                
                if not srt_files:
                    raise HTTPException(status_code=404, detail=f"未找到 {lang} 字幕")
                
                with open(srt_files[0], "r", encoding="utf-8") as f:
                    raw_text = f.read()
        
        # 解析字幕文本为纯文本（去除时间戳）
        clean_lines = []
        for line in raw_text.split("\n"):
            line = line.strip()
            # 跳过序号、时间戳、空行、WebVTT头部
            if not line or line.isdigit() or "-->" in line or line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
                continue
            # 跳过 HTML 标签
            clean = re.sub(r"<[^>]+>", "", line)
            if clean:
                clean_lines.append(clean)
        
        content = "\n".join(clean_lines)
        truncated = content[:5000] + ("\n\n…(字幕过长已截断)" if len(content) > 5000 else "")
        
        return {
            "success": True,
            "content": truncated,
            "raw_length": len(raw_text),
            "clean_length": len(content)
        }
        
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=501, detail="yt-dlp 未安装")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="下载字幕超时")
    except Exception as e:
        logging.error(f"字幕下载失败: {e}")
        raise HTTPException(status_code=500, detail=f"字幕下载失败: {str(e)}")

# ══════════ Emoji Search API ══════════

@app.get("/api/emoji/search")
async def search_emoji(q: str = Query("", description="搜索关键词"), limit: int = Query(30, ge=1, le=100)):
    """
    搜索 Emoji
    支持中文关键词搜索，如 "笑"、"哭"、"心"、"动物" 等
    """
    if not HAS_EMOJI:
        raise HTTPException(status_code=501, detail="Emoji 功能未启用")
    
    if not q.strip():
        # 返回常用分类
        return {
            "query": "",
            "results": [
                {"emoji": "😀", "name": "笑脸", "category": "表情"},
                {"emoji": "😂", "name": "笑哭", "category": "表情"},
                {"emoji": "❤️", "name": "红心", "category": "符号"},
                {"emoji": "👍", "name": "赞", "category": "手势"},
                {"emoji": "🎉", "name": "庆祝", "category": "活动"},
                {"emoji": "🌟", "name": "星星", "category": "自然"},
                {"emoji": "🔥", "name": "火", "category": "自然"},
                {"emoji": "💯", "name": "一百分", "category": "符号"},
                {"emoji": "😭", "name": "大哭", "category": "表情"},
                {"emoji": "😊", "name": "微笑", "category": "表情"},
                {"emoji": "🥺", "name": "恳求", "category": "表情"},
                {"emoji": "💕", "name": "双心", "category": "符号"},
                {"emoji": "✨", "name": "闪光", "category": "自然"},
                {"emoji": "🥰", "name": "喜爱", "category": "表情"},
                {"emoji": "😅", "name": "尴尬", "category": "表情"},
            ]
        }
    
    q_lower = q.strip().lower()
    results = []
    
    # 使用 emoji 库搜索
    # EMOJI_DATA 包含了所有 emoji 及其描述
    for emoji_char, data in emoji_lib.EMOJI_DATA.items():
        name = data.get("en", "").lower()
        # 也检查中文名称（如果有的话）
        alias_list = [a.lower() for a in data.get("alias", [])]
        
        if q_lower in name or any(q_lower in a for a in alias_list):
            results.append({
                "emoji": emoji_char,
                "name": data.get("en", "").replace("_", " ").title(),
                "aliases": data.get("alias", [])
            })
        
        if len(results) >= limit:
            break
    
    # 如果英文搜索没找到，尝试中文关键词映射
    if not results:
        zh_map = {
            "笑": ["grinning", "smile", "laugh", "joy", "satisfied", "grin", "laughing"],
            "哭": ["cry", "tear", "sob", "sad"],
            "心": ["heart", "love", "kiss"],
            "爱": ["heart", "love", "kiss", "couple"],
            "动物": ["cat", "dog", "animal", "bird", "fish", "rabbit", "bear", "monkey"],
            "花": ["flower", "blossom", "rose", "tulip", "cherry", "sunflower"],
            "吃": ["food", "eat", "drink", "rice", "bread", "meat"],
            "喝": ["drink", "coffee", "tea", "beer", "wine", "cocktail", "beverage"],
            "音乐": ["music", "note", "song", "sound", "instrument"],
            "运动": ["sport", "ball", "game", "run", "swim", "exercise"],
            "交通": ["car", "bus", "train", "plane", "bike", "vehicle", "travel"],
            "天气": ["sun", "rain", "cloud", "snow", "weather", "thunder", "wind"],
            "星星": ["star", "sparkle", "glow"],
            "火": ["fire", "flame", "hot"],
            "钱": ["money", "dollar", "coin", "cash", "bank"],
            "手": ["hand", "wave", "clap", "thumb", "fist", "point"],
            "时间": ["clock", "time", "hour", "watch", "alarm"],
            "书": ["book", "read", "library", "notebook"],
            "手机": ["phone", "mobile", "cell", "iphone"],
            "电脑": ["computer", "laptop", "pc", "desktop"],
            "礼物": ["gift", "present", "box", "ribbon"],
            "太阳": ["sun", "sunny", "sunrise", "sunset"],
            "月亮": ["moon", "crescent", "night"],
            "生气": ["angry", "mad", "rage", "furious", "angry"],
            "惊讶": ["surprise", "shock", "astonish", "amaze"],
            "睡觉": ["sleep", "sleepy", "tired", "yawn", "bed"],
            "赞": ["thumbs", "up", "like", "ok", "good", "great", "approve"],
        }
        
        keywords = zh_map.get(q.strip(), [q_lower])
        for emoji_char, data in emoji_lib.EMOJI_DATA.items():
            name = data.get("en", "").lower()
            if any(kw in name for kw in keywords):
                results.append({
                    "emoji": emoji_char,
                    "name": data.get("en", "").replace("_", " ").title(),
                    "aliases": data.get("alias", [])
                })
            if len(results) >= limit:
                break
    
    return {
        "query": q,
        "results": results,
        "count": len(results)
    }

# ══════════ OCR Handwriting Recognition API ══════════
# 前端 Tesseract.js 离线识别优先，本端点作为降级方案
# 支持：日文假名、汉字、英文字母的手写识别

class OCRRequest(BaseModel):
    image_base64: str  # base64 编码的 PNG/JPEG 图片
    lang_hint: str = "auto"  # 语言提示: ja/en/auto，帮助 Tesseract 选对语言包

@app.post("/api/ocr/recognize")
async def ocr_recognize(req: OCRRequest):
    """
    手写文字 OCR 识别
    使用 pytesseract（系统 Tesseract OCR）进行识别
    前端降级链：Tesseract.js(离线) → 本端点(后端OCR) → AI视觉(最终兜底)
    """
    if not HAS_OCR:
        raise HTTPException(status_code=501, detail="OCR 服务未启用，请安装 pytesseract 和 Pillow")
    
    if not req.image_base64:
        raise HTTPException(status_code=400, detail="图片数据不能为空")
    
    try:
        # 解码 base64 图片
        image_bytes = __import__('base64').b64decode(req.image_base64)
        image = Image.open(io.BytesIO(image_bytes))
        
        # 预处理：放大图片（手写通常较小）、转灰度、增强对比度
        w, h = image.size
        if w < 200 or h < 80:
            image = image.resize((w * 2, h * 2), Image.LANCZOS)
        
        # 转灰度 + 二值化增强手写笔迹
        image = image.convert('L')
        # 自适应阈值二值化
        import numpy as np
        img_array = np.array(image)
        threshold = np.mean(img_array) * 0.85
        image = Image.fromarray((img_array < threshold).astype(np.uint8) * 255)
        
        # 选择语言包
        lang_map = {"ja": "jpn", "en": "eng", "auto": "eng+jpn"}
        lang = lang_map.get(req.lang_hint, "eng+jpn")
        
        # OCR 识别
        text = pytesseract.image_to_string(image, lang=lang, config='--psm 7 -c tessedit_char_whitelist=')
        text = text.strip()
        
        if not text:
            return {"success": False, "text": "", "message": "未能识别到文字"}
        
        return {"success": True, "text": text, "engine": "pytesseract"}
        
    except Exception as e:
        logging.error(f"OCR 识别失败: {e}")
        raise HTTPException(status_code=500, detail=f"OCR 识别失败: {str(e)}")


# ══════════ Dictionary API (Free Dictionary + Jisho) ══════════

@app.get("/api/dictionary/{word}")
async def lookup_word(word: str, lang: str = Query("en", description="语言: en/ja")):
    """
    词典查询
    - 英语: 使用 Free Dictionary API (https://api.dictionaryapi.dev)
    - 日语: 使用 Jisho.org API (https://jisho.org)
    """
    word = word.strip()
    if not word:
        raise HTTPException(status_code=400, detail="请提供要查询的单词")
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if lang == "ja":
                # Jisho API for Japanese
                resp = await client.get(
                    f"https://jisho.org/api/v1/search/words",
                    params={"keyword": word}
                )
                if resp.status_code != 200:
                    raise HTTPException(status_code=502, detail="日语词典服务不可用")
                
                data = resp.json()
                entries = []
                for item in (data.get("data") or [])[:3]:
                    jp = item.get("japanese", [{}])[0]
                    senses = item.get("senses", [{}])[0]
                    entries.append({
                        "word": jp.get("word", jp.get("reading", word)),
                        "reading": jp.get("reading", ""),
                        "definitions": senses.get("english_definitions", []),
                        "pos": "/".join(senses.get("parts_of_speech", [])),
                    })
                
                return {
                    "word": word,
                    "lang": "ja",
                    "entries": entries,
                    "source": "jisho.org"
                }
            else:
                # Free Dictionary API for English
                resp = await client.get(
                    f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
                )
                if resp.status_code == 404:
                    return {"word": word, "lang": "en", "entries": [], "source": "dictionaryapi.dev", "message": "未找到该词"}
                if resp.status_code != 200:
                    raise HTTPException(status_code=502, detail="英语词典服务不可用")
                
                data = resp.json()
                entries = []
                for item in data[:2]:
                    meanings = []
                    for m in item.get("meanings", []):
                        defs = []
                        for d in m.get("definitions", [])[:3]:
                            defs.append({
                                "definition": d.get("definition", ""),
                                "example": d.get("example", "")
                            })
                        meanings.append({
                            "partOfSpeech": m.get("partOfSpeech", ""),
                            "definitions": defs
                        })
                    
                    # 音标
                    phonetics = []
                    for p in item.get("phonetics", [])[:2]:
                        phonetics.append({
                            "text": p.get("text", ""),
                            "audio": p.get("audio", "")
                        })
                    
                    entries.append({
                        "word": item.get("word", word),
                        "phonetics": phonetics,
                        "meanings": meanings
                    })
                
                return {
                    "word": word,
                    "lang": "en",
                    "entries": entries,
                    "source": "dictionaryapi.dev"
                }
                
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="词典服务不可达")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"词典查询失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>椿岛 - 管理后台</title>
<style>
:root{--bg:#0f1117;--card-bg:#1a1d27;--border:#2a2d3a;--text:#e4e6eb;--text2:#9ca3af;--accent:#6366f1;--accent-hover:#818cf8;--success:#22c55e;--danger:#ef4444;--warning:#f59e0b}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);min-height:100vh;line-height:1.6}
.container{max-width:1200px;margin:0 auto;padding:24px 20px}
.tab-bar{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:24px}
.tab-btn{padding:12px 24px;border:none;background:none;color:var(--text2);font-size:14px;font-weight:500;cursor:pointer;font-family:inherit;border-bottom:2px solid transparent;transition:all .2s}
.tab-btn.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-btn:hover{color:var(--text)}
.tab-content{display:none}
.tab-content.active{display:block}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:16px}
header h1{font-size:24px;font-weight:600;letter-spacing:.04em}
header .subtitle{font-size:13px;color:var(--text2);margin-top:4px;letter-spacing:.06em}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px}
.stat-card{background:var(--card-bg);border:1px solid var(--border);border-radius:14px;padding:20px 24px}
.stat-card .stat-label{font-size:12px;color:var(--text2);letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px}
.stat-card .stat-value{font-size:36px;font-weight:700;letter-spacing:-.02em}
.stat-card.total .stat-value{color:var(--accent)}
.stat-card.activated .stat-value{color:var(--success)}
.stat-card.unactivated .stat-value{color:var(--warning)}
.section{background:var(--card-bg);border:1px solid var(--border);border-radius:14px;padding:24px;margin-bottom:24px}
.section-title{font-size:16px;font-weight:600;margin-bottom:20px;letter-spacing:.03em}
.form-row{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-bottom:16px}
.form-group{display:flex;flex-direction:column;gap:6px}
.form-group label{font-size:12px;color:var(--text2);letter-spacing:.05em}
.form-group input,.form-group textarea,.form-group select{background:#0f1117;border:1px solid var(--border);border-radius:10px;padding:10px 14px;font-size:14px;color:var(--text);outline:none;font-family:inherit;transition:border-color .2s}
.form-group input:focus,.form-group textarea:focus,.form-group select:focus{border-color:var(--accent)}
.form-group textarea{min-height:80px;resize:vertical}
.btn{padding:10px 20px;border:none;border-radius:10px;font-size:14px;font-weight:500;cursor:pointer;font-family:inherit;letter-spacing:.03em;transition:all .2s}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:var(--accent-hover)}
.btn-danger{background:transparent;color:var(--danger);border:1px solid var(--danger)}
.btn-danger:hover{background:var(--danger);color:#fff}
.btn-success{background:var(--success);color:#fff}
.btn-success:hover{opacity:.8}
.btn-sm{padding:6px 14px;font-size:12px;border-radius:8px}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text2)}
.btn-outline:hover{border-color:var(--accent);color:var(--accent)}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.toolbar input{flex:1;min-width:200px;background:#0f1117;border:1px solid var(--border);border-radius:10px;padding:10px 14px;font-size:14px;color:var(--text);outline:none;font-family:inherit}
.toolbar input:focus{border-color:var(--accent)}
.toolbar select{background:#0f1117;border:1px solid var(--border);border-radius:10px;padding:10px 14px;font-size:14px;color:var(--text);outline:none;cursor:pointer;font-family:inherit}
table{width:100%;border-collapse:collapse}
thead th{text-align:left;padding:12px 14px;font-size:12px;color:var(--text2);letter-spacing:.06em;text-transform:uppercase;border-bottom:1px solid var(--border)}
tbody td{padding:14px;border-bottom:1px solid var(--border);font-size:14px}
tbody tr:hover{background:rgba(99,102,241,.04)}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:500;letter-spacing:.03em}
.badge-active{background:rgba(34,197,94,.15);color:var(--success)}
.badge-inactive{background:rgba(245,158,11,.15);color:var(--warning)}
.badge-pending{background:rgba(99,102,241,.15);color:var(--accent)}
.badge-rejected{background:rgba(239,68,68,.15);color:var(--danger)}
.code-text{font-family:"SF Mono",Menlo,monospace;font-size:13px;letter-spacing:.05em}
.empty-row{text-align:center;padding:40px 20px;color:var(--text2);font-size:14px}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;align-items:center;justify-content:center}
.modal.open{display:flex}
.modal-box{background:var(--card-bg);border:1px solid var(--border);border-radius:16px;width:min(900px,95vw);max-height:85vh;overflow-y:auto;padding:24px;position:relative}
.modal-box h3{font-size:18px;margin-bottom:16px}
.modal-close{position:absolute;top:16px;right:16px;background:none;border:none;color:var(--text2);font-size:20px;cursor:pointer}
.preview-frame{width:100%;height:500px;border:1px solid var(--border);border-radius:10px;background:#fff;overflow:hidden}
.source-code-box{background:#0f1117;border:1px solid var(--border);border-radius:10px;padding:16px;font-family:"SF Mono",Menlo,monospace;font-size:12px;color:var(--text2);white-space:pre-wrap;max-height:300px;overflow-y:auto;margin:12px 0}
.review-actions{display:flex;gap:12px;margin-top:16px}
.review-textarea{width:100%;min-height:80px;margin-top:12px}
.toast{position:fixed;top:20px;right:20px;padding:12px 20px;border-radius:10px;font-size:14px;z-index:999;animation:toastIn .3s ease;box-shadow:0 8px 24px rgba(0,0,0,.4)}
.toast-success{background:var(--success);color:#fff}
.toast-error{background:var(--danger);color:#fff}
@keyframes toastIn{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}
@media(max-width:640px){.container{padding:16px 12px}header h1{font-size:20px}.stat-card .stat-value{font-size:28px}.form-row{flex-direction:column}table{font-size:12px}.preview-frame{height:350px}}
</style>
</head>
<body>
<div class="container">
<div class="tab-bar">
  <button class="tab-btn active" onclick="switchTab('codes')">测试码管理</button>
  <button class="tab-btn" onclick="switchTab('store')">应用审核</button>
</div>

<div class="tab-content active" id="tab-codes">
<header>
  <div>
    <h1>椿岛测试码管理</h1>
    <div class="subtitle">Invite Code Dashboard</div>
  </div>
</header>

<div class="stats-grid">
  <div class="stat-card total"><div class="stat-label">总数</div><div class="stat-value" id="stat-total">0</div></div>
  <div class="stat-card activated"><div class="stat-label">已激活</div><div class="stat-value" id="stat-activated">0</div></div>
  <div class="stat-card unactivated"><div class="stat-label">未激活</div><div class="stat-value" id="stat-unactivated">0</div></div>
</div>

<div class="section">
  <div class="section-title">生成测试码</div>
  <div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-bottom:16px">
    <div class="form-group"><label>生成数量 (自动)</label><input type="number" id="gen-count" value="1" min="1" max="500" style="width:120px"></div>
    <div class="form-group"><label>前缀 (可选)</label><input type="text" id="gen-prefix" placeholder="如 CHUNDAO-" style="width:180px"></div>
    <div class="form-group"><label>备注 (可选)</label><input type="text" id="gen-notes" placeholder="批次备注" style="width:200px"></div>
    <button class="btn btn-primary" onclick="generateCodes()">自动生成</button>
  </div>
  <div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
    <div class="form-group" style="flex:1;min-width:300px"><label>手动输入 (每行一个, 或用逗号/空格分隔)</label><textarea id="gen-manual" placeholder="AAAA-BBBB-CCCC&#10;DDDD-EEEE-FFFF"></textarea></div>
    <button class="btn btn-primary" onclick="generateManual()">手动添加</button>
  </div>
</div>

<div class="section">
  <div class="section-title">测试码列表</div>
  <div class="toolbar">
    <input type="text" id="search-input" placeholder="搜索测试码..." oninput="loadCodes()">
    <select id="status-filter" onchange="loadCodes()"><option value="all">全部</option><option value="activated">已激活</option><option value="unactivated">未激活</option></select>
    <button class="btn btn-outline" onclick="refreshAll()">刷新</button>
  </div>
  <div style="overflow-x:auto">
  <table><thead><tr><th>测试码</th><th>状态</th><th>备注</th><th>创建时间</th><th>激活时间</th><th>操作</th></tr></thead><tbody id="codes-tbody"></tbody></table>
  </div>
</div>
</div>

<div class="tab-content" id="tab-store">
<header>
  <div><h1>应用商店审核</h1><div class="subtitle">App Review Dashboard</div></div>
  <button class="btn btn-outline btn-sm" onclick="loadStoreApps()">刷新</button>
</header>
<div class="section">
  <div class="section-title">待审核应用</div>
  <div style="overflow-x:auto">
  <table><thead><tr><th>ID</th><th>图标</th><th>名称</th><th>分类</th><th>作者</th><th>状态</th><th>提交时间</th><th>操作</th></tr></thead><tbody id="store-tbody"></tbody></table>
  </div>
</div>
</div>
</div>

<div class="modal" id="review-modal">
  <div class="modal-box">
    <button class="modal-close" onclick="closeReviewModal()">&times;</button>
    <h3 id="rm-title">审核应用</h3>
    <div style="display:flex;gap:16px;flex-wrap:wrap">
      <div style="flex:1;min-width:300px">
        <div class="form-group" style="margin-bottom:12px"><label>应用名称</label><div id="rm-name" style="padding:8px 0;font-size:16px;font-weight:600">-</div></div>
        <div class="form-group" style="margin-bottom:12px"><label>分类</label><div id="rm-cat" style="padding:4px 0">-</div></div>
        <div class="form-group" style="margin-bottom:12px"><label>作者</label><div id="rm-author" style="padding:4px 0">-</div></div>
        <div class="form-group" style="margin-bottom:12px"><label>描述</label><div id="rm-desc" style="padding:4px 0;color:var(--text2)">-</div></div>
      </div>
      <div style="flex:1;min-width:300px">
        <div class="form-group" style="margin-bottom:12px"><label>预览</label><div class="preview-frame"><iframe id="rm-preview" style="width:390px;height:100%;border:none;display:block;margin:0 auto" sandbox="allow-scripts"></iframe></div></div>
      </div>
    </div>
    <div class="form-group" style="margin-top:12px"><label>源代码</label><div class="source-code-box" id="rm-source">-</div></div>
    <div class="review-actions" id="rm-actions">
      <textarea class="review-textarea" id="rm-notes" placeholder="审核备注（通过可选，拒绝必填）"></textarea>
      <button class="btn btn-success" onclick="approveApp()">通过审核</button>
      <button class="btn btn-danger" onclick="rejectApp()">拒绝</button>
    </div>
  </div>
</div>

<div id="toast" class="toast" style="display:none"></div>

<script>
const API = '/api';
let _currentReviewId = null;

function switchTab(tab){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.textContent.includes(tab==='codes'?'测试码':'审核')));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
  document.getElementById('tab-'+tab).classList.add('active');
  if(tab==='codes') refreshAll(); else loadStoreApps();
}

function toast(msg, type){
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = 'toast toast-' + type;
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 2500);
}

// ── Codes ──
async function loadStats(){
  const r = await fetch(API + '/stats'); const d = await r.json();
  document.getElementById('stat-total').textContent = d.total;
  document.getElementById('stat-activated').textContent = d.activated;
  document.getElementById('stat-unactivated').textContent = d.unactivated;
}
async function loadCodes(){
  const status = document.getElementById('status-filter').value;
  const search = document.getElementById('search-input').value;
  const r = await fetch(API + '/codes?status=' + status + '&search=' + encodeURIComponent(search));
  const d = await r.json(); const tbody = document.getElementById('codes-tbody');
  if(d.codes.length === 0){ tbody.innerHTML = '<tr><td colspan="6" class="empty-row">暂无数据</td></tr>'; }
  else { tbody.innerHTML = d.codes.map(c => `<tr><td><span class="code-text">${esc(c.code)}</span></td><td>${c.is_activated?'<span class="badge badge-active">已激活</span>':'<span class="badge badge-inactive">未激活</span>'}</td><td style="color:var(--text2);font-size:12px">${esc(c.notes||'-')}</td><td style="color:var(--text2);font-size:12px">${c.created_at}</td><td style="color:var(--text2);font-size:12px">${c.activated_at||'-'}</td><td><button class="btn btn-sm btn-danger" onclick="deleteCode('${esc(c.code)}')">删除</button></td></tr>`).join(''); }
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
async function refreshAll(){ await loadStats(); await loadCodes(); }
async function generateCodes(){
  const count = parseInt(document.getElementById('gen-count').value)||1;
  const prefix = document.getElementById('gen-prefix').value.trim();
  const notes = document.getElementById('gen-notes').value.trim();
  const r = await fetch(API+'/codes/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count,prefix,notes})});
  const d = await r.json();
  if(d.success){toast('成功生成 '+d.count+' 个测试码','success');refreshAll()}else{toast('生成失败','error')}
}
async function generateManual(){
  const text = document.getElementById('gen-manual').value.trim();
  if(!text){toast('请输入测试码','error');return}
  const codes = text.split(/[\\n,\\s]+/).filter(Boolean);
  const r = await fetch(API+'/codes/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({codes})});
  const d = await r.json();
  if(d.success){toast('成功添加 '+d.count+' 个测试码','success');document.getElementById('gen-manual').value='';refreshAll()}else{toast('添加失败','error')}
}
async function deleteCode(code){
  if(!confirm('确定删除测试码 '+code+' ?'))return;
  const r = await fetch(API+'/codes/'+encodeURIComponent(code),{method:'DELETE'});
  if(r.ok){toast('已删除','success');refreshAll()}else{toast('删除失败','error')}
}

// ── Store Review ──
async function loadStoreApps(){
  const r = await fetch(API+'/store/apps/review'); const d = await r.json();
  const tbody = document.getElementById('store-tbody');
  if(d.apps.length===0){ tbody.innerHTML = '<tr><td colspan="8" class="empty-row">暂无提交</td></tr>'; return; }
  const labels = {pending:'待审核',approved:'已通过',rejected:'已拒绝'};
  const classes = {pending:'badge-pending',approved:'badge-active',rejected:'badge-rejected'};
  tbody.innerHTML = d.apps.map(a => `<tr><td>#${a.id}</td><td style="font-size:22px">${esc(a.icon_emoji)}</td><td><b>${esc(a.name)}</b></td><td style="color:var(--text2)">${esc(a.category)}</td><td style="color:var(--text2)">${esc(a.author_name)}</td><td><span class="badge ${classes[a.status]}">${labels[a.status]||a.status}</span></td><td style="color:var(--text2);font-size:12px">${a.created_at}</td><td><button class="btn btn-sm btn-outline" onclick="openReview(${a.id})">查看</button> <button class="btn btn-sm btn-danger" onclick="deleteStoreApp(${a.id})">删除</button></td></tr>`).join('');
}
async function openReview(id){
  const r = await fetch(API+'/store/apps/'+id); const a = await r.json();
  _currentReviewId = id;
  document.getElementById('rm-title').textContent = '审核 #'+id+' - '+a.name;
  document.getElementById('rm-name').textContent = a.name;
  document.getElementById('rm-cat').textContent = a.category;
  document.getElementById('rm-author').textContent = a.author_name + ' (' + a.author_id + ')';
  document.getElementById('rm-desc').textContent = a.full_description || a.description;
  document.getElementById('rm-source').textContent = a.source_code || '(无源代码)';
  var iframe = document.getElementById('rm-preview');
  iframe.srcdoc = a.source_code || '<div style="padding:40px;text-align:center;color:#aaa">无源代码</div>';
  document.getElementById('rm-notes').value = a.review_notes || '';
  var actions = document.getElementById('rm-actions');
  if(a.status==='pending'){ actions.style.display = 'block'; }
  else { actions.style.display = 'none'; }
  document.getElementById('review-modal').classList.add('open');
}
function closeReviewModal(){ document.getElementById('review-modal').classList.remove('open'); _currentReviewId = null; }
async function approveApp(){
  if(!_currentReviewId) return;
  const notes = document.getElementById('rm-notes').value;
  const r = await fetch(API+'/store/apps/'+_currentReviewId+'/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({review_notes:notes})});
  if(r.ok){ toast('已通过审核','success'); closeReviewModal(); loadStoreApps(); } else { toast('操作失败','error'); }
}
async function rejectApp(){
  if(!_currentReviewId) return;
  const notes = document.getElementById('rm-notes').value.trim();
  if(!notes){ toast('请填写拒绝原因','error'); return; }
  const r = await fetch(API+'/store/apps/'+_currentReviewId+'/reject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({review_notes:notes})});
  if(r.ok){ toast('已拒绝','success'); closeReviewModal(); loadStoreApps(); } else { toast('操作失败','error'); }
}
async function deleteStoreApp(id){
  if(!confirm('确定删除应用 #'+id+' ?')) return;
  const r = await fetch(API+'/store/apps/'+id,{method:'DELETE'});
  if(r.ok){ toast('已删除','success'); loadStoreApps(); } else { toast('删除失败','error'); }
}

refreshAll();
</script>
</body>
</html>"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
