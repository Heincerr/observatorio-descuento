#!/usr/bin/env python3
"""
Observatorio del Descuento — descubrimiento de referencias
Autor: Harry Heincer Jawer Sanchez Arango

Construye referencias.csv automáticamente leyendo el sitemap público
de cada minorista. Muestreo sistemático, no selección manual: elimina
el sesgo de escoger a dedo los productos.

Uso:
    python descubrir_urls.py

Requiere:
    pip install requests
"""

import csv
import gzip
import io
import random
import re
import sys
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

# --- Configuración -----------------------------------------------------------

SALIDA = "referencias.csv"
POR_MINORISTA = 40          # cuántos productos tomar de cada tienda
SEMILLA = 2026              # fija el muestreo para que sea reproducible

USER_AGENT = (
    "ObservatorioDescuento/1.0 (investigacion academica; "
    "Universidad Colegio Mayor de Cundinamarca; "
    "contacto: hhsanchez@universidadmayor.edu.co)"
)

# Tiendas a explorar. Agrega o quita según lo que te responda.
MINORISTAS = {
    "ktronix":  "https://www.ktronix.com",
    "alkosto":  "https://www.alkosto.com",
    "falabella": "https://www.falabella.com.co",
    "exito":    "https://www.exito.com",
}

# Patrón de URL de página de producto. La mayoría de tiendas colombianas
# usan plataforma VTEX, donde las fichas de producto terminan en /p
PATRON_PRODUCTO = re.compile(r"/p(/|$|\?)")

TIMEOUT = 30
PAUSA = 3

sesion = requests.Session()
sesion.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "es-CO"})


# --- Utilidades --------------------------------------------------------------

def permitido(url):
    """Respeta robots.txt. Si no permite, el minorista se descarta."""
    partes = urlparse(url)
    rp = RobotFileParser()
    rp.set_url(f"{partes.scheme}://{partes.netloc}/robots.txt")
    try:
        rp.read()
    except Exception:
        return True
    return rp.can_fetch(USER_AGENT, url)


def bajar_texto(url):
    """Descarga una URL y descomprime si viene en .gz"""
    r = sesion.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    contenido = r.content
    if url.endswith(".gz") or contenido[:2] == b"\x1f\x8b":
        contenido = gzip.GzipFile(fileobj=io.BytesIO(contenido)).read()
    return contenido.decode("utf-8", errors="ignore")


def extraer_locs(xml):
    """Saca todas las etiquetas <loc> de un sitemap."""
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def sitemaps_desde_robots(base):
    """Los sitemaps suelen estar declarados en robots.txt"""
    try:
        texto = bajar_texto(f"{base}/robots.txt")
    except Exception:
        return []
    return re.findall(r"(?im)^\s*sitemap:\s*(\S+)", texto)


# --- Recolección de URLs de producto ----------------------------------------

def urls_de_producto(nombre, base, objetivo):
    print(f"\n--- {nombre} ---")

    candidatos = sitemaps_desde_robots(base) or [f"{base}/sitemap.xml"]
    print(f"  sitemaps declarados: {len(candidatos)}")

    encontradas = []
    pendientes = list(candidatos)
    revisados = set()

    while pendientes and len(encontradas) < objetivo * 5:
        sm = pendientes.pop(0)
        if sm in revisados:
            continue
        revisados.add(sm)

        try:
            xml = bajar_texto(sm)
        except Exception as e:
            print(f"  no se pudo leer {sm[:60]}: {type(e).__name__}")
            continue

        locs = extraer_locs(xml)
        productos = [u for u in locs if PATRON_PRODUCTO.search(u)]
        subsitemaps = [u for u in locs if u.endswith((".xml", ".xml.gz"))]

        encontradas.extend(productos)
        # Prioriza sub-sitemaps que huelan a producto
        pendientes = [s for s in subsitemaps if "produc" in s.lower()] + pendientes

        if productos:
            print(f"  +{len(productos)} productos en {sm.split('/')[-1][:40]}")
        time.sleep(PAUSA)

    # Quita duplicados conservando el orden
    unicas = list(dict.fromkeys(encontradas))
    print(f"  total encontradas: {len(unicas)}")

    if len(unicas) <= objetivo:
        return unicas

    # Muestreo sistemático: recorre la lista a intervalos regulares.
    # Es reproducible y evita concentrarse en una sola categoría.
    paso = len(unicas) / objetivo
    return [unicas[int(i * paso)] for i in range(objetivo)]


def main():
    random.seed(SEMILLA)
    filas = []
    contador = 1

    for nombre, base in MINORISTAS.items():
        if not permitido(f"{base}/sitemap.xml"):
            print(f"\n--- {nombre} ---\n  robots.txt no permite el acceso. "
                  f"Se descarta (esto es un dato, anótalo).")
            continue
        try:
            urls = urls_de_producto(nombre, base, POR_MINORISTA)
        except Exception as e:
            print(f"  fallo general en {nombre}: {type(e).__name__}")
            continue

        for u in urls:
            filas.append({
                "id": f"REF{contador:04d}",
                "minorista": nombre,
                "categoria": "",
                "url": u,
                "selector": "",
            })
            contador += 1

    if not filas:
        print("\nNo se obtuvo ninguna URL. Revisa la conexión o los sitemaps.")
        sys.exit(1)

    with open(SALIDA, "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(
            f, fieldnames=["id", "minorista", "categoria", "url", "selector"])
        escritor.writeheader()
        escritor.writerows(filas)

    print(f"\nListo: {len(filas)} referencias escritas en {SALIDA}")
    for nombre in MINORISTAS:
        n = sum(1 for f_ in filas if f_["minorista"] == nombre)
        if n:
            print(f"  {nombre}: {n}")
    print("\nRevisa el archivo antes de correr la captura. "
          "Si alguna URL no es de producto, bórrala.")


if __name__ == "__main__":
    main()
