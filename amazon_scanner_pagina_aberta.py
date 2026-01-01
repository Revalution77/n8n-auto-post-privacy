"""
Scanner de ofertas Amazon.es (página já aberta)
================================================

Como abrir o Chrome com remote debugging:
- Windows (PowerShell/CMD):
  chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\\temp\\chrome-debug"
- macOS:
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="/tmp/chrome-debug"
- Linux:
  google-chrome --remote-debugging-port=9222 --user-data-dir="/tmp/chrome-debug"

Instalação de dependências:
    python -m pip install -r requirements.txt

Execução básica (não navega nem abre novas abas):
    python amazon_scanner_pagina_aberta.py

Parâmetros principais:
    --port 9222                 Porta do Chrome em modo remote debugging (default: 9222)
    --tag barracadescon-21      Tag de afiliado a acrescentar se faltar (default: barracadescon-21)
    --min_discount 5.0          Desconto mínimo em % para filtrar (default: 5.0)
    --max_products 400          Máximo de produtos a processar (default: 400)
    --auto_scroll 1             1 = scroll até ao fim para carregar lazy load; 0 = não scroll (default: 1)
    --deep_verify 0             1 = abrir cada produto em nova aba para tentar achar referência; 0 = não abrir (default: 0)

O script conecta-se a um Chrome já aberto, lê a aba atual (sem clicar ou navegar por padrão),
identifica cartões de produto, calcula descontos reais quando existe preço de referência visível
e gera um CSV em csv_scanner/ com o TOP de produtos.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import NoSuchWindowException, WebDriverException
from selenium.webdriver.chrome.options import Options

ASIN_REGEX = re.compile(r"^[A-Z0-9]{10}$")
PRICE_REGEX = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}")
DP_LINK_REGEX = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)
PER_UNIT_MARKERS = ["/kg", "€/kg", "€/100", "/100", "por kg", "por 100", "/l", "/unidad", "por unidad", "por ud", "/ud", "por litro"]


@dataclass
class Product:
    asin: str
    title: str
    url: str
    current_price: Optional[float]
    reference_price: Optional[float]
    reference_type: Optional[str]
    evidence: Optional[str]
    discount_pct: Optional[float]
    savings: Optional[float]


def is_valid_asin(asin: str) -> bool:
    return bool(ASIN_REGEX.match(asin.strip().upper()))


def parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in PER_UNIT_MARKERS):
        return None
    if "/" in lowered:
        return None
    cleaned = (
        lowered.replace("€", "")
        .replace("eur", "")
        .replace(" ", "")
        .replace("\xa0", "")
    )
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def format_price(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def canonical_url_from_asin(asin: str) -> str:
    return f"https://www.amazon.es/dp/{asin}"


def make_affiliate_link(url: str, tag: str) -> str:
    if "amazon.es" not in url:
        return url
    if "tag=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}tag={tag}"


def get_driver(port: int) -> webdriver.Chrome:
    chrome_options = Options()
    chrome_options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")
    try:
        driver = webdriver.Chrome(options=chrome_options)
    except WebDriverException as exc:  # pragma: no cover - defensive
        message = (
            "Não foi possível conectar ao Chrome via remote debugging.\n"
            "1) Verifique se abriu o Chrome com --remote-debugging-port="
            f"{port} e --user-data-dir=...\n"
            "2) Confirme se o ChromeDriver está disponível. Selenium 4.6+ usa o Selenium Manager\n"
            "   automaticamente; se falhar, instale webdriver-manager: pip install webdriver-manager\n"
            f"Erro original: {exc}"
        )
        raise SystemExit(message)
    return driver


def auto_scroll_page(driver: webdriver.Chrome, pause: float = 1.0, max_attempts: int = 20) -> None:
    last_height = 0
    stagnant_rounds = 0
    for _ in range(max_attempts):
        try:
            new_height = driver.execute_script("return document.body.scrollHeight")
        except Exception:
            break
        if new_height == last_height:
            stagnant_rounds += 1
            if stagnant_rounds >= 3:
                break
        else:
            stagnant_rounds = 0
        try:
            driver.execute_script("window.scrollTo(0, arguments[0]);", new_height)
        except Exception:
            break
        last_height = new_height
        time.sleep(pause)


def extract_asin_from_element(element) -> Optional[str]:
    asin_attr = element.get("data-asin")
    if asin_attr and is_valid_asin(asin_attr):
        return asin_attr.upper()
    href = element.get("href")
    if href:
        match = DP_LINK_REGEX.search(href)
        if match:
            asin = match.group(1).upper()
            if is_valid_asin(asin):
                return asin
    return None


def gather_cards(soup: BeautifulSoup, max_products: int) -> Dict[str, object]:
    cards: Dict[str, object] = {}
    for elem in soup.select("[data-asin]"):
        asin = extract_asin_from_element(elem)
        if asin and asin not in cards:
            cards[asin] = elem
        if len(cards) >= max_products:
            break
    if len(cards) < max_products:
        for link in soup.find_all("a", href=True):
            if len(cards) >= max_products:
                break
            asin = extract_asin_from_element(link)
            if not asin or asin in cards:
                continue
            parent = link
            for _ in range(4):
                if parent.parent:
                    parent = parent.parent
            cards[asin] = parent
    return cards


def extract_title(card) -> str:
    selectors = [
        ("h2 a span", False),
        ("h2", True),
        ("span.a-size-base-plus.a-color-base.a-text-normal", True),
        ("span.a-size-medium.a-color-base.a-text-normal", True),
        ("a.a-link-normal.a-text-normal", True),
    ]
    for selector, allow_self in selectors:
        found = card.select_one(selector)
        if found:
            text = found.get_text(" ", strip=True)
            if text:
                return text
        if allow_self:
            text = card.get_text(" ", strip=True)
            if text:
                return text
    return "(sem título)"


def find_primary_price(card) -> Optional[Tuple[float, str]]:
    for price_block in card.find_all("span", class_=lambda c: c and "a-price" in c.split() and "a-text-price" not in c.split()):
        offscreen = price_block.find("span", class_=lambda c: c and "a-offscreen" in c.split())
        if offscreen:
            value = parse_price(offscreen.get_text())
            if value is not None:
                return value, offscreen.get_text(strip=True)
    return None


def find_strikethrough_price(card) -> Optional[Tuple[float, str]]:
    strike = card.find("span", class_=lambda c: c and "a-price" in c.split() and "a-text-price" in c.split())
    if strike:
        offscreen = strike.find("span", class_=lambda c: c and "a-offscreen" in c.split())
        if offscreen:
            value = parse_price(offscreen.get_text())
            if value is not None:
                return value, offscreen.get_text(strip=True)
    return None


def extract_reference_from_text(card_text: str, keywords: Iterable[str]) -> Optional[Tuple[float, str]]:
    combined_kw = "|".join(re.escape(kw) for kw in keywords)
    if not combined_kw:
        return None
    pattern = re.compile(fr"({combined_kw}).{{0,30}}?(" + PRICE_REGEX.pattern + ")", re.IGNORECASE)
    match = pattern.search(card_text)
    if match:
        price_value = parse_price(match.group(2))
        if price_value is not None:
            snippet = match.group(0).strip()
            return price_value, snippet
    return None


def find_reference_price(card) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    text = card.get_text(" ", strip=True)

    # 1) Últimos 30 dias
    ref = extract_reference_from_text(text, ["ultimos 30 dias", "últimos 30 dias", "30 dias", "30 días"])
    if ref:
        return ref[0], "30_dias", ref[1]

    # 2) Mediano/médio
    ref = extract_reference_from_text(text, ["precio medio", "precio mediano", "preço médio", "preço mediano"])
    if ref:
        return ref[0], "mediano_medio", ref[1]

    # 3) PVP / recomendado
    ref = extract_reference_from_text(text, ["pvp", "rrp", "precio recomendado", "preço recomendado", "precio habitual"])
    if ref:
        return ref[0], "pvp_rrp", ref[1]

    # 4) Preço tachado
    strike = find_strikethrough_price(card)
    if strike:
        return strike[0], "tachado", strike[1]

    # 5) Anterior
    ref = extract_reference_from_text(text, ["antes", "precio anterior", "preço anterior"])
    if ref:
        return ref[0], "anterior", ref[1]

    return None, None, None


def deep_verify_reference(driver: webdriver.Chrome, product: Product, delay: float = 2.0) -> Product:
    original_window = driver.current_window_handle
    try:
        driver.execute_script("window.open(arguments[0], '_blank');", product.url)
        driver.switch_to.window(driver.window_handles[-1])
        time.sleep(delay)
        page_html = driver.page_source
        soup = BeautifulSoup(page_html, "lxml")
        card = soup.find("body") or soup
        ref_price, ref_type, evidence = find_reference_price(card)
        if ref_price and (product.reference_price is None or ref_price > (product.reference_price or 0)):
            product.reference_price = ref_price
            product.reference_type = ref_type
            product.evidence = evidence
            if product.current_price and product.reference_price > product.current_price:
                product.savings = product.reference_price - product.current_price
                product.discount_pct = (product.savings / product.reference_price) * 100
    except NoSuchWindowException:
        pass
    finally:
        try:
            if len(driver.window_handles) > 1:
                driver.close()
                driver.switch_to.window(original_window)
        except Exception:
            try:
                driver.switch_to.window(original_window)
            except Exception:
                pass
    return product


def extract_products_from_page(html: str, max_products: int) -> List[Tuple[str, object]]:
    soup = BeautifulSoup(html, "lxml")
    cards = gather_cards(soup, max_products)
    return [(asin, element) for asin, element in cards.items()]


def build_products(driver: webdriver.Chrome, max_products: int, deep_verify: bool) -> List[Product]:
    html = driver.page_source
    url = driver.current_url
    products: List[Product] = []
    card_pairs = extract_products_from_page(html, max_products)
    print(f"[INFO] Cartões encontrados: {len(card_pairs)}")

    for asin, card in card_pairs:
        title = extract_title(card)
        primary_price = find_primary_price(card)
        current_price = primary_price[0] if primary_price else None
        ref_price, ref_type, evidence = find_reference_price(card)
        canonical = canonical_url_from_asin(asin)
        discount_pct = None
        savings = None
        if current_price and ref_price and ref_price > current_price:
            savings = ref_price - current_price
            discount_pct = (savings / ref_price) * 100
        product = Product(
            asin=asin,
            title=title,
            url=canonical,
            current_price=current_price,
            reference_price=ref_price,
            reference_type=ref_type,
            evidence=evidence,
            discount_pct=discount_pct,
            savings=savings,
        )
        if deep_verify:
            product = deep_verify_reference(driver, product)
        products.append(product)
    print(f"[INFO] Produtos com ASIN válido: {len(products)}")
    return products


def filter_and_sort_products(products: List[Product], min_discount: float) -> List[Product]:
    filtered = [p for p in products if p.discount_pct is not None and p.discount_pct >= min_discount]
    print(f"[INFO] Produtos após filtro de desconto >= {min_discount}%: {len(filtered)}")
    filtered.sort(key=lambda p: (p.discount_pct or 0, p.savings or 0), reverse=True)
    return filtered


def export_csv(products: List[Product], source_url: str, tag: str) -> str:
    os.makedirs("csv_scanner", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join("csv_scanner", f"amazon_scan_{timestamp}.csv")
    headers = [
        "Fonte_Pagina",
        "ASIN",
        "Titulo",
        "Preco_Atual",
        "Preco_Referencia",
        "Tipo_Referencia",
        "Desconto_%",
        "Poupanca",
        "Link_Afiliado",
        "Evidencia",
    ]
    with open(filepath, "w", encoding="utf-8-sig", newline="") as csvfile:
        writer = csv.writer(csvfile, delimiter=";")
        writer.writerow(headers)
        for product in products:
            writer.writerow(
                [
                    source_url,
                    product.asin,
                    product.title,
                    f"{product.current_price:.2f}" if product.current_price is not None else "",
                    f"{product.reference_price:.2f}" if product.reference_price is not None else "",
                    product.reference_type or "",
                    f"{product.discount_pct:.2f}" if product.discount_pct is not None else "",
                    f"{product.savings:.2f}" if product.savings is not None else "",
                    make_affiliate_link(product.url, tag),
                    product.evidence or "",
                ]
            )
    return filepath


def print_top(products: List[Product], limit: int = 25) -> None:
    print("\nTOP resultados:")
    for idx, product in enumerate(products[:limit], start=1):
        discount_text = f"{product.discount_pct:.1f}%" if product.discount_pct is not None else "-"
        current_text = format_price(product.current_price)
        ref_text = format_price(product.reference_price)
        savings_text = format_price(product.savings)
        ref_type = product.reference_type or "?"
        print(
            f"{idx:02d}. {discount_text} | {current_text} -> {ref_text} ({ref_type}) | "
            f"Poupança: {savings_text} | {product.asin} | {product.title[:120]} | {product.url}"
        )


def ensure_amazon_domain(url: str) -> None:
    if "amazon.es" not in url:
        raise SystemExit(
            "A aba atual não parece ser amazon.es. Abra manualmente a página da Amazon.es e volte a executar."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scanner de página já aberta da Amazon.es")
    parser.add_argument("--port", type=int, default=9222, help="Porta do Chrome em modo remote debugging")
    parser.add_argument("--tag", type=str, default="barracadescon-21", help="Tag de afiliado a acrescentar se faltar")
    parser.add_argument("--min_discount", type=float, default=5.0, help="Desconto mínimo em % para aceitar")
    parser.add_argument("--max_products", type=int, default=400, help="Máximo de produtos a processar")
    parser.add_argument("--auto_scroll", type=int, choices=[0, 1], default=1, help="1 = scroll automático, 0 = não scroll")
    parser.add_argument("--deep_verify", type=int, choices=[0, 1], default=0, help="1 = abrir cada produto em nova aba para procurar referência")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    driver = get_driver(args.port)

    try:
        current_url = driver.current_url
    except WebDriverException as exc:  # pragma: no cover - defensive
        raise SystemExit(f"Não foi possível ler a aba atual. Confirme o Chrome aberto. Detalhe: {exc}")

    ensure_amazon_domain(current_url)
    print(f"[INFO] Conectado ao Chrome. Página atual: {current_url}")

    if args.auto_scroll == 1:
        print("[INFO] Auto-scroll ativado para carregar mais resultados...")
        auto_scroll_page(driver)
    else:
        print("[INFO] Auto-scroll desativado. Apenas conteúdo já visível será lido.")

    products = build_products(driver, args.max_products, bool(args.deep_verify))
    filtered = filter_and_sort_products(products, args.min_discount)

    if not filtered:
        print("Nenhum produto com desconto real encontrado com os critérios atuais.")
        return

    csv_path = export_csv(filtered, current_url, args.tag)
    print(f"CSV gerado em: {csv_path}")
    print_top(filtered)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Execução interrompida pelo utilizador.")
        sys.exit(1)
