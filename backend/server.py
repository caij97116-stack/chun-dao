import sqlite3
import secrets
import string
import os
import json
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import contextmanager

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

init_db()

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

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
