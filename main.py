# main.py
import re, asyncio, xml.etree.ElementTree as ET, json
from typing import Dict, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from bs4 import BeautifulSoup as BS
from playwright.async_api import async_playwright
import httpx

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

CAROUSEL_CLASS_SELECTOR = (
    ".swiper, .slick-slider, .owl-carousel, .glide, .flickity-enabled, .splide, [class*='carousel']"
)

SEASON_MAP = {
    "ski|sci|snow": "nov–mar",
    "mare|piscina|costum": "mag–ago",
    "barbecue|griglia|giardino": "apr–lug",
    "scuola|zaino|astuccio": "ago–set",
    "natale|christmas|regali": "nov–dic",
    "halloween": "ott",
}

app = FastAPI(title="Ecommerce Auditor", version="0.2.0")

def absolutize(base: str, href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"): return href
    return base.rstrip("/") + "/" + href.lstrip("/")

async def _open_page(url: str, width=1366, height=900):
    p = await async_playwright().start()
    browser = await p.chromium.launch()
    ctx = await browser.new_context(viewport={"width": width, "height": height})
    page = await ctx.new_page()
    await page.goto(url, wait_until="networkidle", timeout=45000)
    # cookie banners più comuni
    for sel in [
        "button#onetrust-accept-btn-handler",
        "button[aria-label*='Accept']",
        "button:has-text('Accetta')",
        "button:has-text('Accept')",
        "button:has-text('OK')",
        "[data-testid='cookie-accept']",
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click(timeout=700)
                await page.wait_for_timeout(300)
        except:
            pass
    # scroll per lazy-load
    total = await page.evaluate("() => document.body.scrollHeight")
    y = 0
    while y < total:
        y += 800
        await page.evaluate(f"() => window.scrollTo(0, {y})")
        await page.wait_for_timeout(250)
        total = await page.evaluate("() => document.body.scrollHeight")
    await page.wait_for_timeout(1200)
    return p, browser, page

async def grab_html(url: str) -> str:
    p, browser, page = await _open_page(url)
    html = await page.content()
    await browser.close(); await p.stop()
    return html

def extract_scripts(soup: BS) -> List[str]:
    out = []
    for s in soup.select("script[src]"): out.append(s.get("src",""))
    for s in soup.select("script:not([src])"):
        if s.string: out.append((s.string or "")[:2000])
    return out

def detect_vendor(html: str, scripts: List[str]) -> str:
    haystack = html + "\n" + "\n".join(scripts)
    for name, pats in VENDOR_SIGNS.items():
        if any(re.search(p, haystack, re.I) for p in pats):
            return name
    if re.search(r'(?s)<form[^>]+role=["\']search', html, re.I):
        return "Base/native (Woo/Shopify)"
    return "Non chiaro (prob. base/custom)"

async def count_products_from_sitemap(origin: str) -> int | None:
    base = origin.rstrip("/")
    for path in ["/sitemap.xml", "/sitemap_index.xml"]:
        url = base + path
        try:
            r = httpx.get(url, timeout=10)
            if r.status_code != 200: continue
            root = ET.fromstring(r.text)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            locs = [loc.text for loc in root.findall(".//sm:loc", ns)]
            prod_sitemaps = [l for l in locs if re.search(r'product', l, re.I)] or locs
            total = 0
            for sm in prod_sitemaps[:5]:
                rr = httpx.get(sm, timeout=10)
                if rr.status_code != 200: continue
                rr_root = ET.fromstring(rr.text)
                total += len(rr_root.findall(".//sm:url", ns))
            return total if total>0 else None
        except Exception:
            continue
    return None

def season_guess(text: str):
    hits = []
    for patt, months in SEASON_MAP.items():
        if re.search(patt, text, re.I): hits.append(months)
    if not hits: return {"stagionale": False, "confidenza": "bassa"}
    months = ", ".join(sorted(set(hits)))
    return {"stagionale": True, "alta": months, "confidenza": "media"}

async def detect_carousels_js(url: str) -> dict:
    p, browser, page = await _open_page(url)
    result = await page.evaluate(f"""
() => {{
  const labels = new Set();
  const known = document.querySelectorAll("{CAROUSEL_CLASS_SELECTOR}");
  // overflow orizzontale
  const horiz = Array.from(document.querySelectorAll('*')).filter(el => {{
    const s = getComputedStyle(el);
    const horiz = (s.overflowX === 'auto' || s.overflowX === 'scroll');
    const wide = el.scrollWidth > el.clientWidth * 1.2;
    const items = el.querySelectorAll('li, .card, .product, .product-card, article, figure, .grid__item, .product-item');
    return (horiz || wide) && items.length >= 4;
  }});
  const all = Array.from(known).concat(horiz).slice(0, 15);

  function nearText(el){{
    let t = '';
    // heading precedente
    let prev = el.previousElementSibling, i=0;
    while(prev && i<4){{
      if(/^H[1-6]$/.test(prev.tagName)) t += ' ' + prev.textContent.trim();
      prev = prev.previousElementSibling; i++;
    }}
    const aria = el.getAttribute('aria-label') || '';
    if(aria) t += ' ' + aria;
    if(el.parentElement){{
      const ptxt = el.parentElement.querySelector('h2,h3,h4,[aria-label]');
      if(ptxt) t += ' ' + ptxt.textContent.trim();
    }}
    return t.trim();
  }}

  const kw = /(visti di recente|correlat|acquistati insieme|potrebbe interessarti|pi[uù] vendut|novit|consigliat|trending|best|top|recommended|related|recent|piu venduti|i piu venduti)/i;
  all.forEach(el => {{
    const t = nearText(el);
    if (kw.test(t)) labels.add(t.slice(0,120));
  }});
  return {{
    hasCarousel: all.length > 0,
    labelsFound: Array.from(labels)
  }};
}}
""")
    # prepara dati debug
    debug = await page.evaluate(f"""
() => {{
  return {{
    knownCount: document.querySelectorAll("{CAROUSEL_CLASS_SELECTOR}").length,
    hasHorizontal: Array.from(document.querySelectorAll('*')).some(el => {{
      const s = getComputedStyle(el);
      const horiz = (s.overflowX === 'auto' || s.overflowX === 'scroll');
      const wide = el.scrollWidth > el.clientWidth * 1.2;
      const items = el.querySelectorAll('li, .card, .product, .product-card, article, figure, .grid__item, .product-item');
      return (horiz || wide) && items.length >= 4;
    }})
  }};
}}
""")
    await browser.close(); await p.stop()
    return {"hasCarousel": bool(result.get("hasCarousel")), "labelsFound": result.get("labelsFound", []), "_debug": debug}

@app.get("/")
def root():
    return {"ok": True, "try": "/audit?url=https://esempio.com", "more": ["/docs", "/audit/html?url=https://demo.opencart.com/"]}

@app.get("/audit")
async def audit(url: str):
    try:
        home_html = await grab_html(url)
    except Exception as e:
        raise HTTPException(400, f"Impossibile caricare la home: {e}")

    soup = BS(home_html, "lxml")
    scripts = extract_scripts(soup)
    vendor = detect_vendor(home_html, scripts)

    links = [a.get("href") for a in soup.select("a[href]") if a.get("href")]
    pdp = next((l for l in links if re.search(r"/product|/prodotti|/produto|/item|/p/|/detail|/prod-", l, re.I)), None)
    if not pdp:
        for l in links[:50]:
            test_url = absolutize(url, l)
            try:
                h = await grab_html(test_url)
                if '"@type":"Product"' in h or 'itemtype="http://schema.org/Product"' in h.lower():
                    pdp = l; break
            except: pass
    cart = next((l for l in links if re.search(r"cart|carrello|checkout|basket|bag", l, re.I)), None)

    pages_text = soup.get_text(" ", strip=True)
    home_caro = await detect_carousels_js(url)

    pdp_caro = {}
    if pdp:
        try: pdp_caro = await detect_carousels_js(absolutize(url, pdp))
        except: pdp_caro = {}

    cart_caro = {}
    if cart:
        try: cart_caro = await detect_carousels_js(absolutize(url, cart))
        except: cart_caro = {}

    season = season_guess(pages_text)
    count = await count_products_from_sitemap(url)

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
            "home": {k:v for k,v in home_caro.items() if k!='_debug'},
            "product": {k:v for k,v in (pdp_caro or {"hasCarousel": False}).items() if k!='_debug'},
            "cart": {k:v for k,v in (cart_caro or {"hasCarousel": False}).items() if k!='_debug'}
        },
        "catalog_size_estimate": count,
        "seasonality": season,
        "quick_gaps": gaps[:3]
    })

@app.get("/audit/html", response_class=HTMLResponse)
async def audit_html(url: str):
    data = await audit(url)
    if hasattr(data, "body"): data = json.loads(data.body)
    se = data.get("search_engine", "N/D")
    car = data.get("carousels", {})
    size = data.get("catalog_size_estimate")
    seas = data.get("seasonality", {})
    gaps = data.get("quick_gaps", [])
    def yesno(b): return "✅" if b else "❌"
    h, p, c = car.get("home", {}), car.get("product", {}), car.get("cart", {})
    html = f"""
    <html><head><meta charset="utf-8"><title>Audit eCommerce</title>
    <style>body{{font-family:system-ui,Arial;max-width:900px;margin:40px auto}}.card{{border:1px solid #ddd;border-radius:10px;padding:16px;margin:12px 0}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:8px}}</style>
    </head><body>
      <h1>Audit eCommerce</h1>
      <div class="card"><h2>Motore di ricerca</h2><p><b>Rilevato:</b> {se}</p></div>
      <div class="card"><h2>Caroselli / Cross-sell</h2>
        <table>
          <tr><th>Pagina</th><th>Ha caroselli?</th><th>Etichette trovate</th></tr>
          <tr><td>Home</td><td>{yesno(h.get("hasCarousel", False))}</td><td>{", ".join(h.get("labelsFound", [])) or "-"}</td></tr>
          <tr><td>PDP</td><td>{yesno(p.get("hasCarousel", False))}</td><td>{", ".join(p.get("labelsFound", [])) or "-"}</td></tr>
          <tr><td>Carrello</td><td>{yesno(c.get("hasCarousel", False))}</td><td>{", ".join(c.get("labelsFound", [])) or "-"}</td></tr>
        </table>
      </div>
      <div class="card"><h2>Catalogo</h2><p><b>Stima numero prodotti:</b> {size if size is not None else "Non determinabile"}</p></div>
      <div class="card"><h2>Stagionalità</h2><p>{("Stagionale" if seas.get("stagionale") else "Non stagionale o incerto")} – confidenza: {seas.get("confidenza","-")} {(" – Alta: " + seas.get("alta","")) if seas.get("stagionale") else ""}</p></div>
      <div class="card"><h2>GAP rapidi</h2><ul>{"".join(f"<li>{g}</li>" for g in gaps) or "<li>Nessun gap evidente</li>"}</ul></div>
    </body></html>
    """
    return HTMLResponse(html)

@app.get("/audit/debug")
async def audit_debug(url: str):
    # utile per capire perché non vede i caroselli
    hc = await detect_carousels_js(url)
    return {"carousels_raw": hc}
