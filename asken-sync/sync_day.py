#!/usr/bin/env python3
"""
あすけんから1日分のデータを取得し、リズムケア用JSONを出力する。

ローカル（Cookie取得）:
  .\\start-chrome.ps1
  python sync_day.py --connect --upload-cookies

クラウド（GitHub Actions）:
  python sync_day.py --push
  （Firestore asken_config/cookies から Cookie を読み込む）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, sync_playwright

ASKEN_BASE = "https://www.asken.jp"
EXPORT_DIR = Path(__file__).parent / "export"
COOKIES_PATH = Path(__file__).parent / ".asken_cookies.json"
STORAGE_PATH = Path(__file__).parent / ".asken_storage_state.json"
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CONFIG_PATH = Path(__file__).parent / "firebase_config.json"

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
        if is_login_page(page):
            raise AuthError("あすけんのログインが切れています")
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
    if is_login_page(page):
        raise AuthError("あすけんのログインが切れています")
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


class AuthError(Exception):
    pass


def is_login_page(page: Page) -> bool:
    if "/login" in page.url.lower():
        return True
    html = page.content()
    return 'id="login_form"' in html or 'action="/login"' in html


def connect_chrome(playwright, port: int) -> tuple[Any, Page]:
    endpoint = f"http://127.0.0.1:{port}"
    print(f"Chromeに接続: {endpoint}")
    browser = playwright.chromium.connect_over_cdp(endpoint)
    context = browser.contexts[0] if browser.contexts else browser.new_context(locale="ja-JP")
    page = context.pages[0] if context.pages else context.new_page()
    return browser, page


def normalize_cookie_list(raw: Any) -> list[dict]:
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw["cookies"]
    if not isinstance(raw, list):
        raise ValueError("CookieはJSON配列、または {\"cookies\": [...]} 形式で指定してください")
    cookies = []
    for item in raw:
        if not isinstance(item, dict) or "name" not in item or "value" not in item:
            raise ValueError("Cookieの形式が正しくありません")
        cookie = {
            "name": item["name"],
            "value": item["value"],
            "domain": item.get("domain", ".asken.jp"),
            "path": item.get("path", "/"),
        }
        if "expires" in item:
            cookie["expires"] = item["expires"]
        if "httpOnly" in item:
            cookie["httpOnly"] = item["httpOnly"]
        if "secure" in item:
            cookie["secure"] = item["secure"]
        if "sameSite" in item and item["sameSite"]:
            cookie["sameSite"] = item["sameSite"]
        cookies.append(cookie)
    return cookies


def load_auth_from_sources() -> tuple[dict | None, list[dict] | None]:
    env_state = os.environ.get("ASKEN_STORAGE_STATE_JSON")
    if env_state:
        state = json.loads(env_state)
        return state, state.get("cookies")

    if STORAGE_PATH.exists():
        state = json.loads(STORAGE_PATH.read_text(encoding="utf-8"))
        return state, state.get("cookies")

    env_json = os.environ.get("ASKEN_COOKIES_JSON")
    if env_json:
        cookies = normalize_cookie_list(json.loads(env_json))
        return None, cookies

    if COOKIES_PATH.exists():
        cookies = normalize_cookie_list(json.loads(COOKIES_PATH.read_text(encoding="utf-8")))
        return None, cookies

    db, uid = get_firestore()
    snap = db.collection("users").document(uid).collection("asken_config").document("cookies").get()
    if snap.exists:
        data = snap.to_dict() or {}
        if data.get("storage_state"):
            state = data["storage_state"]
            return state, state.get("cookies")
        if data.get("cookies"):
            return None, normalize_cookie_list(data)

    raise FileNotFoundError(
        "ログイン情報が見つかりません。refresh-cookies.bat を実行してください。"
    )


def save_auth_local(storage_state: dict) -> None:
    STORAGE_PATH.write_text(json.dumps(storage_state, ensure_ascii=False, indent=2), encoding="utf-8")
    cookies = storage_state.get("cookies", [])
    if cookies:
        COOKIES_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ログイン情報を保存しました: {STORAGE_PATH}")


def launch_headless(playwright, storage_state: dict | None, cookies: list[dict] | None) -> tuple[Browser, Page]:
    browser = playwright.chromium.launch(headless=True)
    context_kwargs: dict[str, Any] = {"locale": "ja-JP", "user_agent": CHROME_UA}
    if storage_state:
        context_kwargs["storage_state"] = storage_state
    context = browser.new_context(**context_kwargs)
    if cookies and not storage_state:
        context.add_cookies(cookies)
    page = context.new_page()
    return browser, page


def collect_storage_state(page: Page) -> dict:
    return page.context.storage_state()


def load_firebase_settings() -> tuple[str, Any]:
    uid = os.environ.get("FIREBASE_UID", "").strip()
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()

    if sa_json:
        if not uid:
            raise ValueError("FIREBASE_UID 環境変数を設定してください")
        return uid, json.loads(sa_json)

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"{CONFIG_PATH.name} がありません。firebase_config.json.example をコピーして設定してください。"
        )
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    uid = config.get("firebase_uid", "")
    key_path = Path(config.get("service_account_json", "serviceAccountKey.json"))
    if not uid:
        raise ValueError("firebase_config.json に firebase_uid を設定してください")
    if not key_path.is_absolute():
        key_path = Path(__file__).parent / key_path
    if not key_path.exists():
        raise FileNotFoundError(f"サービスアカウントキーが見つかりません: {key_path}")
    return uid, str(key_path)


def get_firestore():
    import firebase_admin
    from firebase_admin import credentials, firestore

    uid, cred = load_firebase_settings()
    if not firebase_admin._apps:
        if isinstance(cred, dict):
            firebase_admin.initialize_app(credentials.Certificate(cred))
        else:
            firebase_admin.initialize_app(credentials.Certificate(cred))
    return firestore.client(), uid


def push_status(
    *,
    ok: bool,
    error: str | None = None,
    message: str | None = None,
    synced_date: str | None = None,
) -> None:
    db, uid = get_firestore()
    from firebase_admin import firestore as fs

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    payload: dict[str, Any] = {
        "ok": ok,
        "error": error,
        "message": message or "",
        "lastAttemptAt": now_ms,
        "updatedAt": fs.SERVER_TIMESTAMP,
    }
    if ok:
        payload["lastSuccessAt"] = now_ms
        if synced_date:
            payload["lastSyncedDate"] = synced_date
    ref = db.collection("users").document(uid).collection("asken_config").document("status")
    ref.set(payload, merge=True)
    print(f"同期ステータスを更新しました: ok={ok} error={error}")


def upload_auth_to_firestore(storage_state: dict) -> None:
    db, uid = get_firestore()
    from firebase_admin import firestore as fs

    ref = db.collection("users").document(uid).collection("asken_config").document("cookies")
    ref.set(
        {
            "storage_state": storage_state,
            "cookies": storage_state.get("cookies", []),
            "updatedAt": fs.SERVER_TIMESTAMP,
            "updatedAtMs": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
    )
    status_ref = db.collection("users").document(uid).collection("asken_config").document("status")
    status_ref.set(
        {
            "ok": None,
            "error": None,
            "message": "ログイン情報を更新しました。次回の自動同期をお待ちください。",
            "cookiesUpdatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
            "updatedAt": fs.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    print(f"Firestoreにログイン情報を保存しました: users/{uid}/asken_config/cookies")


def push_to_firestore(payload: dict) -> None:
    db, uid = get_firestore()
    from firebase_admin import firestore as fs

    ref = (
        db.collection("users")
        .document(uid)
        .collection("asken_inbox")
        .document(payload["date"])
    )
    ref.set({**payload, "pushedAt": fs.SERVER_TIMESTAMP})
    print(f"Firestore受信箱に送信しました: users/{uid}/asken_inbox/{payload['date']}")


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


def run_sync(args: argparse.Namespace) -> int:
    out_path = Path(args.output) if args.output else EXPORT_DIR / f"{args.date}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = None
    with sync_playwright() as p:
        browser = None
        try:
            if args.connect:
                browser, page = connect_chrome(p, args.cdp_port)
                if args.upload_cookies or args.save_cookies:
                    storage_state = collect_storage_state(page)
                    if args.save_cookies:
                        save_auth_local(storage_state)
                    if args.upload_cookies:
                        upload_auth_to_firestore(storage_state)
                    if not args.push and not args.output:
                        return 0
            else:
                try:
                    storage_state, cookies = load_auth_from_sources()
                except FileNotFoundError as exc:
                    push_status(ok=False, error="no_cookies", message=str(exc))
                    print(f"エラー: {exc}")
                    return 1
                browser, page = launch_headless(p, storage_state, cookies)

            payload = sync_day(page, args.date)
        except AuthError as exc:
            push_status(ok=False, error="cookie_expired", message=str(exc))
            print(f"認証エラー: {exc}")
            return 1
        except Exception as exc:
            push_status(ok=False, error="sync_failed", message=str(exc))
            print(f"取得失敗: {exc}")
            return 1
        finally:
            if browser and not args.connect:
                browser.close()

    if not payload:
        return 1

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(payload)
    print(f"\n保存しました: {out_path}")

    if args.push:
        try:
            push_to_firestore(payload)
            push_status(
                ok=True,
                error=None,
                message="同期に成功しました",
                synced_date=payload["date"],
            )
            print("スマホのリズムケアを開くと自動で反映されます。")
        except ImportError:
            push_status(ok=False, error="sync_failed", message="firebase-admin が未インストール")
            print("\nエラー: firebase-admin が未インストールです")
            print("  pip install firebase-admin")
            return 1
        except Exception as exc:
            push_status(ok=False, error="sync_failed", message=str(exc))
            print(f"\nFirestore送信失敗: {exc}")
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="あすけん1日分をリズムケア用JSONに出力")
    parser.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--connect", action="store_true", help="start-chrome.ps1 のChromeに接続")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("-o", "--output", help="出力ファイル（省略時は export/日付.json）")
    parser.add_argument(
        "--push",
        action="store_true",
        help="Firestoreのasken_inboxに送信（スマホが自動取り込み）",
    )
    parser.add_argument(
        "--save-cookies",
        action="store_true",
        help="接続中ChromeのCookieを .asken_cookies.json に保存",
    )
    parser.add_argument(
        "--upload-cookies",
        action="store_true",
        help="接続中ChromeのCookieを Firestore asken_config/cookies に保存",
    )
    args = parser.parse_args()

    if not args.connect and not args.push and not args.output:
        parser.error("--connect、--push、または -o のいずれかを指定してください")

    return run_sync(args)


if __name__ == "__main__":
    raise SystemExit(main())
