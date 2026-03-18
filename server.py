import os
import json
import uuid
import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory, session, redirect
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "local-dev-secret-xyz-change-me")
CORS(app, supports_credentials=True)

DATA_FILE = Path("data/products.json")
SETTINGS_FILE = Path("data/settings.json")
DATA_FILE.parent.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}


# ── 認証 ──────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "認証が必要です", "redirect": "/login"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


# ── データ操作 ────────────────────────────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"products": []}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {
        "notify_email": "",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_password": "",
        "chatwork_token": "",
        "chatwork_room_id": "",
        "keepa_api_key": "",
    }


def save_settings(settings: dict) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# ── メール通知 ────────────────────────────────────────────────────────

def send_email_notification(products_below: list) -> None:
    if not products_below:
        return
    settings = load_settings()
    if not (settings.get("notify_email") and settings.get("smtp_user") and settings.get("smtp_password")):
        logger.info("メール設定未完了のため通知スキップ")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"【Amazon価格追跡】{len(products_below)}件が目標価格以下になりました"
    msg["From"] = settings["smtp_user"]
    msg["To"] = settings["notify_email"]

    rows = "".join(
        f"""<tr>
          <td style="padding:8px;border:1px solid #ddd">{p['name'][:60]}</td>
          <td style="padding:8px;border:1px solid #ddd;color:#e53935;font-weight:bold">¥{int(p['current_price']):,}</td>
          <td style="padding:8px;border:1px solid #ddd">¥{int(p['target_price']):,}</td>
          <td style="padding:8px;border:1px solid #ddd"><a href="{p['url']}">Amazonで見る</a></td>
        </tr>"""
        for p in products_below
    )
    html = f"""<html><body style="font-family:sans-serif;color:#222;padding:24px">
    <h2 style="color:#e53935">🔴 目標価格以下の商品が見つかりました</h2>
    <table style="border-collapse:collapse;width:100%">
      <tr style="background:#f5f5f5">
        <th style="padding:8px;border:1px solid #ddd">商品名</th>
        <th style="padding:8px;border:1px solid #ddd">現在価格</th>
        <th style="padding:8px;border:1px solid #ddd">目標価格</th>
        <th style="padding:8px;border:1px solid #ddd">リンク</th>
      </tr>
      {rows}
    </table>
    <p style="color:#999;font-size:12px;margin-top:24px">Amazon価格追跡アプリより自動送信</p>
    </body></html>"""

    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"])) as server:
            server.ehlo()
            server.starttls()
            server.login(settings["smtp_user"], settings["smtp_password"])
            server.sendmail(settings["smtp_user"], settings["notify_email"], msg.as_string())
        logger.info("メール通知送信完了: %s 件", len(products_below))
    except Exception as e:
        logger.error("メール送信失敗: %s", e)


# ── Chatwork 通知 ─────────────────────────────────────────────────────

def send_chatwork_notification(products_below: list) -> None:
    if not products_below:
        return
    settings = load_settings()
    token = settings.get("chatwork_token", "")
    room_id = settings.get("chatwork_room_id", "")
    if not token or not room_id:
        logger.info("Chatwork設定未完了のため通知スキップ")
        return

    lines = ["[info][title]🔴 Amazon価格追跡 - 目標価格以下の商品があります[/title]"]
    for p in products_below:
        diff = int(p["target_price"] - p["current_price"])
        lines.append(
            f"■ {p['name'][:50]}\n"
            f"  現在価格: ¥{int(p['current_price']):,}  目標: ¥{int(p['target_price']):,}  (¥{diff:,} お得)\n"
            f"  {p['url']}"
        )
    lines.append("[/info]")
    message = "\n".join(lines)

    try:
        resp = requests.post(
            f"https://api.chatwork.com/v2/rooms/{room_id}/messages",
            headers={"X-ChatWorkToken": token},
            data={"body": message},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Chatwork通知送信完了: %s 件", len(products_below))
    except Exception as e:
        logger.error("Chatwork通知失敗: %s", e)


# ── Keepa API ─────────────────────────────────────────────────────────

# Keepa ドメインコード（amazon.co.jp = 5）
KEEPA_DOMAIN = 5

def fetch_price_via_keepa(asin: str, api_key: str) -> dict:
    """
    Keepa API で価格・商品名・画像を取得する。
    価格は Keepa 内部単位（円×100）で返されるため 100 で割る。
    """
    if not api_key or not asin:
        return {"error": "Keepa APIキーまたはASINが未設定です"}

    url = (
        f"https://api.keepa.com/product"
        f"?key={api_key}&domain={KEEPA_DOMAIN}&asin={asin}&stats=1"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": f"Keepa API リクエスト失敗: {e}"}

    if data.get("error"):
        return {"error": f"Keepa API エラー: {data['error'].get('message', '不明')}"}

    products = data.get("products") or []
    if not products:
        return {"error": "Keepa API: 商品が見つかりませんでした"}

    product = products[0]

    # 商品名
    name = product.get("title") or "商品名不明"

    # 画像（カンマ区切りの先頭）
    images_csv = product.get("imagesCSV", "")
    first_image = images_csv.split(",")[0] if images_csv else ""
    image = f"https://images-na.ssl-images-amazon.com/images/I/{first_image}" if first_image else ""

    # 現在価格（stats.current: [Amazon価格, 新品マーケット, ...] 単位=円×100）
    stats = product.get("stats") or {}
    current = stats.get("current") or []

    price_raw = None
    # インデックス0=Amazon直販, 1=新品出品者（いずれか有効な方を使用）
    for idx in [0, 1, 7, 11]:
        if idx < len(current) and current[idx] is not None and current[idx] > 0:
            price_raw = current[idx]
            break

    if price_raw is None:
        return {"error": "Keepa API: 現在の価格情報がありません（在庫切れの可能性）"}

    price = price_raw / 100.0  # 円×100 → 円
    if price < 100:
        return {"error": "Keepa API: 取得した価格が不正です（在庫・表示切れの可能性）"}

    return {"name": name, "price": price, "image": image, "asin": asin}


# ── スクレイピング ─────────────────────────────────────────────────────

def fetch_amazon_price(url: str) -> dict:
    """Keepa API が設定されていれば優先使用、なければスクレイピングにフォールバック。"""
    asin = extract_asin(url)
    settings = load_settings()
    keepa_key = settings.get("keepa_api_key", "")

    if keepa_key and asin:
        result = fetch_price_via_keepa(asin, keepa_key)
        if "error" not in result:
            return result
        logger.warning("Keepa API 失敗 (%s)、スクレイピングにフォールバック", result["error"])

    return _scrape_amazon_price(url)


def clean_amazon_url(url: str) -> str:
    """トラッキングパラメータを除去してASINベースのシンプルなURLに変換する。"""
    asin = extract_asin(url)
    if asin:
        if "amazon.co.jp" in url:
            return f"https://www.amazon.co.jp/dp/{asin}"
        if "amazon.com" in url:
            return f"https://www.amazon.com/dp/{asin}"
    return url


def _scrape_amazon_price(url: str) -> dict:
    clean_url = clean_amazon_url(url)
    sess = requests.Session()
    try:
        resp = sess.get(clean_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"error": f"ページ取得失敗: {e}"}

    soup = BeautifulSoup(resp.text, "html.parser")

    if "robot" in resp.text.lower() or "captcha" in resp.text.lower():
        return {"error": "Amazonにブロックされました。しばらく待ってから再試行してください。"}

    name_tag = soup.select_one("#productTitle")
    name = name_tag.get_text(strip=True) if name_tag else "商品名不明"

    img_tag = soup.select_one("#landingImage") or soup.select_one("#imgBlkFront")
    image = img_tag.get("src", "") if img_tag else ""

    price = None

    # 優先度順に特定セクションを確認（誤セレクタを避ける）
    specific_selectors = [
        "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#apex_desktop_newAccordionRow .a-price:not(.a-text-price) .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#priceblock_saleprice",
        "#newBuyBoxPrice",
        "#olpLinkWidget_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#apex_offerDisplay_desktop .a-price:not(.a-text-price) .a-offscreen",
    ]
    for sel in specific_selectors:
        tag = soup.select_one(sel)
        if tag:
            p = parse_price(tag.get_text(strip=True))
            if p is not None and p >= 100:  # ¥100未満は誤検知として除外
                price = p
                break

    # 特定セレクタで見つからない場合、全 .a-price から100円以上の最小値を使用
    if price is None:
        candidates = []
        for tag in soup.select(".a-price:not(.a-text-price) .a-offscreen"):
            p = parse_price(tag.get_text(strip=True))
            if p is not None and p >= 100:
                candidates.append(p)
        if candidates:
            price = min(candidates)  # 最安値（通常は販売価格）

    if price is None:
        return {"error": "価格が見つかりませんでした（在庫切れまたはページ構造の変更の可能性）"}
    if price < 100:
        return {"error": "価格の取得に失敗しました（不正な値。Keepa APIの設定を推奨）"}

    return {"name": name, "price": price, "image": image, "asin": extract_asin(url)}


def parse_price(text: str) -> float | None:
    import re
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_asin(url: str) -> str:
    import re
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    return m.group(1) if m else ""


def check_all_prices() -> None:
    data = load_data()
    products_below = []

    for product in data["products"]:
        url = product.get("url", "")
        if not url:
            continue
        logger.info("価格チェック中: %s", product.get("name", url))
        result = fetch_amazon_price(url)

        entry = {
            "checked_at": datetime.now().isoformat(),
            "price": result.get("price"),
            "error": result.get("error"),
        }
        if result.get("price") is not None and result["price"] < 100:
            entry["error"] = "価格の取得に失敗しました（不正な値）"
            entry["price"] = None
        product.setdefault("history", []).append(entry)

        if result.get("price") is not None and result["price"] >= 100:
            product["current_price"] = result["price"]
            product["last_checked"] = entry["checked_at"]
            if result.get("name") and result["name"] != "商品名不明":
                product["name"] = result["name"]
            if result.get("image"):
                product["image"] = result["image"]
            if result.get("asin"):
                product["asin"] = result["asin"]
            if product.get("target_price") is not None and result["price"] <= product["target_price"]:
                products_below.append(product)

    save_data(data)
    send_email_notification(products_below)
    send_chatwork_notification(products_below)
    logger.info("価格チェック完了")


# ── ルート ────────────────────────────────────────────────────────────

@app.route("/login")
def login_page():
    if session.get("logged_in"):
        return redirect("/")
    return send_from_directory("static", "login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(silent=True) or {}
    username = body.get("username", "").strip()
    password = body.get("password", "")
    admin_user = os.environ.get("ADMIN_USERNAME", "admin")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "amazon123")
    if username == admin_user and password == admin_pass:
        session["logged_in"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"error": "ユーザー名またはパスワードが違います"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/")
@login_required
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/settings", methods=["GET"])
@login_required
def get_settings():
    s = load_settings()
    return jsonify({
        "notify_email": s.get("notify_email", ""),
        "smtp_host": s.get("smtp_host", "smtp.gmail.com"),
        "smtp_port": s.get("smtp_port", 587),
        "smtp_user": s.get("smtp_user", ""),
        "smtp_password_set": bool(s.get("smtp_password")),
        "chatwork_token_set": bool(s.get("chatwork_token")),
        "chatwork_room_id": s.get("chatwork_room_id", ""),
        "keepa_api_key_set": bool(s.get("keepa_api_key")),
    })


@app.route("/api/settings", methods=["POST"])
@login_required
def update_settings():
    body = request.get_json(silent=True) or {}
    settings = load_settings()
    for key in ["notify_email", "smtp_host", "smtp_port", "smtp_user", "chatwork_room_id"]:
        if key in body:
            settings[key] = body[key]
    if body.get("smtp_password"):
        settings["smtp_password"] = body["smtp_password"]
    if body.get("chatwork_token"):
        settings["chatwork_token"] = body["chatwork_token"]
    if body.get("keepa_api_key"):
        settings["keepa_api_key"] = body["keepa_api_key"]
    save_settings(settings)
    return jsonify({"ok": True})


@app.route("/api/settings/test-email", methods=["POST"])
@login_required
def test_email():
    settings = load_settings()
    if not (settings.get("notify_email") and settings.get("smtp_user") and settings.get("smtp_password")):
        return jsonify({"error": "メール設定が未完了です"}), 400
    try:
        msg = MIMEText("Amazon価格追跡アプリのテストメールです。設定が正常に完了しています。", "plain", "utf-8")
        msg["Subject"] = "【Amazon価格追跡】テストメール"
        msg["From"] = settings["smtp_user"]
        msg["To"] = settings["notify_email"]
        with smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"])) as server:
            server.ehlo()
            server.starttls()
            server.login(settings["smtp_user"], settings["smtp_password"])
            server.sendmail(settings["smtp_user"], settings["notify_email"], msg.as_string())
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/test-chatwork", methods=["POST"])
@login_required
def test_chatwork():
    settings = load_settings()
    if not (settings.get("chatwork_token") and settings.get("chatwork_room_id")):
        return jsonify({"error": "ChatworkのAPIトークンとルームIDを入力してください"}), 400
    try:
        resp = requests.post(
            f"https://api.chatwork.com/v2/rooms/{settings['chatwork_room_id']}/messages",
            headers={"X-ChatWorkToken": settings["chatwork_token"]},
            data={"body": "✅ Amazon価格追跡アプリのテスト通知です。正常に連携できています！"},
            timeout=10,
        )
        resp.raise_for_status()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/products", methods=["GET"])
@login_required
def get_products():
    data = load_data()
    return jsonify(data["products"])


@app.route("/api/products", methods=["POST"])
@login_required
def add_product():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    target_price = body.get("target_price")

    if not url:
        return jsonify({"error": "URLを入力してください"}), 400
    if "amazon" not in url.lower():
        return jsonify({"error": "AmazonのURLを入力してください"}), 400
    try:
        target_price = float(target_price) if target_price is not None else None
    except (ValueError, TypeError):
        return jsonify({"error": "目標価格は数値で入力してください"}), 400

    # トラッキングパラメータを除去したURLで保存
    url = clean_amazon_url(url)
    result = fetch_amazon_price(url)
    if "error" in result:
        product = {
            "id": str(uuid.uuid4()),
            "url": url,
            "asin": result.get("asin") or extract_asin(url),
            "name": result.get("name") or extract_asin(url) or url,
            "image": result.get("image", ""),
            "target_price": target_price,
            "current_price": None,
            "last_checked": None,
            "added_at": datetime.now().isoformat(),
            "history": [{"checked_at": datetime.now().isoformat(), "price": None, "error": result["error"]}],
        }
        data = load_data()
        data["products"].append(product)
        save_data(data)
        return jsonify({"product": product, "warning": result["error"]}), 201

    product = {
        "id": str(uuid.uuid4()),
        "url": url,
        "asin": result.get("asin") or extract_asin(url),
        "name": result["name"],
        "image": result.get("image", ""),
        "target_price": target_price,
        "current_price": result["price"],
        "last_checked": datetime.now().isoformat(),
        "added_at": datetime.now().isoformat(),
        "history": [{"checked_at": datetime.now().isoformat(), "price": result["price"], "error": None}],
    }
    data = load_data()
    data["products"].append(product)
    save_data(data)
    return jsonify({"product": product}), 201


@app.route("/api/products/<product_id>", methods=["DELETE"])
@login_required
def delete_product(product_id):
    data = load_data()
    before = len(data["products"])
    data["products"] = [p for p in data["products"] if p["id"] != product_id]
    if len(data["products"]) == before:
        return jsonify({"error": "商品が見つかりません"}), 404
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/products/<product_id>", methods=["PATCH"])
@login_required
def update_product(product_id):
    body = request.get_json(silent=True) or {}
    data = load_data()
    for product in data["products"]:
        if product["id"] == product_id:
            if "target_price" in body:
                try:
                    product["target_price"] = float(body["target_price"]) if body["target_price"] != "" else None
                except (ValueError, TypeError):
                    return jsonify({"error": "目標価格は数値で入力してください"}), 400
            save_data(data)
            return jsonify({"product": product})
    return jsonify({"error": "商品が見つかりません"}), 404


@app.route("/api/products/<product_id>/check", methods=["POST"])
@login_required
def check_product(product_id):
    data = load_data()
    for product in data["products"]:
        if product["id"] == product_id:
            result = fetch_amazon_price(product["url"])
            entry = {
                "checked_at": datetime.now().isoformat(),
                "price": result.get("price"),
                "error": result.get("error"),
            }
            if result.get("price") is not None and result["price"] < 100:
                entry["error"] = "価格の取得に失敗しました（不正な値）"
                entry["price"] = None
            product.setdefault("history", []).append(entry)
            if result.get("price") is not None and result["price"] >= 100:
                product["current_price"] = result["price"]
                product["last_checked"] = entry["checked_at"]
                if result.get("name") and result["name"] != "商品名不明":
                    product["name"] = result["name"]
                if result.get("image"):
                    product["image"] = result["image"]
                if result.get("asin"):
                    product["asin"] = result["asin"]
            save_data(data)
            if "error" in result:
                return jsonify({"warning": result["error"], "product": product})
            return jsonify({"product": product})
    return jsonify({"error": "商品が見つかりません"}), 404


@app.route("/api/check-all", methods=["POST"])
@login_required
def check_all():
    check_all_prices()
    data = load_data()
    return jsonify(data["products"])


def start_scheduler():
    scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
    scheduler.add_job(check_all_prices, "cron", hour=9, minute=0)
    scheduler.start()
    logger.info("スケジューラ起動: 毎日 09:00 に価格チェックを実行します")


start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
