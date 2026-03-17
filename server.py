import os
import json
import uuid
import logging
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
CORS(app)

DATA_FILE = Path("data/products.json")
DATA_FILE.parent.mkdir(exist_ok=True)

HEADERS_LIST = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }
]


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


def fetch_amazon_price(url: str) -> dict:
    """
    AmazonページからASIN・商品名・価格・画像を取得する。
    成功時は {"name": ..., "price": float, "image": ..., "asin": ...} を返す。
    失敗時は {"error": ...} を返す。
    """
    session = requests.Session()
    headers = HEADERS_LIST[0].copy()

    try:
        resp = session.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"error": f"ページ取得失敗: {e}"}

    soup = BeautifulSoup(resp.text, "html.parser")

    # 商品名
    name_tag = soup.select_one("#productTitle")
    name = name_tag.get_text(strip=True) if name_tag else "商品名不明"

    # 画像
    img_tag = soup.select_one("#landingImage") or soup.select_one("#imgBlkFront")
    image = img_tag.get("src", "") if img_tag else ""

    # 価格 (複数セレクタを試みる)
    price = None
    price_selectors = [
        ".a-price .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#priceblock_saleprice",
        ".a-price-whole",
        "#corePrice_feature_div .a-offscreen",
        "#apex_offerDisplay_desktop .a-offscreen",
        "#newBuyBoxPrice",
    ]
    for sel in price_selectors:
        tag = soup.select_one(sel)
        if tag:
            raw = tag.get_text(strip=True)
            price = parse_price(raw)
            if price is not None:
                break

    if price is None:
        # ページが Captcha 等でブロックされているか確認
        if "robot" in resp.text.lower() or "captcha" in resp.text.lower():
            return {"error": "Amazonにブロックされました。しばらく待ってから再試行してください。"}
        return {"error": "価格が見つかりませんでした（セレクタ不一致の可能性あり）"}

    # ASIN を URL から抽出
    asin = extract_asin(url)

    return {"name": name, "price": price, "image": image, "asin": asin}


def parse_price(text: str) -> float | None:
    """「￥1,234」「¥1,234」「1,234円」のような文字列を float に変換する。"""
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
    """全商品の価格を取得してデータを更新する（スケジューラから呼ばれる）。"""
    data = load_data()
    updated = False

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
        product.setdefault("history", []).append(entry)

        if result.get("price") is not None:
            product["current_price"] = result["price"]
            product["last_checked"] = entry["checked_at"]
            # 画像・商品名を初回または更新
            if result.get("name") and result["name"] != "商品名不明":
                product["name"] = result["name"]
            if result.get("image"):
                product["image"] = result["image"]

        updated = True

    if updated:
        save_data(data)
    logger.info("価格チェック完了")


# ── API エンドポイント ──────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/products", methods=["GET"])
def get_products():
    data = load_data()
    return jsonify(data["products"])


@app.route("/api/products", methods=["POST"])
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

    # 初回価格取得
    result = fetch_amazon_price(url)

    if "error" in result:
        # エラーでも登録は許可し、名前は URL から仮設定
        product = {
            "id": str(uuid.uuid4()),
            "url": url,
            "name": extract_asin(url) or url,
            "image": "",
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
def delete_product(product_id):
    data = load_data()
    before = len(data["products"])
    data["products"] = [p for p in data["products"] if p["id"] != product_id]
    if len(data["products"]) == before:
        return jsonify({"error": "商品が見つかりません"}), 404
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/products/<product_id>", methods=["PATCH"])
def update_product(product_id):
    """目標価格の更新。"""
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
def check_product(product_id):
    """指定商品の価格を今すぐチェックする。"""
    data = load_data()
    for product in data["products"]:
        if product["id"] == product_id:
            result = fetch_amazon_price(product["url"])
            entry = {
                "checked_at": datetime.now().isoformat(),
                "price": result.get("price"),
                "error": result.get("error"),
            }
            product.setdefault("history", []).append(entry)
            if result.get("price") is not None:
                product["current_price"] = result["price"]
                product["last_checked"] = entry["checked_at"]
                if result.get("name") and result["name"] != "商品名不明":
                    product["name"] = result["name"]
                if result.get("image"):
                    product["image"] = result["image"]
            save_data(data)
            if "error" in result:
                return jsonify({"warning": result["error"], "product": product})
            return jsonify({"product": product})
    return jsonify({"error": "商品が見つかりません"}), 404


@app.route("/api/check-all", methods=["POST"])
def check_all():
    """全商品を今すぐチェックする。"""
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
