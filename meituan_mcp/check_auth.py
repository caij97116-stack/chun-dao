#!/usr/bin/env python3
"""
验证美团外卖登录态是否有效
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

AUTH_FILE = Path(__file__).resolve().parent / "auth_meituan.json"


async def main():
    if not AUTH_FILE.exists():
        print("❌ 未找到 auth_meituan.json，请先运行 save_auth.py 保存登录态")
        return

    print("=" * 50)
    print("🍱 美团外卖 MCP - 登录态验证")
    print("=" * 50)
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        device = p.devices["iPhone 13"]
        context = await browser.new_context(
            **device,
            storage_state=str(AUTH_FILE),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = await context.new_page()

        try:
            await page.goto(
                "https://h5.waimai.meituan.com/",
                wait_until="networkidle",
                timeout=20000,
            )

            # 检查页面是否需要登录
            content = await page.content()
            if "登录" in content and "手机号登录" in content:
                print("❌ 登录态已过期，请重新运行 save_auth.py")
            else:
                title = await page.title()
                url = page.url
                print(f"✅ 登录态有效！")
                print(f"📄 页面标题: {title}")
                print(f"📍 URL: {url}")

            # 打印 cookies 信息
            cookies = await context.cookies()
            print(f"🍪 Cookies 数量: {len(cookies)}")
            for c in cookies[:5]:
                print(f"   - {c['name']}: {c['domain']}")

        except Exception as e:
            print(f"❌ 验证失败: {e}")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
