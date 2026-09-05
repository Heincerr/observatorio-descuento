#!/usr/bin/env python3
"""
Observatorio del Descuento — captura diaria de precios
Autor: Harry Heincer Jawer Sanchez Arango

Uso:
    python captura_precios.py

Requiere:
    pip install requests beautifulsoup4

Entrada:  referencias.csv  (columnas: id,minorista,categoria,url)
Salida:   precios.db       (SQLite, append-only)
"""

import csv
import json
import re
import sqlite3
import sys
import time
import random
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

# --- Configuración -----------------------------------------------------------

ARCHIVO_REFERENCIAS = "referencias.csv"
BASE_DATOS = "precios.db"

# Identifícate. Es una exigencia ética y reduce el riesgo de bloqueo.
USER_AGENT = (
    "ObservatorioDescuento/1.0 (investigacion academica; "
    "Universidad Colegio Mayor de Cundinamarca; "
    "contacto: TU_CORREO@unicolmayor.edu.co)"
)

PAUSA_MIN = 8      # segundos entre peticiones al mismo dominio
PAUSA_MAX = 15
TIMEOUT = 25
REINTENTOS = 3

# --- Base de datos -----------------------------------------------------------

ESQUEMA = """
CREATE TABLE IF NOT EXISTS observaciones (
    id_observacion   INTEGER PRIMARY KEY AUTOINCREMENT,
    id_referencia    TEXT NOT NULL,
    minorista        TEXT NOT NULL,
    categoria        TEXT,
    url              TEXT NOT NULL,
    momento_utc      TEXT NOT NULL,
    precio           REAL,
    precio_lista     REAL,
    moneda           TEXT,
    disponibilidad   TEXT,
    metodo           TEXT,
    estado           TEXT NOT NULL,
    detalle_error    TEXT,
    html_hash        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ref_momento
    ON observaciones (id_referencia, momento_utc);
"""


def abrir_bd():
    con = sqlite3.connect(BASE_DATOS)
    con.executescript(ESQUEMA)
    return con


def guardar(con, fila):
    con.execute(
        """INSERT INTO observaciones
           (id_referencia, minorista, categoria, url, momento_utc,
            precio, precio_lista, moneda, disponibilidad,
            metodo, estado, detalle_error, html_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        fila,
    )
    con.commit()


# --- Extracción de precio ----------------------------------------------------

def _a_numero(valor):
    """Convierte '$ 1.299.900' o '1299900.00' a float."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    texto = re.sub(r"[^\d.,]", "", texto)
    if not texto:
        return None
    # Formato colombiano: punto separa miles, coma separa decimales
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif "," in texto:
        texto = texto.replace(",", ".")
    elif texto.count(".") > 1:
        texto = texto.replace(".", "")
    elif re.search(r"\.\d{3}$", texto):
        texto = texto.replace(".", "")
    try:
        return float(texto)
    except ValueError:
        return None


def _recorrer_jsonld(nodo):
    """Devuelve todos los diccionarios anidados de un bloque JSON-LD."""
    if isinstance(nodo, dict):
        yield nodo
        for v in nodo.values():
            yield from _recorrer_jsonld(v)
    elif isinstance(nodo, list):
        for v in nodo:
            yield from _recorrer_jsonld(v)


def extraer_desde_jsonld(sopa):
    """
    Método preferido. La mayoría de los minoristas grandes publican
    datos estructurados schema.org/Product en la propia página.
    Es mucho más estable que depender de clases CSS.
    """
    for etiqueta in sopa.find_all("script", type="application/ld+json"):
        try:
            datos = json.loads(etiqueta.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for d in _recorrer_jsonld(datos):
            if not isinstance(d, dict):
                continue
            if d.get("@type") in ("Offer", "AggregateOffer") or "price" in d:
                precio = _a_numero(d.get("price") or d.get("lowPrice"))
                if precio:
                    return {
                        "precio": precio,
                        "moneda": d.get("priceCurrency"),
                        "disponibilidad": str(d.get("availability") or ""),
                        "metodo": "json-ld",
                    }
    return None


def extraer_desde_meta(sopa):
    """Respaldo: etiquetas OpenGraph / itemprop."""
    selectores = [
        ("meta", {"property": "product:price:amount"}, "content"),
        ("meta", {"property": "og:price:amount"}, "content"),
        ("meta", {"itemprop": "price"}, "content"),
    ]
    for nombre, attrs, campo in selectores:
        etiqueta = sopa.find(nombre, attrs=attrs)
        if etiqueta and etiqueta.get(campo):
            precio = _a_numero(etiqueta[campo])
            if precio:
                return {"precio": precio, "moneda": None,
                        "disponibilidad": "", "metodo": "meta"}
    return None


def extraer_desde_selector(sopa, selector_css):
    """
    Último recurso: selector CSS propio de cada minorista.
    Debes inspeccionar la página (clic derecho > Inspeccionar) y anotarlo
    en la columna 'selector' del CSV si los dos métodos anteriores fallan.
    """
    if not selector_css:
        return None
    nodo = sopa.select_one(selector_css)
    if nodo:
        precio = _a_numero(nodo.get_text())
        if precio:
            return {"precio": precio, "moneda": None,
                    "disponibilidad": "", "metodo": "selector"}
    return None


# --- robots.txt --------------------------------------------------------------

_cache_robots = {}


def permitido(url):
    dominio = urlparse(url).netloc
    if dominio not in _cache_robots:
        rp = RobotFileParser()
        rp.set_url(f"{urlparse(url).scheme}://{dominio}/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None
        _cache_robots[dominio] = rp
    rp = _cache_robots[dominio]
    if rp is None:
        return True  # sin robots.txt legible, se procede con cautela
    return rp.can_fetch(USER_AGENT, url)


# --- Captura -----------------------------------------------------------------

def capturar(fila, sesion):
    url = fila["url"]
    ahora = datetime.now(timezone.utc).isoformat()
    base = (fila["id"], fila["minorista"], fila.get("categoria", ""), url, ahora)

    if not permitido(url):
        return base + (None, None, None, None, None, "bloqueado_robots",
                       "robots.txt no permite el acceso", None)

    ultimo_error = ""
    for intento in range(REINTENTOS):
        try:
            r = sesion.get(url, timeout=TIMEOUT)
            if r.status_code != 200:
                ultimo_error = f"HTTP {r.status_code}"
                time.sleep(5 * (intento + 1))
                continue

            sopa = BeautifulSoup(r.text, "html.parser")
            resultado = (
                extraer_desde_jsonld(sopa)
                or extraer_desde_meta(sopa)
                or extraer_desde_selector(sopa, fila.get("selector"))
            )
            html_hash = str(hash(r.text))

            if resultado:
                return base + (
                    resultado["precio"], None, resultado["moneda"],
                    resultado["disponibilidad"], resultado["metodo"],
                    "ok", None, html_hash,
                )
            return base + (None, None, None, None, None,
                           "sin_precio",
                           "Página cargada pero no se identificó el precio",
                           html_hash)

        except requests.RequestException as e:
            ultimo_error = type(e).__name__
            time.sleep(5 * (intento + 1))

    return base + (None, None, None, None, None, "error", ultimo_error, None)


def main():
    try:
        with open(ARCHIVO_REFERENCIAS, newline="", encoding="utf-8") as f:
            referencias = list(csv.DictReader(f))
    except FileNotFoundError:
        print(f"Falta {ARCHIVO_REFERENCIAS}. Crea el archivo con las columnas: "
              "id,minorista,categoria,url,selector")
        sys.exit(1)

    con = abrir_bd()
    sesion = requests.Session()
    sesion.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "es-CO,es;q=0.9",
    })

    ok = fallos = 0
    for i, fila in enumerate(referencias, 1):
        registro = capturar(fila, sesion)
        guardar(con, registro)
        estado = registro[10]
        if estado == "ok":
            ok += 1
            print(f"[{i}/{len(referencias)}] {fila['id']}: {registro[5]:,.0f}")
        else:
            fallos += 1
            print(f"[{i}/{len(referencias)}] {fila['id']}: {estado}")
        time.sleep(random.uniform(PAUSA_MIN, PAUSA_MAX))

    con.close()
    print(f"\nCaptura terminada. Correctas: {ok} | Fallidas: {fallos}")
    print("IMPORTANTE: los registros fallidos también se guardan. "
          "Un vacío en la serie no es lo mismo que un cambio de precio.")


if __name__ == "__main__":
    main()
