#!/usr/bin/env python3
"""
保存美团外卖登录态
运行此脚本 -> 手动扫码/登录 -> 自动保存 Session 到 auth_meituan.json
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

AUTH_FILE = Path(__file__).resolve().parent / "auth_meituan.json"


async def main():
    print("=" * 50)
    print("🍱 美团外卖 MCP - 登录态保存工具")
    print("=" * 50)
    print()
    print("即将打开浏览器，请在浏览器中完成登录（扫码/手机号）。")
    print("登录成功后，Session 将自动保存到 auth_meituan.json")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        device = p.devices["iPhone 13"]
        context = await browser.new_context(
            **device,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            permissions=["geolocation"],
            geolocation={"longitude": 121.4737, "latitude": 31.2304},
        )
        page = await context.new_page()

        # 打开美团外卖 H5
        await page.goto(
            "https://h5.waimai.meituan.com/",
            wait_until="networkidle",
            timeout=30000,
        )

        print("✅ 浏览器已打开，请在浏览器窗口中完成登录...")
        print("💡 提示：登录完成后回到终端按 Enter 保存 Session")
        print()

        # 等待用户确认
        input("👉 登录完成后按 Enter 键继续...")

        # 保存登录态
        await context.storage_state(path=str(AUTH_FILE))
        print(f"✅ 登录态已保存到: {AUTH_FILE}")
        print(f"📦 文件大小: {AUTH_FILE.stat().st_size} bytes")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())