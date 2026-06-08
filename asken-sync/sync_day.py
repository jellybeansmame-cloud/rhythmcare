#!/usr/bin/env python3
"""
あすけんから1日分のデータを取得し、リズムケア用JSONを出力する。

使い方:
  .\\start-chrome.ps1   # ログイン済みChromeを起動
  python sync_day.py --connect --date 2026-06-07

出力: asken-sync/export/2026-06-07.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ASKEN_BASE = "https://www.asken.jp"
EXPORT_DIR = Path(__file__).parent / "export"

MEAL_KEYS = ("breakfast", "lunch", "dinner", "sweets")
MEAL_LABELS = {
    "breakfast": "朝食",
    "lunch": "昼食",
    "dinner": "夕食",
    "sweets": "間食",
}


def parse_eat_datas(html: str) -> list[dict]:
    match = re.search(r"V2WspMeal\.eatDatas\s*=\s*(\{.*?\});\s*", html, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    items = []
    for entry in data.values():
        items.append(
            {
                "name": entry.get("menu_name", ""),
                "quantity": str(entry.get("menu_quantity", "1")),
                "kcal": str(entry.get("energy", "")),
            }
        )
    return items


def parse_comment_body(html: str) -> dict:
    result: dict = {}

    def field(name: str, key: str) -> None:
        pattern = rf'name="data\[Body\]\[{name}\]"[^>]*value="([^"]*)"'
        m = re.search(pattern, html)
        if m and m.group(1).strip():
            result[key] = m.group(1).strip()

    field("weight", "weight")
    field("body_fat", "body_fat")
    field("hosu", "steps")
    return result


def parse_calendar_row(html: str, day: int) -> dict:
    result: dict = {}
    rows = re.findall(
        r'<div class="calendar_body_\d+">(.*?)</div>\s*<div class="calendar_border">',
        html,
        re.DOTALL,
    )
    for row in rows:
        day_match = re.search(r'class="val_date">\s*(\d+)\(', row)
        if not day_match or int(day_match.group(1)) != day:
            continue

        bowel_html = re.search(r'class="val_bowel">(.*?)</div>', row, re.DOTALL)
        if bowel_html:
            chunk = bowel_html.group(1)
            if "bowel_ok" in chunk:
                result["bowel"] = "yes"
            elif "bowel_ng" in chunk:
                result["bowel"] = "no"

        phys_html = re.search(r'class="val_physiology">(.*?)</div>', row, re.DOTALL)
        if phys_html and "icon_heart" in phys_html.group(1):
            result["physiology"] = True
        else:
            result["physiology"] = False

        weight_match = re.search(
            r'class="val_weight">\s*(?:<span[^>]*>)?\s*([\d.]+)\s*kg',
            row,
        )
        if weight_match:
            result["weight"] = weight_match.group(1)

        fat_match = re.search(
            r'class="val_bodyfat">\s*(?:<span[^>]*>)?\s*([\d.]+)\s*%',
            row,
        )
        if fat_match:
            result["body_fat"] = fat_match.group(1)
        break

    return result


def fetch_meals(page: Page, target_date: str) -> dict:
    meals: dict[str, list] = {}
    total_kcal = 0.0
    for key in MEAL_KEYS:
        url = f"{ASKEN_BASE}/wsp/meal/{key}/{target_date}"
        page.goto(url, wait_until="networkidle", timeout=60_000)
        time.sleep(0.5)
        items = parse_eat_datas(page.content())
        meals[key] = items
        for item in items:
            try:
                total_kcal += float(item.get("kcal") or 0)
            except ValueError:
                pass
    return {
        "meals": meals,
        "meal_total_kcal": int(total_kcal) if total_kcal else None,
    }


def normalize_weight(val: str | None) -> str | None:
    if not val:
        return None
    match = re.search(r"[\d.]+", str(val).replace(",", ""))
    return match.group(0) if match else None


def fetch_body(page: Page, target_date: str) -> dict:
    year, month, day = (int(x) for x in target_date.split("-"))
    body: dict = {}

    comment_url = f"{ASKEN_BASE}/wsp/comment/{target_date}"
    page.goto(comment_url, wait_until="networkidle", timeout=60_000)
    time.sleep(0.3)
    body.update(parse_comment_body(page.content()))

    cal_url = f"{ASKEN_BASE}/my_diary/view_calendar/{year}/{month}/1/0/0"
    response = page.request.post(cal_url)
    if response.ok:
        cal = parse_calendar_row(response.text(), day)
        if "bowel" in cal:
            body["bowel"] = cal["bowel"]
        if "physiology" in cal:
            body["physiology"] = cal["physiology"]
        if not body.get("weight") and cal.get("weight"):
            body["weight"] = cal["weight"]
        if not body.get("body_fat") and cal.get("body_fat"):
            body["body_fat"] = cal["body_fat"]

    return body


def connect_chrome(playwright, port: int):
    endpoint = f"http://127.0.0.1:{port}"
    print(f"Chromeに接続: {endpoint}")
    browser = playwright.chromium.connect_over_cdp(endpoint)
    context = browser.contexts[0] if browser.contexts else browser.new_context(locale="ja-JP")
    page = context.pages[0] if context.pages else context.new_page()
    return browser, page


def sync_day(page: Page, target_date: str) -> dict:
    print(f"取得中: {target_date}")
    meal_data = fetch_meals(page, target_date)
    body_data = fetch_body(page, target_date)

    payload = {
        "source": "asken",
        "version": 1,
        "date": target_date,
        "meals": meal_data["meals"],
        "meal_total_kcal": meal_data["meal_total_kcal"],
        "weight": normalize_weight(body_data.get("weight")),
        "body_fat": body_data.get("body_fat"),
        "steps": body_data.get("steps"),
        "bowel": body_data.get("bowel"),
        "physiology": body_data.get("physiology", False),
    }
    return payload


def print_summary(payload: dict) -> None:
    print("\n--- 取得結果 ---")
    print(f"日付: {payload['date']}")
    for key in MEAL_KEYS:
        items = payload["meals"].get(key, [])
        label = MEAL_LABELS[key]
        if items:
            print(f"{label}: {len(items)}品")
            for item in items[:3]:
                print(f"  ・{item['name']} ({item['kcal']}kcal)")
            if len(items) > 3:
                print(f"  …他 {len(items) - 3}品")
        else:
            print(f"{label}: なし")
    print(f"合計カロリー: {payload.get('meal_total_kcal') or '-'}")
    print(f"体重: {payload.get('weight') or '-'} kg")
    print(f"体脂肪: {payload.get('body_fat') or '-'} %")
    print(f"お通じ: {payload.get('bowel') or '-'}")
    print(f"生理: {'あり' if payload.get('physiology') else 'なし'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="あすけん1日分をリズムケア用JSONに出力")
    parser.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--connect", action="store_true", help="start-chrome.ps1 のChromeに接続")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("-o", "--output", help="出力ファイル（省略時は export/日付.json）")
    args = parser.parse_args()

    if not args.connect:
        print("エラー: --connect を付けて実行してください")
        print("  1. .\\start-chrome.ps1")
        print("  2. python sync_day.py --connect --date 2026-06-07")
        return 1

    out_path = Path(args.output) if args.output else EXPORT_DIR / f"{args.date}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            browser, page = connect_chrome(p, args.cdp_port)
        except Exception as exc:
            print(f"Chrome接続失敗: {exc}")
            print("先に .\\start-chrome.ps1 を実行し、あすけんにログインしてください")
            return 1

        try:
            payload = sync_day(page, args.date)
        except Exception as exc:
            print(f"取得失敗: {exc}")
            return 1

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(payload)
    print(f"\n保存しました: {out_path}")
    print("\n次のステップ:")
    print("  リズムケア → 設定 → あすけん同期JSON からこのファイルをインポート")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
