#!/usr/bin/env python3
"""
🍱 Meituan-MCP-Server — 基于 MCP 协议的美团外卖自动化助手
通过 Playwright 驱动浏览器，让大模型能够搜索商家、浏览菜单、加购下单。
"""

import asyncio
import os
import sys
import json
import random
import re
from pathlib import Path

from mcp.server import Server
from mcp.types import Tool, TextContent
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# ── 路径初始化 ──────────────────────────────────────────────
CURRENT_DIR = Path(__file__).resolve().parent
AUTH_FILE = CURRENT_DIR / "auth_meituan.json"

server = Server("meituan-mcp")

# ── 浏览器状态管理 ───────────────────────────────────────────
class BrowserState:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.remembered_shops: list[str] = []
        self.cart_items: list[str] = []
        self.current_page_label = "未知"

state = BrowserState()

# ── 浏览器工具函数 ───────────────────────────────────────────

async def show_click_feedback(page, x, y):
    """点击时显示红色圆点反馈"""
    await page.evaluate(f'''
        () => {{
            const dot = document.createElement('div');
            dot.style.cssText = `
                position:fixed; left:{x}px; top:{y}px;
                width:30px; height:30px;
                background:rgba(255,0,0,0.7); border-radius:50%;
                border:3px solid #FFD700; pointer-events:none;
                z-index:999999; transform:translate(-50%,-50%);
                box-shadow:0 0 15px rgba(255,0,0,0.8);
            `;
            document.body.appendChild(dot);
            setTimeout(() => dot.remove(), 1500);
        }}
    ''')
    await asyncio.sleep(1.5)


async def human_smooth_scroll(page, distance):
    """模拟人类滚动行为"""
    scrolled = 0
    is_down = distance > 0
    abs_dist = abs(distance)
    while scrolled < abs_dist:
        step = random.randint(300, 700)
        if scrolled + step > abs_dist:
            step = abs_dist - scrolled
        await page.mouse.wheel(0, step if is_down else -step)
        scrolled += step
        await asyncio.sleep(random.uniform(0.04, 0.08))
    await asyncio.sleep(1.5)


async def ensure_browser():
    """确保浏览器已启动并登录"""
    if state.page is None:
        state.playwright = await async_playwright().start()
        state.browser = await state.playwright.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )

        # 加载持久化登录态
        storage_state = str(AUTH_FILE) if AUTH_FILE.exists() else None

        # 使用 iPhone 13 设备模拟移动端
        device = state.playwright.devices["iPhone 13"]
        state.context = await state.browser.new_context(
            **device,
            storage_state=storage_state,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            permissions=["geolocation"],
            geolocation={"longitude": 121.4737, "latitude": 31.2304},
        )
        state.page = await state.context.new_page()

        try:
            await state.page.goto(
                "https://h5.waimai.meituan.com/",
                wait_until="networkidle",
                timeout=30000,
            )
        except Exception:
            pass


def extract_items_from_html(html: str) -> list[str]:
    """从 HTML 中提取商家名或菜品名"""
    soup = BeautifulSoup(html, "html.parser")
    selectors = [
        ".shop-name", ".wm-item-title", "h3",
        'div[class*="name"]', ".food-name", 'span[class*="name"]',
        ".restaurant-name", ".store-name",
    ]
    raw_list = []
    for s in selectors:
        for tag in soup.select(s):
            txt = tag.get_text().strip()
            if txt and len(txt) > 1 and not any(
                x in txt for x in ["搜索", "配送", "评价", "月售", "公告", "起送", "人均"]
            ):
                raw_list.append(txt)
    return list(dict.fromkeys(raw_list))


def detect_page_mode(html: str) -> str:
    """检测当前页面是商户列表还是菜单页"""
    soup = BeautifulSoup(html, "html.parser")
    has_add_btn = soup.find(attrs={"aria-label": "增加"}) is not None
    has_cart = soup.select_one('[class*="cart"], .shopping-cart') is not None
    has_spec = soup.select_one('[class*="spec"], [class*="sku"]') is not None
    if has_add_btn or has_cart or has_spec:
        return "menu"
    return "shop_list"


# ── MCP 工具定义 ─────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="fetch_meituan_content",
            description="获取当前页面内容。自动识别商家列表或菜单模式，返回清洗后的列表。",
            inputSchema={"type": "object"},
        ),
        Tool(
            name="smart_scroll",
            description="模拟人类滑动页面。正数向下滑，负数向上滑。",
            inputSchema={
                "type": "object",
                "properties": {"distance": {"type": "integer", "description": "滑动像素距离"}},
                "required": ["distance"],
            },
        ),
        Tool(
            name="click_target",
            description="根据文本点击目标（商家名/菜品名），进入商店或详情页。",
            inputSchema={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要点击的文本"}},
                "required": ["text"],
            },
        ),
        Tool(
            name="add_food_to_cart",
            description="点击菜品名进入详情页并加购到购物车。",
            inputSchema={
                "type": "object",
                "properties": {"food_name": {"type": "string", "description": "菜品名称"}},
                "required": ["food_name"],
            },
        ),
        Tool(
            name="search_meituan",
            description="在美团外卖搜索框搜索商家或菜品。",
            inputSchema={
                "type": "object",
                "properties": {"keyword": {"type": "string", "description": "搜索关键词"}},
                "required": ["keyword"],
            },
        ),
        Tool(
            name="go_back",
            description="返回上一页（从菜单返回商户列表）。",
            inputSchema={"type": "object"},
        ),
        Tool(
            name="view_cart",
            description="查看当前购物车内容。",
            inputSchema={"type": "object"},
        ),
        Tool(
            name="get_page_url",
            description="获取当前页面 URL 和标题。",
            inputSchema={"type": "object"},
        ),
    ]


# ── MCP 工具实现 ─────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    await ensure_browser()

    if name == "fetch_meituan_content":
        await asyncio.sleep(1.5)
        content = await state.page.content()
        mode = detect_page_mode(content)
        items = extract_items_from_html(content)

        if mode == "menu":
            state.current_page_label = "🍴 菜单模式"
            # 从菜品列表中过滤掉商户名
            final_items = [x for x in items if x not in state.remembered_shops]
        else:
            state.current_page_label = "🏠 商户列表"
            final_items = items
            state.remembered_shops = items

        res = (
            f"📊 状态：【{state.current_page_label}】\n"
            f"📋 列表内容 ({len(final_items)} 项)：\n"
            + "\n".join([f"  [{i}] {s}" for i, s in enumerate(final_items)])
        )
        return [TextContent(type="text", text=res)]

    elif name == "smart_scroll":
        await human_smooth_scroll(state.page, arguments["distance"])
        return [TextContent(type="text", text="✅ 滑动完成")]

    elif name == "click_target":
        text = arguments["text"]
        target = state.page.get_by_text(text).last
        if await target.is_visible():
            box = await target.bounding_box()
            if box:
                await show_click_feedback(
                    state.page,
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
            await target.click()
            await asyncio.sleep(3)
            return [TextContent(type="text", text=f"🔴 已进入：{text}")]
        return [TextContent(type="text", text=f"❌ 找不到目标：{text}")]

    elif name == "add_food_to_cart":
        food_name = arguments["food_name"]
        target = state.page.get_by_text(food_name).last
        if await target.is_visible():
            box = await target.bounding_box()
            if box:
                await show_click_feedback(
                    state.page,
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
                await target.click()
                await asyncio.sleep(2.5)

                # 详情页中依次点击加购按钮
                for btn_txt in ["加入购物车", "选好了", "确定", "加入", "+"]:
                    btn = state.page.get_by_text(btn_txt).last
                    if await btn.is_visible():
                        b_box = await btn.bounding_box()
                        if b_box:
                            await show_click_feedback(
                                state.page,
                                b_box["x"] + b_box["width"] / 2,
                                b_box["y"] + b_box["height"] / 2,
                            )
                        await btn.click()
                        await asyncio.sleep(1)
                        state.cart_items.append(food_name)
                        return [
                            TextContent(
                                type="text",
                                text=f"✅ 已加购：{food_name}\n🛒 购物车共 {len(state.cart_items)} 件",
                            )
                        ]
        return [TextContent(type="text", text=f"❌ 操作失败，请确保菜品可见")]

    elif name == "search_meituan":
        keyword = arguments["keyword"]
        # 尝试找到搜索框
        search_selectors = [
            'input[type="search"]',
            'input[placeholder*="搜索"]',
            ".search-input input",
            '[class*="search"] input',
        ]
        for sel in search_selectors:
            try:
                search_box = state.page.locator(sel).first
                if await search_box.is_visible():
                    await search_box.click()
                    await asyncio.sleep(0.5)
                    await search_box.fill(keyword)
                    await asyncio.sleep(0.5)
                    await state.page.keyboard.press("Enter")
                    await asyncio.sleep(3)
                    return [TextContent(type="text", text=f"🔍 已搜索：{keyword}")]
            except Exception:
                continue

        # 备用方案：直接导航到搜索 URL
        try:
            encoded = keyword
            await state.page.goto(
                f"https://h5.waimai.meituan.com/waimai/mindex/search?query={encoded}",
                wait_until="networkidle",
                timeout=15000,
            )
            await asyncio.sleep(3)
            return [TextContent(type="text", text=f"🔍 已搜索：{keyword}")]
        except Exception as e:
            return [TextContent(type="text", text=f"❌ 搜索失败：{e}")]

    elif name == "go_back":
        try:
            await state.page.go_back()
            await asyncio.sleep(2)
            state.remembered_shops = []
            return [TextContent(type="text", text="⬅️ 已返回上一页")]
        except Exception as e:
            return [TextContent(type="text", text=f"❌ 返回失败：{e}")]

    elif name == "view_cart":
        if not state.cart_items:
            return [TextContent(type="text", text="🛒 购物车为空")]
        cart_list = "\n".join(
            [f"  [{i}] {item}" for i, item in enumerate(state.cart_items)]
        )
        return [
            TextContent(
                type="text",
                text=f"🛒 购物车 ({len(state.cart_items)} 件)：\n{cart_list}",
            )
        ]

    elif name == "get_page_url":
        url = state.page.url
        title = await state.page.title()
        return [
            TextContent(
                type="text",
                text=f"📍 URL: {url}\n📄 标题: {title}\n🏷️ 模式: {state.current_page_label}",
            )
        ]

    raise ValueError(f"Unknown tool: {name}")


# ── 入口 ─────────────────────────────────────────────────────

async def main():
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
