#!/usr/bin/env python3
"""
あすけんから1日分のデータを取得し、リズムケア用JSONを出力する。

ローカル（Cookie取得）:
  .\\start-chrome.ps1
  python sync_day.py --connect --upload-cookies

クラウド（GitHub Actions）:
  python sync_day.py --push
  （メール/パスワードで自動ログイン。または Firestore のログイン情報）
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
CREDENTIALS_PATH = Path(__file__).parent / "asken_credentials.json"

MEAL_KEYS = ("breakfast", "lunch", "dinner", "sweets")
MEAL_LABELS = {
    "breakfast": "朝食",
    "lunch": "昼食",
    "dinner": "夕食",
    "sweets": "間食",
}
# あすけん「アドバイスを見る」食事別ページ（/wsp/advice/{日付}/{id}）
MEAL_ADVICE_IDS = {
    "breakfast": "3",
    "lunch": "4",
    "dinner": "5",
}
NUTRIENT_KEYS = ("energy", "protein", "lipid", "carbohydrate")


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


def parse_meal_kcal(html: str) -> int | None:
    match = re.search(r'id="meal_type_energy">(\d+)kcal', html)
    if not match:
        return None
    return int(match.group(1))


def parse_nutrients(html: str) -> dict[str, str]:
    nutrients: dict[str, str] = {}
    for label, key in [
        ("エネルギー", "energy"),
        ("タンパク質", "protein"),
        ("脂質", "lipid"),
        ("炭水化物", "carbohydrate"),
    ]:
        pattern = (
            rf'<li class="title">{label}</li>.*?'
            rf'<li class="val[^"]*">([\d.]+)(?:kcal|g)</li>'
        )
        m = re.search(pattern, html, re.DOTALL)
        if m:
            nutrients[key] = m.group(1)
    return nutrients


def parse_advice(html: str) -> dict:
    result: dict[str, Any] = {}

    score_match = re.search(r"(\d+)\s*点", html)
    if score_match:
        result["health_score"] = int(score_match.group(1))

    nutrients = parse_nutrients(html)
    if nutrients:
        result["daily_nutrients"] = nutrients

    return result


def compute_sweets_nutrients(
    daily_nutrients: dict[str, str],
    meal_pfc: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    remainder: dict[str, str] = {}
    for key in ("protein", "lipid", "carbohydrate"):
        try:
            daily_val = float(daily_nutrients.get(key) or 0)
        except ValueError:
            daily_val = 0.0
        meal_sum = sum(
            float((meal_pfc.get(meal_key) or {}).get(key) or 0)
            for meal_key in MEAL_ADVICE_IDS
        )
        remainder[key] = str(round(max(0.0, daily_val - meal_sum), 1))
    if daily_nutrients.get("energy"):
        try:
            daily_e = float(daily_nutrients["energy"])
        except ValueError:
            daily_e = 0.0
        meal_e = sum(
            float((meal_pfc.get(meal_key) or {}).get("energy") or 0)
            for meal_key in MEAL_ADVICE_IDS
        )
        rem_e = round(max(0.0, daily_e - meal_e))
        if rem_e > 0:
            remainder["energy"] = str(int(rem_e))
    return remainder or None


def fetch_meal_pfc(
    page: Page,
    target_date: str,
    meals: dict[str, list],
    daily_nutrients: dict[str, str] | None,
) -> dict[str, dict[str, str]]:
    meal_pfc: dict[str, dict[str, str]] = {}
    for meal_key, advice_id in MEAL_ADVICE_IDS.items():
        if not meals.get(meal_key):
            continue
        url = f"{ASKEN_BASE}/wsp/advice/{target_date}/{advice_id}"
        goto_asken(page, url)
        if is_login_page(page):
            raise AuthError("あすけんのログインが切れています")
        nutrients = parse_nutrients(page.content())
        if nutrients:
            meal_pfc[meal_key] = nutrients

    if meals.get("sweets") and daily_nutrients and meal_pfc:
        sweets = compute_sweets_nutrients(daily_nutrients, meal_pfc)
        if sweets:
            meal_pfc["sweets"] = sweets

    return meal_pfc


def parse_exercise_datas(html: str) -> dict:
    match = re.search(r"WspExerciseV2\.exeDatas\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    menus = []
    for entry in data.get("menus") or []:
        menus.append(
            {
                "name": entry.get("name", ""),
                "amount": str(entry.get("amount", "")),
                "used_calory": str(entry.get("used_calory", "")),
                "cal": entry.get("cal", ""),
            }
        )
    return {
        "has_exercise": data.get("do") == "1" and bool(menus),
        "total_calory": data.get("total"),
        "menus": menus,
    }


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
    meal_kcal: dict[str, int | None] = {}
    total_kcal = 0.0
    for key in MEAL_KEYS:
        url = f"{ASKEN_BASE}/wsp/meal/{key}/{target_date}"
        goto_asken(page, url)
        if is_login_page(page):
            raise AuthError("あすけんのログインが切れています")
        html = page.content()
        items = parse_eat_datas(html)
        meals[key] = items
        meal_kcal[key] = parse_meal_kcal(html)
        for item in items:
            try:
                total_kcal += float(item.get("kcal") or 0)
            except ValueError:
                pass
    return {
        "meals": meals,
        "meal_kcal": meal_kcal,
        "meal_total_kcal": int(total_kcal) if total_kcal else None,
    }


def fetch_advice(page: Page, target_date: str) -> dict:
    url = f"{ASKEN_BASE}/wsp/advice/{target_date}"
    goto_asken(page, url)
    if is_login_page(page):
        raise AuthError("あすけんのログインが切れています")
    return parse_advice(page.content())


def fetch_exercise(page: Page, target_date: str) -> dict:
    url = f"{ASKEN_BASE}/wsp/exercise/{target_date}"
    goto_asken(page, url)
    if is_login_page(page):
        raise AuthError("あすけんのログインが切れています")
    return parse_exercise_datas(page.content())


def normalize_weight(val: str | None) -> str | None:
    if not val:
        return None
    match = re.search(r"[\d.]+", str(val).replace(",", ""))
    return match.group(0) if match else None


def fetch_body(page: Page, target_date: str) -> dict:
    year, month, day = (int(x) for x in target_date.split("-"))
    body: dict = {}

    comment_url = f"{ASKEN_BASE}/wsp/comment/{target_date}"
    goto_asken(page, comment_url)
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


def goto_asken(page: Page, url: str, *, retries: int = 3) -> None:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            time.sleep(0.4)
            return
        except Exception as exc:
            last_exc = exc
            print(f"ページ取得リトライ ({attempt}/{retries}): {url} — {exc}")
            time.sleep(1.0)
    if last_exc:
        raise last_exc


def is_login_page(page: Page) -> bool:
    if "/login" in page.url.lower():
        return True
    html = page.content()
    return (
        'id="login_form"' in html
        or 'id="indexForm"' in html
        or 'id="CustomerMemberEmail"' in html
    )


def load_asken_credentials() -> dict | None:
    email = os.environ.get("ASKEN_EMAIL", "").strip()
    password = os.environ.get("ASKEN_PASSWORD", "").strip()
    if email and password:
        return {"email": email, "password": password}

    if CREDENTIALS_PATH.exists():
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        if data.get("email") and data.get("password"):
            return {"email": data["email"], "password": data["password"]}

    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if config.get("asken_email") and config.get("asken_password"):
            return {
                "email": config["asken_email"],
                "password": config["asken_password"],
            }

    db, uid = get_firestore()
    snap = (
        db.collection("users")
        .document(uid)
        .collection("asken_config")
        .document("credentials")
        .get()
    )
    if snap.exists:
        data = snap.to_dict() or {}
        if data.get("email") and data.get("password"):
            return {"email": data["email"], "password": data["password"]}
    return None


def login_with_credentials(page: Page, email: str, password: str) -> None:
    print("メールアドレスでログイン中...")
    page.goto(f"{ASKEN_BASE}/login", wait_until="domcontentloaded", timeout=60_000)
    page.fill("#CustomerMemberEmail", email)
    page.fill("#CustomerMemberPasswdPlain", password)
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=60_000):
            page.click("#SubmitSubmit")
    except Exception:
        page.click("#SubmitSubmit")
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
    time.sleep(0.5)
    if is_login_page(page):
        raise AuthError(
            "ログインに失敗しました。メールアドレスとパスワードを確認してください。"
        )
    print("ログイン成功")


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


def load_auth_cookies_only() -> tuple[dict | None, list[dict] | None]:
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

    try:
        db, uid = get_firestore()
        snap = db.collection("users").document(uid).collection("asken_config").document("cookies").get()
        if snap.exists:
            data = snap.to_dict() or {}
            if data.get("storage_state"):
                state = data["storage_state"]
                return state, state.get("cookies")
            if data.get("cookies"):
                return None, normalize_cookie_list(data)
    except Exception as exc:
        print(f"FirestoreのCookie読み込みスキップ: {exc}")
    return None, None


def load_auth_from_sources() -> tuple[dict | None, list[dict] | None]:
    storage_state, cookies = load_auth_cookies_only()
    if storage_state or cookies:
        return storage_state, cookies

    raise FileNotFoundError(
        "ログイン情報が見つかりません。refresh-cookies.bat を実行してください。"
    )


def open_asken_session(playwright, target_date: str) -> tuple[Any, Page, bool]:
    storage_state, cookies = load_auth_cookies_only()
    if storage_state or cookies:
        browser, page = launch_headless(playwright, storage_state, cookies)
        probe_url = f"{ASKEN_BASE}/wsp/meal/breakfast/{target_date}"
        goto_asken(page, probe_url)
        if not is_login_page(page):
            print("保存済みのログイン情報で接続しました")
            return browser, page, False
        print("保存済みCookieが無効です。メール/パスワードで再ログインします...")
        browser.close()

    credentials = load_asken_credentials()
    if not credentials:
        raise FileNotFoundError(
            "あすけんのログイン情報がありません。"
            "リズムケア設定でメール/パスワードを保存するか、"
            "GitHub Secrets に ASKEN_EMAIL / ASKEN_PASSWORD を設定してください。"
        )
    browser, page = launch_headless(playwright, None, None)
    login_with_credentials(page, credentials["email"], credentials["password"])
    return browser, page, True


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
    advice_data = fetch_advice(page, target_date)
    exercise_data = fetch_exercise(page, target_date)
    daily_nutrients = advice_data.get("daily_nutrients")
    meal_pfc = fetch_meal_pfc(page, target_date, meal_data["meals"], daily_nutrients)

    payload = {
        "source": "asken",
        "version": 2,
        "date": target_date,
        "meals": meal_data["meals"],
        "meal_kcal": meal_data.get("meal_kcal"),
        "meal_pfc": meal_pfc or None,
        "meal_total_kcal": meal_data["meal_total_kcal"],
        "health_score": advice_data.get("health_score"),
        "daily_nutrients": daily_nutrients,
        "exercise": exercise_data or None,
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
    if payload.get("health_score") is not None:
        print(f"健康度: {payload['health_score']}点")
    nutrients = payload.get("daily_nutrients") or {}
    if nutrients:
        print(
            "1日PFC: "
            f"P{nutrients.get('protein', '-')}g "
            f"F{nutrients.get('lipid', '-')}g "
            f"C{nutrients.get('carbohydrate', '-')}g"
        )
    meal_kcal = payload.get("meal_kcal") or {}
    meal_pfc = payload.get("meal_pfc") or {}
    for key in MEAL_KEYS:
        items = payload["meals"].get(key, [])
        label = MEAL_LABELS[key]
        kcal = meal_kcal.get(key)
        pfc = meal_pfc.get(key)
        if items:
            header = f"{label}: {len(items)}品"
            if kcal:
                header += f" / {kcal}kcal"
            if pfc:
                header += (
                    f" / P{pfc.get('protein', '-')}g F{pfc.get('lipid', '-')}g C{pfc.get('carbohydrate', '-')}g"
                )
            print(header)
            for item in items:
                print(f"  ・{item['name']} ({item['kcal']}kcal)")
        else:
            print(f"{label}: なし")
    print(f"合計カロリー: {payload.get('meal_total_kcal') or '-'}")
    exercise = payload.get("exercise") or {}
    if exercise.get("has_exercise"):
        print(f"運動: {exercise.get('total_calory') or '-'}kcal ({len(exercise.get('menus') or [])}件)")
    else:
        print("運動: なし")
    print(f"体重: {payload.get('weight') or '-'} kg")
    print(f"体脂肪: {payload.get('body_fat') or '-'} %")
    print(f"お通じ: {payload.get('bowel') or '-'}")
    print(f"生理: {'あり' if payload.get('physiology') else 'なし'}")


def run_sync(args: argparse.Namespace) -> int:
    out_path = Path(args.output) if args.output else EXPORT_DIR / f"{args.date}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = None
    used_credentials = False
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
                    browser, page, used_credentials = open_asken_session(p, args.date)
                except FileNotFoundError as exc:
                    push_status(
                        ok=False,
                        error="no_cookies",
                        message=str(exc),
                    )
                    print(f"エラー: {exc}")
                    return 1

            payload = sync_day(page, args.date)
            if used_credentials and payload:
                try:
                    upload_auth_to_firestore(collect_storage_state(page))
                except Exception as exc:
                    print(f"Cookie更新スキップ: {exc}")
        except AuthError as exc:
            err = "auth_failed" if used_credentials else "cookie_expired"
            hint = (
                " PCで refresh-cookies.bat を実行するか、"
                "リズムケア設定のあすけんログイン情報を確認してください。"
            )
            push_status(ok=False, error=err, message=str(exc) + hint)
            print(f"認証エラー: {exc}")
            return 1
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            push_status(ok=False, error="sync_failed", message=str(exc))
            print(f"取得失敗: {exc}")
            print(tb)
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


def parse_date_arg(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%d")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="あすけん1日分をリズムケア用JSONに出力")
    parser.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="一括取得の開始日")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="一括取得の終了日")
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

    if args.date_from or args.date_to:
        if not args.date_from or not args.date_to:
            parser.error("--from と --to は両方指定してください")
        if args.connect:
            parser.error("一括取得では --connect は使えません")
        start = datetime.strptime(parse_date_arg(args.date_from), "%Y-%m-%d").date()
        end = datetime.strptime(parse_date_arg(args.date_to), "%Y-%m-%d").date()
        if start > end:
            parser.error("--from は --to 以前の日付にしてください")
        exit_code = 0
        current = start
        while current <= end:
            args.date = current.isoformat()
            if not args.output:
                args.output = None
            code = run_sync(args)
            if code != 0:
                exit_code = code
            current = current.fromordinal(current.toordinal() + 1)
        return exit_code

    if not args.connect and not args.push and not args.output:
        parser.error("--connect、--push、または -o のいずれかを指定してください")

    return run_sync(args)


if __name__ == "__main__":
    raise SystemExit(main())
