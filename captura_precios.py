#!/usr/bin/env python3
"""
Observatorio del Descuento — captura diaria de precios (v2)
Autor: Harry Heincer Jawer Sanchez Arango

Cambios frente a la v1:
  1. Captura el precio de referencia anunciado (el valor tachado).
  2. JSON-LD anclado al producto de la ficha, no al primer precio que aparezca.
  3. Conversión numérica que entiende notación científica (1.2629017E7).
  4. Huella SHA-256 estable, verificable entre ejecuciones.
  7. Guarda el nombre del producto, no solo su código.
  5. Guarda el extracto crudo del dato como respaldo probatorio.
  6. Marca como sospechosos los precios implausibles en lugar de guardarlos callado.

Uso:
    python captura_precios.py

Requiere:
    pip install requests beautifulsoup4

Entrada:  referencias.csv  (columnas: id,minorista,categoria,url,selector)
Salida:   precios.db       (SQLite, solo agrega, nunca sobrescribe)
"""

import csv
import hashlib
import json
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

# --- Configuración -----------------------------------------------------------

ARCHIVO_REFERENCIAS = "referencias.csv"
BASE_DATOS = "precios.db"

USER_AGENT = (
    "ObservatorioDescuento/1.0 (investigacion academica; "
    "Universidad Colegio Mayor de Cundinamarca; "
    "contacto: hhsanchez@universidadmayor.edu.co)"
)

PAUSA_MIN = 8
PAUSA_MAX = 15
TIMEOUT = 25
REINTENTOS = 3

# Ningún producto de catálogo cuesta menos de esto en pesos colombianos.
# Por debajo del umbral la observación se marca, no se descarta.
PRECIO_MINIMO_PLAUSIBLE = 1000

# Un precio de referencia más de cinco veces mayor que el precio de venta
# casi siempre indica un error de extracción, no un descuento.
FACTOR_MAXIMO_REFERENCIA = 5

# --- Base de datos -----------------------------------------------------------

ESQUEMA = """
CREATE TABLE IF NOT EXISTS observaciones (
    id_observacion       INTEGER PRIMARY KEY AUTOINCREMENT,
    id_referencia        TEXT NOT NULL,
    minorista            TEXT NOT NULL,
    categoria            TEXT,
    url                  TEXT NOT NULL,
    momento_utc          TEXT NOT NULL,
    precio               REAL,
    precio_lista         REAL,
    moneda               TEXT,
    disponibilidad       TEXT,
    metodo               TEXT,
    estado               TEXT NOT NULL,
    detalle_error        TEXT,
    html_hash            TEXT
);
CREATE INDEX IF NOT EXISTS idx_ref_momento
    ON observaciones (id_referencia, momento_utc);
"""

# Columnas nuevas de la v2. Se agregan sin tocar los datos ya capturados.
COLUMNAS_NUEVAS = [
    ("nombre", "TEXT"),              # nombre del producto según la ficha
    ("precio_referencia", "REAL"),   # el valor tachado que anuncia la tienda
    ("descuento_anunciado", "REAL"),  # porcentaje que se deriva de ese valor
    ("metodo_referencia", "TEXT"),   # de dónde se sacó el precio tachado
    ("extracto", "TEXT"),            # respaldo crudo del dato extraído
    ("version_captura", "TEXT"),     # qué versión del programa lo capturó
]

CAMPOS = [
    "id_referencia", "minorista", "categoria", "url", "momento_utc",
    "precio", "precio_lista", "moneda", "disponibilidad", "metodo",
    "estado", "detalle_error", "html_hash", "nombre",
    "precio_referencia", "descuento_anunciado", "metodo_referencia",
    "extracto", "version_captura",
]


def abrir_bd():
    con = sqlite3.connect(BASE_DATOS)
    con.executescript(ESQUEMA)
    existentes = {f[1] for f in con.execute("PRAGMA table_info(observaciones)")}
    for nombre, tipo in COLUMNAS_NUEVAS:
        if nombre not in existentes:
            con.execute(f"ALTER TABLE observaciones ADD COLUMN {nombre} {tipo}")
    con.commit()
    return con


def guardar(con, registro):
    marcadores = ",".join("?" * len(CAMPOS))
    con.execute(
        f"INSERT INTO observaciones ({','.join(CAMPOS)}) VALUES ({marcadores})",
        tuple(registro.get(c) for c in CAMPOS),
    )
    con.commit()


# --- Conversión de números ---------------------------------------------------

def a_numero(valor):
    """
    Convierte a float lo que publiquen las tiendas:
      '$ 1.299.900'   -> 1299900.0
      '1299900.00'    -> 1299900.0
      '1.2629017E7'   -> 12629017.0   (notación científica)
      1299900         -> 1299900.0

    La v1 borraba la letra E de la notación científica y convertía
    12.629.017 pesos en 1,26 pesos. Aquí se intenta primero la lectura
    directa, que ya entiende ese formato.
    """
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)

    texto = str(valor).strip()
    if not texto:
        return None

    # Lectura directa: cubre '1299900.00' y '1.2629017E7'
    try:
        return float(texto)
    except ValueError:
        pass

    # Notación científica con símbolos alrededor: '$1.2629017E7 COP'
    cientifica = re.search(r"(-?\d+(?:\.\d+)?)[eE]([+-]?\d+)", texto)
    if cientifica:
        try:
            return float(f"{cientifica.group(1)}e{cientifica.group(2)}")
        except ValueError:
            pass

    # Formato con separadores de miles y decimales
    limpio = re.sub(r"[^\d.,]", "", texto)
    if not limpio:
        return None
    if "," in limpio and "." in limpio:
        limpio = limpio.replace(".", "").replace(",", ".")
    elif "," in limpio:
        limpio = limpio.replace(",", ".")
    elif limpio.count(".") > 1:
        limpio = limpio.replace(".", "")
    elif re.search(r"\.\d{3}$", limpio):
        limpio = limpio.replace(".", "")
    try:
        return float(limpio)
    except ValueError:
        return None


# --- Identificación del producto de la ficha ---------------------------------

def id_desde_url(url):
    """Extrae el código de producto de una URL tipo VTEX: .../p/195950797978"""
    m = re.search(r"/p/([^/?#]+)", url)
    if m:
        return m.group(1)
    return urlparse(url).path.rstrip("/").split("/")[-1]


def _recorrer(nodo):
    if isinstance(nodo, dict):
        yield nodo
        for v in nodo.values():
            yield from _recorrer(v)
    elif isinstance(nodo, list):
        for v in nodo:
            yield from _recorrer(v)


def _es_producto(d):
    tipo = d.get("@type")
    if isinstance(tipo, list):
        return "Product" in tipo
    return tipo == "Product"


def _coincide(producto, ident, url):
    """¿Este bloque Product corresponde a la ficha que estamos mirando?"""
    if not ident:
        return False
    campos = ["sku", "mpn", "productID", "gtin13", "gtin", "@id", "url"]
    for c in campos:
        v = producto.get(c)
        if v and ident in str(v):
            return True
    ruta = urlparse(url).path.rstrip("/")
    for c in ["@id", "url"]:
        v = producto.get(c)
        if v and ruta and ruta in str(v):
            return True
    return False


def _ofertas_de(producto):
    ofertas = producto.get("offers")
    if not ofertas:
        return []
    if isinstance(ofertas, dict):
        return [o for o in _recorrer(ofertas) if isinstance(o, dict)]
    if isinstance(ofertas, list):
        salida = []
        for o in ofertas:
            salida.extend(x for x in _recorrer(o) if isinstance(x, dict))
        return salida
    return []


def extraer_desde_jsonld(sopa, url):
    """
    Método preferido: datos estructurados schema.org de la propia ficha.

    La v1 devolvía el primer diccionario con clave 'price' que encontrara,
    que en páginas con carrusel de productos relacionados podía ser el
    precio de otro producto. Aquí se exige que el bloque Product
    corresponda al código de la URL.
    """
    ident = id_desde_url(url)
    productos = []
    for etiqueta in sopa.find_all("script", type="application/ld+json"):
        try:
            datos = json.loads(etiqueta.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        productos.extend(d for d in _recorrer(datos)
                         if isinstance(d, dict) and _es_producto(d))

    if not productos:
        return None

    # Primero el que coincide con el código de la URL; si ninguno coincide
    # y solo hay uno en la página, se acepta ese.
    elegidos = [p for p in productos if _coincide(p, ident, url)]
    if not elegidos:
        if len(productos) == 1:
            elegidos = productos
        else:
            return None

    producto = elegidos[0]
    for oferta in _ofertas_de(producto):
        precio = a_numero(oferta.get("price") or oferta.get("lowPrice"))
        if not precio:
            continue
        return {
            "precio": precio,
            "moneda": oferta.get("priceCurrency"),
            "disponibilidad": str(oferta.get("availability") or ""),
            "metodo": "json-ld",
            "extracto": json.dumps(
                {k: oferta.get(k) for k in
                 ("price", "lowPrice", "highPrice", "listPrice",
                  "priceCurrency", "availability")},
                ensure_ascii=False),
        }
    return None


def extraer_desde_meta(sopa):
    """Respaldo: etiquetas OpenGraph e itemprop."""
    selectores = [
        ("meta", {"property": "product:price:amount"}, "content"),
        ("meta", {"property": "og:price:amount"}, "content"),
        ("meta", {"itemprop": "price"}, "content"),
    ]
    for nombre, attrs, campo in selectores:
        etiqueta = sopa.find(nombre, attrs=attrs)
        if etiqueta and etiqueta.get(campo):
            crudo = etiqueta[campo]
            precio = a_numero(crudo)
            if precio:
                return {"precio": precio, "moneda": None,
                        "disponibilidad": "", "metodo": "meta",
                        "extracto": f"meta={crudo}"}
    return None


def extraer_desde_selector(sopa, selector_css):
    """Último recurso: selector CSS anotado a mano en el CSV."""
    if not selector_css:
        return None
    nodo = sopa.select_one(selector_css)
    if nodo:
        crudo = nodo.get_text()
        precio = a_numero(crudo)
        if precio:
            return {"precio": precio, "moneda": None,
                    "disponibilidad": "", "metodo": "selector",
                    "extracto": f"selector={crudo.strip()[:80]}"}
    return None


# --- Nombre del producto ----------------------------------------------------

def extraer_nombre(sopa, url):
    """
    Nombre legible de la ficha. Sin esto los productos solo se identifican
    por su código, que no dice nada a quien lee los resultados.
    Se prefiere el dato estructurado; si falta, el título de la página.
    """
    ident = id_desde_url(url)

    for etiqueta in sopa.find_all("script", type="application/ld+json"):
        try:
            datos = json.loads(etiqueta.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        productos = [d for d in _recorrer(datos)
                     if isinstance(d, dict) and _es_producto(d)]
        elegidos = [p for p in productos if _coincide(p, ident, url)]
        if not elegidos and len(productos) == 1:
            elegidos = productos
        for p in elegidos:
            nombre = p.get("name")
            if nombre and str(nombre).strip():
                return str(nombre).strip()[:180]

    for attrs in ({"property": "og:title"}, {"name": "title"}):
        etiqueta = sopa.find("meta", attrs=attrs)
        if etiqueta and etiqueta.get("content", "").strip():
            return etiqueta["content"].strip()[:180]

    h1 = sopa.find("h1")
    if h1 and h1.get_text().strip():
        return h1.get_text().strip()[:180]

    if sopa.title and sopa.title.string:
        return sopa.title.string.strip()[:180]

    return None


# --- Precio de referencia (el valor tachado) ---------------------------------

# Las plataformas VTEX incrustan el precio de lista en los datos internos
# de la página con alguno de estos nombres.
CLAVES_REFERENCIA = [
    "listPrice", "ListPrice", "listPriceWithTax",
    "priceWithoutDiscount", "PriceWithoutDiscount",
    "oldPrice", "OldPrice", "regularPrice", "originalPrice",
]

PATRON_REFERENCIA = re.compile(
    r'"(' + "|".join(CLAVES_REFERENCIA) + r')"\s*:\s*"?(-?[\d.,eE+]+)"?'
)


def extraer_precio_referencia(html, sopa, precio_venta, url):
    """
    Busca el valor tachado que la tienda muestra como precio anterior.
    Esta es la variable central de la investigación: sin ella no se puede
    contrastar el descuento anunciado con el descuento efectivo.

    Se descartan los candidatos implausibles: menores o iguales al precio
    de venta, o desproporcionadamente mayores, que casi siempre provienen
    de otro producto de la misma página.
    """
    if not precio_venta:
        return None, None

    candidatos = []

    # 1. Datos internos de la plataforma incrustados en el HTML
    for clave, crudo in PATRON_REFERENCIA.findall(html):
        valor = a_numero(crudo)
        if valor:
            candidatos.append((valor, f"json:{clave}"))

    # 2. Etiquetas meta de precio de lista
    for attrs in ({"property": "product:original_price:amount"},
                  {"itemprop": "listPrice"}):
        etiqueta = sopa.find("meta", attrs=attrs)
        if etiqueta and etiqueta.get("content"):
            valor = a_numero(etiqueta["content"])
            if valor:
                candidatos.append((valor, "meta:listPrice"))

    # 3. Texto tachado visible en la ficha
    for nodo in sopa.select("del, s, strike, [class*=tachado], "
                            "[class*=strike], [class*=old-price], "
                            "[class*=listPrice], [class*=list-price]"):
        valor = a_numero(nodo.get_text())
        if valor:
            candidatos.append((valor, "tachado"))

    validos = [
        (v, m) for v, m in candidatos
        if v > precio_venta and v <= precio_venta * FACTOR_MAXIMO_REFERENCIA
    ]
    if not validos:
        return None, None

    # El más frecuente entre los válidos; en empate, el menor.
    conteo = {}
    for v, m in validos:
        conteo.setdefault(round(v, 2), []).append(m)
    mejor = max(conteo.items(), key=lambda kv: (len(kv[1]), -kv[0]))
    return mejor[0], mejor[1][0]


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
        # Sin robots.txt legible se procede con cautela: no se consulta.
        return False
    return rp.can_fetch(USER_AGENT, url)


# --- Captura -----------------------------------------------------------------

VERSION = "v2-2026-09"


def capturar(fila, sesion):
    url = fila["url"]
    reg = {
        "id_referencia": fila["id"],
        "minorista": fila["minorista"],
        "categoria": fila.get("categoria", ""),
        "url": url,
        "momento_utc": datetime.now(timezone.utc).isoformat(),
        "version_captura": VERSION,
    }

    if not permitido(url):
        reg.update(estado="bloqueado_robots",
                   detalle_error="robots.txt no autoriza el acceso")
        return reg

    ultimo_error = ""
    for intento in range(REINTENTOS):
        try:
            r = sesion.get(url, timeout=TIMEOUT)
            if r.status_code != 200:
                ultimo_error = f"HTTP {r.status_code}"
                time.sleep(5 * (intento + 1))
                continue

            html = r.text
            sopa = BeautifulSoup(html, "html.parser")
            # Huella estable: la misma página produce siempre el mismo valor,
            # lo que permite verificar la integridad del registro después.
            reg["html_hash"] = hashlib.sha256(html.encode("utf-8")).hexdigest()

            reg["nombre"] = extraer_nombre(sopa, url)

            resultado = (
                extraer_desde_jsonld(sopa, url)
                or extraer_desde_meta(sopa)
                or extraer_desde_selector(sopa, fila.get("selector"))
            )

            if not resultado:
                reg.update(estado="sin_precio",
                           detalle_error="Página cargada sin precio identificable")
                return reg

            precio = resultado["precio"]
            reg.update(
                precio=precio,
                moneda=resultado.get("moneda"),
                disponibilidad=resultado.get("disponibilidad"),
                metodo=resultado["metodo"],
                extracto=resultado.get("extracto"),
                estado="ok",
            )

            referencia, metodo_ref = extraer_precio_referencia(
                html, sopa, precio, url)
            if referencia:
                reg["precio_referencia"] = referencia
                reg["precio_lista"] = referencia
                reg["metodo_referencia"] = metodo_ref
                reg["descuento_anunciado"] = round(
                    (1 - precio / referencia) * 100, 2)

            # Un precio implausible se guarda igual, pero marcado, para que
            # no contamine el cálculo del mínimo de la ventana de referencia.
            if precio < PRECIO_MINIMO_PLAUSIBLE:
                reg.update(
                    estado="precio_sospechoso",
                    detalle_error=f"Valor implausible: {precio}")
            return reg

        except requests.RequestException as e:
            ultimo_error = type(e).__name__
            time.sleep(5 * (intento + 1))

    reg.update(estado="error", detalle_error=ultimo_error)
    return reg


def main():
    try:
        with open(ARCHIVO_REFERENCIAS, newline="", encoding="utf-8") as f:
            referencias = list(csv.DictReader(f))
    except FileNotFoundError:
        print(f"Falta {ARCHIVO_REFERENCIAS}. Columnas esperadas: "
              "id,minorista,categoria,url,selector")
        sys.exit(1)

    con = abrir_bd()
    sesion = requests.Session()
    sesion.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "es-CO,es;q=0.9",
    })

    ok = fallos = con_referencia = 0
    for i, fila in enumerate(referencias, 1):
        reg = capturar(fila, sesion)
        guardar(con, reg)

        etiqueta = f"[{i}/{len(referencias)}] {reg['id_referencia']}"
        if reg["estado"] == "ok":
            ok += 1
            titulo = (reg.get("nombre") or reg["id_referencia"])[:44]
            linea = f"{etiqueta}: {titulo} — {reg['precio']:,.0f}"
            if reg.get("precio_referencia"):
                con_referencia += 1
                linea += (f" | antes {reg['precio_referencia']:,.0f}"
                          f" = -{reg['descuento_anunciado']:.0f}%")
            print(linea)
        else:
            fallos += 1
            print(f"{etiqueta}: {reg['estado']} {reg.get('detalle_error') or ''}")

        time.sleep(random.uniform(PAUSA_MIN, PAUSA_MAX))

    con.close()
    print(f"\nCaptura terminada. Correctas: {ok} | Fallidas: {fallos}")
    print(f"Con precio de referencia anunciado: {con_referencia} de {ok}")
    print("Los registros fallidos también se guardan: un vacío en la serie "
          "no es lo mismo que un precio sin variación.")


if __name__ == "__main__":
    main()
