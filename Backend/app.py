"""
NutriScan Backend — Flask API
=============================
Provides endpoints for scanning barcodes and searching products
using the OpenFoodFacts public API.

Endpoints:
  POST /scan-barcode   → Fetch product by barcode
  POST /search-product → Search product by name (returns first match)
"""

import os
import re
import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

# ──────────────────────────────────────────────
# 1. Configuration
# ──────────────────────────────────────────────
load_dotenv()  # Load environment variables from .env file

# Resolve the path to the Frontend folder (one level up from Backend)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "Frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="/static")
CORS(app)  # Enable CORS so the frontend can call this API

# Base URLs for OpenFoodFacts (no API key needed, but we keep the
# pattern so it's easy to swap to a paid API later)
PRODUCT_API_URL = "https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
SEARCH_API_URL  = "https://world.openfoodfacts.org/cgi/search.pl"

# Optional API key — stored in .env for forward-compatibility
API_KEY = os.getenv("OPENFOODFACTS_API_KEY", "")


# ──────────────────────────────────────────────
# 2. Helper — Clean & Parse Ingredients Text
# ──────────────────────────────────────────────
def parse_ingredients(raw_text):
    """
    Convert a raw ingredients string into a clean list.
    - Splits on commas, semicolons, or bullet characters
    - Strips whitespace and removes empty entries
    """
    if not raw_text:
        return []

    # Split on comma, semicolon, or bullet / dash list markers
    parts = re.split(r"[,;•]", raw_text)
    cleaned = [item.strip() for item in parts if item.strip()]
    return cleaned


# ──────────────────────────────────────────────
# 3. Helper — Separate Additives & Preservatives
# ──────────────────────────────────────────────

# Common preservative E-numbers (this list can be extended)
PRESERVATIVE_CODES = {
    "e200", "e201", "e202", "e203",          # Sorbates
    "e210", "e211", "e212", "e213",          # Benzoates
    "e214", "e215", "e216", "e217", "e218", "e219",
    "e220", "e221", "e222", "e223", "e224", "e225", "e226", "e227", "e228",  # Sulfites
    "e230", "e231", "e232", "e233",          # Biphenyl & derivatives
    "e234", "e235",
    "e239",                                   # Hexamethylene tetramine
    "e242",                                   # Dimethyl dicarbonate
    "e249", "e250", "e251", "e252",          # Nitrites / Nitrates
    "e260", "e261", "e262", "e263",          # Acetates
    "e270",                                   # Lactic acid
    "e280", "e281", "e282", "e283",          # Propionates
    "e284", "e285",
    "e290",                                   # Carbon dioxide
}


def classify_additives(additive_tags):
    """
    Given a list of additive tags (e.g. ['en:e330', 'en:e211']),
    return two separate lists:
      additives     — general additives (names cleaned)
      preservatives — subset that are known preservatives
    """
    additives = []
    preservatives = []

    if not additive_tags:
        return additives, preservatives

    for tag in additive_tags:
        # Clean the tag: remove language prefix like "en:"
        name = tag.replace("en:", "").strip()
        code = name.lower()

        if code in PRESERVATIVE_CODES:
            preservatives.append(name.upper())
        else:
            additives.append(name.upper())

    return additives, preservatives


# ──────────────────────────────────────────────
# 4. Helper — Build a Structured Response
# ──────────────────────────────────────────────
def build_product_response(product):
    """
    Extract and structure only the required fields from an
    OpenFoodFacts product object.
    Removes null / empty values automatically.
    """
    # Extract raw fields
    product_name   = product.get("product_name", "")
    image_url      = product.get("image_url", "")
    ingredients_raw = product.get("ingredients_text", "")
    additive_tags  = product.get("additives_tags", [])

    # ---- Additional fields the frontend needs ----
    categories     = product.get("categories", "")
    nutriscore     = product.get("nutriscore_score")
    allergens_tags = product.get("allergens_tags", [])
    ingredients_analysis = product.get("ingredients_analysis_tags", [])

    # Parse ingredients text into a clean list
    ingredients = parse_ingredients(ingredients_raw)

    # Separate additives and preservatives
    additives, preservatives = classify_additives(additive_tags)

    # Build response, omitting empty/null values
    response = {}

    if product_name:
        response["product_name"] = product_name
    if image_url:
        response["image"] = image_url
    if ingredients:
        response["ingredients"] = ingredients
    if additives:
        response["additives"] = additives
    if preservatives:
        response["preservatives"] = preservatives

    # Extra fields for frontend compatibility
    if categories:
        response["categories"] = categories
    if nutriscore is not None:
        response["nutriscore_score"] = nutriscore
    if allergens_tags:
        response["allergens_tags"] = [a.replace("en:", "") for a in allergens_tags]
    if ingredients_analysis:
        response["ingredients_analysis_tags"] = ingredients_analysis

    return response


# ──────────────────────────────────────────────
# 5. Endpoint — POST /scan-barcode
# ──────────────────────────────────────────────
@app.route("/scan-barcode", methods=["POST"])
def scan_barcode():
    """
    Accepts JSON: { "barcode": "1234567890123" }
    Returns structured product data or an error.
    """

    # --- 5a. Validate request body ---
    data = request.get_json(silent=True)
    if not data or "barcode" not in data:
        return jsonify({"error": "Missing 'barcode' field in request body"}), 400

    barcode = str(data["barcode"]).strip()
    if not barcode:
        return jsonify({"error": "Barcode cannot be empty"}), 400

    # Basic barcode format check (digits only, 8-13 chars typical)
    if not re.match(r"^\d{4,14}$", barcode):
        return jsonify({"error": "Invalid barcode format. Must be 4-14 digits."}), 400

    # --- 5b. Call external API ---
    try:
        url = PRODUCT_API_URL.format(barcode=barcode)
        headers = {}
        if API_KEY:
            headers["Authorization"] = f"Bearer {API_KEY}"

        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()

    except requests.exceptions.Timeout:
        return jsonify({"error": "External API timed out. Please try again."}), 504
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Unable to reach external API. Check your connection."}), 502
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"API request failed: {str(e)}"}), 500

    # --- 5c. Parse response ---
    api_data = resp.json()

    if api_data.get("status") != 1:
        return jsonify({"error": "Product not found"}), 404

    product = api_data.get("product", {})
    result = build_product_response(product)

    if not result:
        return jsonify({"error": "Product found but contains no usable data"}), 404

    return jsonify(result), 200


# ──────────────────────────────────────────────
# 6. Endpoint — POST /search-product
# ──────────────────────────────────────────────
@app.route("/search-product", methods=["POST"])
def search_product():
    """
    Accepts JSON: { "name": "chocolate" }
    Searches OpenFoodFacts by name, returns the first matching product.
    """

    data = request.get_json(silent=True)
    if not data or "name" not in data:
        return jsonify({"error": "Missing 'name' field in request body"}), 400

    name = str(data["name"]).strip()
    if not name:
        return jsonify({"error": "Product name cannot be empty"}), 400

    # --- Call search API ---
    try:
        resp = requests.get(
            SEARCH_API_URL,
            params={
                "search_terms": name,
                "search_simple": 1,
                "json": 1,
                "page_size": 1,  # We only need the first result
            },
            timeout=10,
        )
        resp.raise_for_status()

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Search API failed: {str(e)}"}), 500

    search_data = resp.json()
    products = search_data.get("products", [])

    if not products:
        return jsonify({"error": "Product not found"}), 404

    # Use the first result
    result = build_product_response(products[0])
    return jsonify(result), 200


# ──────────────────────────────────────────────
# 7. Serve Frontend & Health Check
# ──────────────────────────────────────────────
@app.route("/", methods=["GET"])
def serve_frontend():
    """Serve the main frontend HTML page."""
    return send_from_directory(FRONTEND_DIR, "index3nutripro.html")


@app.route("/health", methods=["GET"])
def health():
    """Simple health-check endpoint."""
    return jsonify({"status": "ok", "service": "NutriScan API"}), 200


# ──────────────────────────────────────────────
# 8. Run the server
# ──────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    print(f"\n🚀 NutriScan API running on http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
