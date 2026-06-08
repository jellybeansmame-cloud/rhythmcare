#!/usr/bin/env python3
"""
あすけん Web版の食事記録データ構造を調査するスクリプト。

使い方:
  pip install -r requirements.txt
  python investigate.py

【Googleログインの場合（推奨）】
  1. .\\start-chrome.ps1 で通常のChromeを起動
  2. 開いたChromeであすけんにGoogleログイン
  3. python investigate.py --connect

【メールアドレスログインの場合】
  python investigate.py
  → 開いたブラウザで「メールアドレスでログイン」を使用
     ※ Googleログインは自動ブラウザではブロックされます
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Response, sync_playwright

OUTPUT_ROOT = Path(__file__).parent / "output"
PROFILE_DIR = Path(__file__).parent / ".browser-profile"
ASKEN_BASE = "https://www.asken.jp"

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]

# 食事・記録に関係しそうなURL（日付は後で置換）
CANDIDATE_PATHS = [
    "/wsp/comment/{date}",
    "/wsp/top/{date}",
    "/wsp/diary/{date}",
    "/wsp/meal/{date}",
    "/my_graph/meal?from_mypage=1",
    "/my_graph/exercise?from_mypage=1",
    "/my_graph/weight?from_mypage=1",
    "/my_plan/",
    "/",
]

MEAL_KEYWORDS = re.compile(
    r"meal|food|menu|breakfast|lunch|dinner|snack|"
    r"朝食|昼食|夕食|間食|食事|メニュー|カロリー|calor|kcal|栄養",
    re.I,
)

JSON_IN_SCRIPT = re.compile(
    r"(?:var\s+)?(\w+)\s*=\s*(\{[\s\S]*?\});?\s*(?:\n|Graph\.|</script>)",
    re.M,
)


def today_str() -> str:
    return date.today().isoformat()


def slugify_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "_") or "root"
    query = parsed.query.replace("&", "_").replace("=", "-")[:60]
    name = path
    if query:
        name += f"__{query}"
    return re.sub(r"[^\w\-.]", "_", name)[:120]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def looks_like_meal_data(text: str) -> bool:
    return bool(MEAL_KEYWORDS.search(text))


def try_parse_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def extract_embedded_json(html: str) -> list[dict]:
    found = []
    for match in JSON_IN_SCRIPT.finditer(html):
        var_name, raw = match.group(1), match.group(2)
        data = try_parse_json(raw)
        if data is None:
            continue
        serialized = json.dumps(data, ensure_ascii=False)
        found.append(
            {
                "variable": var_name,
                "meal_related": looks_like_meal_data(serialized),
                "preview": serialized[:2000],
                "size": len(serialized),
            }
        )
    return found


def save_response(out_dir: Path, response: Response, index: int) -> dict | None:
    url = response.url
    if "asken.jp" not in url:
        return None

    content_type = (response.headers.get("content-type") or "").lower()
    status = response.status

    entry = {
        "url": url,
        "status": status,
        "content_type": content_type,
    }

    try:
        body = response.text()
    except Exception as exc:
        entry["error"] = str(exc)
        return entry

    if not body:
        entry["empty"] = True
        return entry

    meal_related = looks_like_meal_data(body)
    entry["meal_related"] = meal_related
    entry["body_size"] = len(body)

    should_save = (
        "json" in content_type
        or meal_related
        or any(k in url.lower() for k in ("meal", "food", "wsp", "graph", "record", "diary"))
    )

    if should_save:
        ext = "json" if "json" in content_type else "txt"
        parsed = try_parse_json(body)
        filename = f"{index:03d}__{slugify_url(url)}.{ext}"
        target = out_dir / "network" / filename
        ensure_dir(target.parent)
        if parsed is not None:
            target.write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            entry["saved_as"] = str(target.relative_to(out_dir))
        else:
            target.write_text(body[:500_000], encoding="utf-8")
            entry["saved_as"] = str(target.relative_to(out_dir))

    if meal_related and len(body) < 500:
        entry["body_preview"] = body

    return entry


def investigate_page(page: Page, url: str, out_dir: Path) -> dict:
    page_dir = ensure_dir(out_dir / "pages" / slugify_url(url))
    result = {"url": url, "final_url": None, "title": None, "error": None}

    try:
        response = page.goto(url, wait_until="networkidle", timeout=60_000)
        result["status"] = response.status if response else None
        result["final_url"] = page.url
        result["title"] = page.title()

        html = page.content()
        (page_dir / "page.html").write_text(html, encoding="utf-8")

        embedded = extract_embedded_json(html)
        if embedded:
            (page_dir / "embedded_json.json").write_text(
                json.dumps(embedded, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            result["embedded_json_count"] = len(embedded)
            result["embedded_meal_related"] = [
                e["variable"] for e in embedded if e["meal_related"]
            ]

        # 食事っぽいテキスト断片を拾う（デバッグ用）
        snippets = []
        for keyword in ("朝食", "昼食", "夕食", "間食", "食事", "kcal", "カロリー"):
            idx = html.find(keyword)
            if idx >= 0:
                start = max(0, idx - 120)
                end = min(len(html), idx + 280)
                snippets.append({"keyword": keyword, "context": html[start:end]})
        if snippets:
            (page_dir / "text_snippets.json").write_text(
                json.dumps(snippets, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            result["text_snippet_keywords"] = [s["keyword"] for s in snippets]

        page.screenshot(path=page_dir / "screenshot.png", full_page=True)

    except Exception as exc:
        result["error"] = str(exc)

    return result


def apply_stealth(context: BrowserContext) -> None:
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )


def launch_persistent_context(playwright) -> BrowserContext:
    """ログイン状態を .browser-profile に保存する通常モード。"""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    attempts: list[tuple[str, dict]] = [
        ("Google Chrome", {"channel": "chrome"}),
        ("Microsoft Edge", {"channel": "msedge"}),
        ("Playwright Chromium", {}),
    ]
    errors: list[str] = []
    for label, extra in attempts:
        try:
            print(f"ブラウザ起動: {label}（プロファイル: {PROFILE_DIR.name}）")
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                locale="ja-JP",
                args=STEALTH_ARGS,
                ignore_default_args=["--enable-automation"],
                **extra,
            )
            apply_stealth(context)
            return context
        except Exception as exc:
            errors.append(f"  - {label}: {exc}")

    print("\nブラウザを起動できませんでした。")
    print("  python -m playwright install chromium")
    print("\n".join(errors))
    raise RuntimeError("ブラウザ起動に失敗しました")


def connect_existing_chrome(playwright, port: int) -> tuple[BrowserContext, bool]:
    """start-chrome.ps1 で起動した通常Chromeに接続。Googleログイン向け。"""
    endpoint = f"http://127.0.0.1:{port}"
    print(f"起動中のChromeに接続: {endpoint}")
    browser = playwright.chromium.connect_over_cdp(endpoint)
    if browser.contexts:
        return browser.contexts[0], True
    return browser.new_context(locale="ja-JP"), True


def get_active_page(context: BrowserContext) -> Page:
    if context.pages:
        return context.pages[0]
    return context.new_page()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="あすけん Web版のデータ構造を調査")
    parser.add_argument("date", nargs="?", default=today_str(), help="調査対象日 YYYY-MM-DD")
    parser.add_argument(
        "--connect",
        action="store_true",
        help="start-chrome.ps1 で起動したChromeに接続（Googleログイン向け）",
    )
    parser.add_argument("--cdp-port", type=int, default=9222, help="CDPポート（既定: 9222）")
    return parser.parse_args()


def print_login_help(connect_mode: bool) -> None:
    print("\n" + "=" * 60)
    if connect_mode:
        print("【接続モード】start-chrome.ps1 で開いたChromeでログイン済みか確認してください。")
    else:
        print("【ログイン方法】")
        print("  ✓ メールアドレス＋パスワード でログイン")
        print("  ✗ Googleログイン → 自動ブラウザではブロックされます")
        print()
        print("  Googleログインしか使えない場合:")
        print("    1. いったん終了 (Ctrl+C)")
        print("    2. .\\start-chrome.ps1")
        print("    3. python investigate.py --connect")
    print("=" * 60)


def main() -> int:
    args = parse_args()
    target_date = args.date
    connect_mode = args.connect
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ensure_dir(OUTPUT_ROOT / f"{stamp}_{target_date}")

    print("=" * 60)
    print("あすけん Web版 データ構造調査")
    print(f"対象日: {target_date}")
    print(f"出力先: {out_dir}")
    if connect_mode:
        print("モード: 既存Chrome接続 (--connect)")
    print("=" * 60)

    network_log: list[dict] = []
    page_results: list[dict] = []
    owns_browser = False

    with sync_playwright() as p:
        if connect_mode:
            try:
                context, owns_browser = connect_existing_chrome(p, args.cdp_port)
            except Exception as exc:
                print(f"\nChromeに接続できませんでした: {exc}")
                print("\n先に別ターミナルで次を実行してください:")
                print("  .\\start-chrome.ps1")
                print("Chromeが起動したら、あすけんにログインしてから:")
                print("  python investigate.py --connect")
                return 1
            page = get_active_page(context)
        else:
            context = launch_persistent_context(p)
            page = get_active_page(context)

        def on_response(response: Response) -> None:
            entry = save_response(out_dir, response, len(network_log))
            if entry:
                network_log.append(entry)

        page.on("response", on_response)

        if not connect_mode:
            print("\nログイン画面を開きます...")
            page.goto(f"{ASKEN_BASE}/login", wait_until="domcontentloaded")

        print_login_help(connect_mode)
        print("\nログイン済みなら Enter → 調査開始")
        input(">> ")

        # マイページを一度開いてセッションを安定させる
        print("\nマイページを読み込み中...")
        investigate_page(page, f"{ASKEN_BASE}/", out_dir)

        urls = []
        for path in CANDIDATE_PATHS:
            urls.append(ASKEN_BASE + path.format(date=target_date))

        print(f"\n{len(urls)} 件のURLを順に調査します...\n")
        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}] {url}")
            page_results.append(investigate_page(page, url, out_dir))

        # ログイン後にユーザーが普段見る食事記録画面も調査できるよう待機
        print("\n" + "=" * 60)
        print("追加調査モード（任意）")
        print("ブラウザで「食事が記録されている日」の画面を自分で開いてください。")
        print("見たい画面を開いたら Enter → そのページを保存します。")
        print("終了するには q + Enter")
        print("=" * 60)

        while True:
            cmd = input("\nEnter=現在のページを保存 / q=終了 >> ").strip().lower()
            if cmd == "q":
                break
            current = page.url
            print(f"保存: {current}")
            page_results.append(investigate_page(page, current, out_dir))

        # Cookie を保存（次回の調査・本番スクリプト用）
        cookies = context.cookies()
        (out_dir / "cookies.json").write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if connect_mode:
            print("\n※ 接続モードのためChromeは開いたままにします")
        else:
            context.close()

    meal_network = [n for n in network_log if n.get("meal_related")]
    meal_pages = [
        p
        for p in page_results
        if p.get("embedded_meal_related") or p.get("text_snippet_keywords")
    ]

    summary = {
        "target_date": target_date,
        "investigated_at": stamp,
        "pages": page_results,
        "network_total": len(network_log),
        "network_meal_related": meal_network,
        "pages_with_meal_hints": meal_pages,
        "next_steps": [
            "output/.../network/ を開き、meal_related な JSON を確認",
            "output/.../pages/*/embedded_json.json に graphData 等がないか確認",
            "追加調査で開いた食事記録画面の page.html / screenshot.png を確認",
            "見つかった API URL をメモして scrape スクリプト設計へ進む",
        ],
    }

    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 60)
    print("調査完了")
    print(f"保存先: {out_dir}")
    print(f"ネットワーク記録: {len(network_log)} 件（食事関連っぽい: {len(meal_network)} 件）")
    print(f"ページ調査: {len(page_results)} 件")
    print(f"サマリー: {summary_path}")
    print("=" * 60)

    if meal_network:
        print("\n--- 食事関連っぽいネットワーク応答 ---")
        for item in meal_network[:15]:
            print(f"  [{item.get('status')}] {item.get('url')}")
            if item.get("saved_as"):
                print(f"       → {item['saved_as']}")

    if meal_pages:
        print("\n--- ページ内に食事キーワード / embedded JSON あり ---")
        for item in meal_pages:
            print(f"  {item.get('final_url') or item.get('url')}")
            if item.get("embedded_meal_related"):
                print(f"    embedded: {item['embedded_meal_related']}")
            if item.get("text_snippet_keywords"):
                print(f"    keywords: {item['text_snippet_keywords']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
