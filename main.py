# main.py
import re, asyncio, xml.etree.ElementTree as ET
from typing import Dict, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup as BS
from playwright.async_api import async_playwright
import httpx

# ---- Firme per riconoscere i motori di ricerca interni
VENDOR_SIGNS = {
    "Doofinder": [r"doofinder\.com", r"\bdf-"],
    "Algolia": [r"algolia(net|net\.com|\.com)", r"instantsearch"],
    "Klevu": [r"klevu\.com", r"\bklevu\b"],
    "Searchspring": [r"searchspring\.(io|net)"],
    "Searchanise": [r"searchanise\.com"],
    "Boost PFS": [r"boost.*product.*filter|boost-pfs"],
    "Luigi's Box": [r"luigisbox"],
    "Elastic/Custom": [r"_search\b", r"\bmeilisearch\b"],
}

# ---- Caroselli e parole chiave
CAROUSEL_CLASSES = r"(swiper|slick|owl-carousel|glide|flickity)"
CAROUSEL_WORDS = {
    "home": [r"pi[uù] vendut", r"novit", r"consigliat"],
    "pdp":  [r"correlat", r"acquistati insieme", r"visti di recente"],
    "cart": [r"completa il look", r"potrebbe interessarti", r"frequentemente", r"correlat"]
}

# ---- Stagionalità (semplice euristica)
SEASON_MAP = {
    "ski|sci|snow": "nov–mar",
    "mare|piscina|costum": "mag–ago",
    "barbecue|griglia|giardino": "apr–lug",
    "scuola|zaino|astuccio": "ago–set",
    "natale|christmas|regali": "nov–dic",
    "halloween": "ott",
}

app = FastAPI(title="Ecommerce Auditor", version="0.1.0")

async def grab_html(url: str) -> str:
    """Apre la pagina con Playwright (browser headless) e restituisce l'HTML renderizzato."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=20000)
        html = await page.content()
        await browser.close()
        return html

def extract_scripts(soup: BS) -> List[str]:
    out = []
    for s in soup.select("script[src]"):
        out.append(s.get("src",""))
    for s in soup.select("script:not([src])"):
        if s.string: out.append(s.string[:2000])
    return out

def detect_vendor(html: str, scripts: List[str]) -> str:
    haystack = html + "\n" + "\n".join(scripts)
    for name, pats in VENDOR_SIGNS.items():
        if any(re.search(p, haystack, re.I) for p in pats):
            return name
    # Fallback "base"
    if re.search(r'(?s)<form[^>]+role=["\']search', html, re.I):
        return "Base/native (Woo/Shopify)"
    return "Non chiaro (prob. base/custom)"

async def count_products_from_sitemap(origin: str) -> int | None:
    """Conta gli URL prodotto dalle sitemap (veloce, con cappetta)."""
    base = origin.rstrip("/")
    for path in ["/sitemap.xml", "/sitemap_index.xml"]:
        url = base + path
        try:
            r = httpx.get(url, timeout=10)
            if r.status_code != 200: continue
            root = ET.fromstring(r.text)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            locs = [loc.text for loc in root.findall(".//sm:loc", ns)]
            # Se esistono sitemaps specifiche per prodotti, usa quelle
            prod_sitemaps = [l for l in locs if re.search(r'product', l, re.I)] or locs
            total = 0
            for sm in prod_sitemaps[:5]:  # massimo 5 per rapidità
                rr = httpx.get(sm, timeout=10)
                if rr.status_code != 200: continue
                rr_root = ET.fromstring(rr.text)
                total += len(rr_root.findall(".//sm:url", ns))
            return total if total>0 else None
        except Exception:
            continue
    return None

def where_carousels(soup: BS) -> Dict[str, Dict]:
    text = soup.get_text(" ", strip=True)
    html = str(soup)
    has = bool(re.search(CAROUSEL_CLASSES, html, re.I))
    labels = []
    for w in CAROUSEL_WORDS["home"]:
        if re.search(w, text, re.I): labels.append(w)
    return {"hasCarousel": has, "labelsFound": labels}

def season_guess(pages_text: str):
    hits = []
    for patt, months in SEASON_MAP.items():
        if re.search(patt, pages_text, re.I):
            hits.append((patt, months))
    if not hits:
        return {"stagionale": False, "confidenza": "bassa"}
    months = ", ".join(sorted(set(m for _, m in hits)))
    return {"stagionale": True, "alta": months, "confidenza": "media"}

def absolutize(base: str, href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return base.rstrip("/") + "/" + href.lstrip("/")

@app.get("/")
def root():
    return {"ok": True, "try": "/audit?url=https://esempio.com"}

@app.get("/audit")
async def audit(url: str):
    # 1) Home
    try:
        home_html = await grab_html(url)
    except Exception as e:
        raise HTTPException(400, f"Impossibile caricare la home: {e}")

    soup = BS(home_html, "lxml")
    scripts = extract_scripts(soup)
    vendor = detect_vendor(home_html, scripts)

    # 2) Link candidati a PDP e Carrello
    links = [a.get("href") for a in soup.select("a[href]") if a.get("href")]
    pdp = next((l for l in links if re.search(r"/product|/prodotti|/produto|/item|/p/", l, re.I)), None)
    cart = next((l for l in links if re.search(r"cart|carrello", l, re.I)), None)

    pages_text = soup.get_text(" ", strip=True)
    home_caro = where_carousels(soup)

    # 3) PDP
    pdp_caro = {}
    if pdp:
        try:
            pdp_html = await grab_html(absolutize(url, pdp))
            pdp_soup = BS(pdp_html, "lxml")
            pdp_caro = where_carousels(pdp_soup)
            pages_text += " " + pdp_soup.get_text(" ", strip=True)
        except:
            pdp_caro = {}

    # 4) Carrello
    cart_caro = {}
    if cart:
        try:
            cart_html = await grab_html(absolutize(url, cart))
            cart_soup = BS(cart_html, "lxml")
            cart_caro = where_carousels(cart_soup)
            pages_text += " " + cart_soup.get_text(" ", strip=True)
        except:
            cart_caro = {}

    # 5) Stagionalità + conteggio prodotti
    season = season_guess(pages_text)
    count = await count_products_from_sitemap(url)

    # 6) Gap rapidi
    gaps = []
    if not re.search(r"visti di recente", pages_text, re.I):
        gaps.append("Manca 'Visti di recente' sulla PDP.")
    if not cart_caro.get("hasCarousel"):
        gaps.append("Nessun cross-sell nel Carrello.")
    if isinstance(vendor, str) and vendor.startswith("Base"):
        gaps.append("Ricerca base: nessuna sinonimia/typo, nessun merchandising dei risultati.")

    return JSONResponse({
        "search_engine": vendor,
        "carousels": {
            "home": home_caro,
            "product": pdp_caro or {"hasCarousel": False},
            "cart": cart_caro or {"hasCarousel": False}
        },
        "catalog_size_estimate": count,
        "seasonality": season,
        "quick_gaps": gaps[:3]
    })
