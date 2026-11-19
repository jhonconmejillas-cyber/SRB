"""
Analiza los resultados de las corridas de carga y calcula las metricas del informe:

  - TPM: transacciones por minuto que el sistema completo (respuestas recibidas).
  - Tiempo de respuesta visto por el cliente: promedio, mediana, p95 y maximo,
    en total y por tipo de operacion.
  - Cuantas salieron bien, fallaron por reglas del negocio o dieron error.
  - Tolerancia a fallos: reintentos y peticiones atendidas por gt_1 / gt_2.
  - (opcional) Lo medido en el GT (--gt): TPM y tiempo dentro del sistema.

Cada carpeta es una corrida de capa_cliente.escenarios, con un cliente_<id>.csv por cliente.
Si la carpeta se llama rmc<N>_<M>clientes (ej. rmc2_5clientes) se sabe cuantos RMC y
clientes tenia, y se pueden hacer las graficas de TPM y tiempo contra clientes.

Uso:
    python -m herramientas.analizar_metricas resultados/rmc1_3clientes
    python -m herramientas.analizar_metricas resultados/rmc*_*clientes --graficas
    python -m herramientas.analizar_metricas resultados/rmc1_3clientes --gt resultados/gt_metricas.csv

Salida:
    una tabla en pantalla, resultados/resumen_metricas.csv (para Excel o el informe)
    y, con --graficas, resultados/grafica_tpm.png y resultados/grafica_tiempo.png
    (las graficas necesitan:  pip install matplotlib)
"""
import argparse
import csv
import glob
import re
import statistics
import sys
from pathlib import Path

from config import configuracion as config

TIPOS = ["CONSULTA", "RESERVA", "MODIFICACION", "CANCELACION"]

# Colores de las series (1, 2 y 3 RMC): validados para daltonismo.
# Cada linea lleva tambien su etiqueta y un marcador distinto, para no depender del color.
COLORES = ["#2a78d6", "#eb6834", "#1baf7a"]
MARCADORES = ["o", "s", "^"]


# ====================================================================== lectura
def leer_corrida(carpeta):
    """Lee todos los cliente_*.csv de una carpeta y devuelve una lista de filas (dict)."""
    filas = []
    for ruta in sorted(Path(carpeta).glob("cliente_*.csv")):
        with open(ruta, encoding="utf-8") as archivo:
            for fila in csv.DictReader(archivo):
                fila["hora_envio"] = float(fila["hora_envio"])
                fila["ms"] = float(fila["ms"])
                # columnas nuevas (los CSV viejos no las tienen)
                fila["intentos"] = int(fila.get("intentos") or 1)
                fila["gt"] = fila.get("gt") or "1"
                filas.append(fila)
    return filas


def configuracion_de(carpeta, filas):
    """De 'rmc2_5clientes' saca (2, 5). Si el nombre no sigue ese formato: (None, clientes)."""
    encontrado = re.search(r"rmc(\d+)_(\d+)clientes", Path(carpeta).name)
    if encontrado:
        return int(encontrado.group(1)), int(encontrado.group(2))
    return None, len({f["id_cliente"] for f in filas})


# ====================================================================== calculos
def percentil(valores, p):
    """El valor por debajo del cual queda el p% de los datos (p95: el 95%)."""
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    k = (len(ordenados) - 1) * p / 100
    abajo = int(k)
    arriba = min(abajo + 1, len(ordenados) - 1)
    return ordenados[abajo] + (ordenados[arriba] - ordenados[abajo]) * (k - abajo)


def tiempos(valores):
    """Promedio, mediana, p95 y maximo de una lista de milisegundos."""
    if not valores:
        return {"promedio": 0.0, "mediana": 0.0, "p95": 0.0, "maximo": 0.0}
    return {"promedio": statistics.mean(valores), "mediana": statistics.median(valores),
            "p95": percentil(valores, 95), "maximo": max(valores)}


def es_sin_respuesta(fila):
    """El cliente se rindio: ningun GT respondio. No cuenta como transaccion completada."""
    return fila["estado"] == "ERROR" and fila["motivo"] == "NO_DISPONIBLE"


def analizar(carpeta, filas, ruta_gt=None):
    """Calcula todas las metricas de una corrida. Devuelve un dict."""
    rmc, clientes = configuracion_de(carpeta, filas)
    # Ventana de la corrida: desde la primera peticion hasta que llego la ultima respuesta
    inicio = min(f["hora_envio"] for f in filas)
    fin = max(f["hora_envio"] + f["ms"] / 1000 for f in filas)
    minutos = max(fin - inicio, 1e-9) / 60

    completadas = [f for f in filas if not es_sin_respuesta(f)]
    r = {
        "carpeta": Path(carpeta).name, "rmc": rmc, "clientes": clientes,
        "duracion_s": round(fin - inicio, 1),
        "enviadas": len(filas),
        "completadas": len(completadas),
        # TPM = transacciones completadas / minutos que duro la corrida
        "tpm": len(completadas) / minutos,
        "exito": sum(1 for f in filas if f["estado"] in ("EXITO", "ACK")),
        "fallo_negocio": sum(1 for f in filas if f["estado"] == "FALLO"),
        "error": sum(1 for f in filas if f["estado"] == "ERROR"),
        "sin_respuesta": len(filas) - len(completadas),
        "reintentos": sum(f["intentos"] - 1 for f in filas),
        "por_gt1": sum(1 for f in filas if f["gt"] == "1"),
        "por_gt2": sum(1 for f in filas if f["gt"] == "2"),
    }
    for clave, valor in tiempos([f["ms"] for f in completadas]).items():
        r[f"ms_{clave}"] = valor
    # Por tipo de operacion, lo que pide la Tabla 1: tiempo promedio, desviacion
    # estandar y TPM (la cancelacion mide solo el ACK: es asincrona)
    for tipo in TIPOS:
        de_tipo = [f["ms"] for f in completadas if f["tipo"] == tipo]
        t = tipo.lower()
        r[f"n_{t}"] = len(de_tipo)
        r[f"ms_{t}"] = statistics.mean(de_tipo) if de_tipo else 0.0
        # stdev necesita al menos 2 datos
        r[f"desv_{t}"] = statistics.stdev(de_tipo) if len(de_tipo) > 1 else 0.0
        r[f"tpm_{t}"] = len(de_tipo) / minutos

    if ruta_gt:
        r.update(analizar_gt(ruta_gt, inicio, fin))
    return r


def analizar_gt(ruta_gt, inicio, fin):
    """
    Lo medido en el GT durante la misma ventana de tiempo de la corrida.
    El CSV del GT acumula todas las corridas, por eso se filtra por hora.
    El tiempo del GT no incluye la red entre el cliente y el GT.
    """
    filas = []
    with open(ruta_gt, encoding="utf-8") as archivo:
        for fila in csv.DictReader(archivo):
            hora = float(fila["hora_fin"])
            if inicio <= hora <= fin + 1:
                filas.append(float(fila["ms"]))
    minutos = max(fin - inicio, 1e-9) / 60
    return {"gt_transacciones": len(filas), "gt_tpm": len(filas) / minutos,
            "gt_ms_promedio": statistics.mean(filas) if filas else 0.0}


# ====================================================================== salida
def imprimir(resultados):
    """Tabla resumen en pantalla."""
    print(f"\n{'corrida':<20}{'RMC':>4}{'cli':>5}{'TPM':>8}{'prom ms':>9}{'p95 ms':>9}"
          f"{'max ms':>9}{'exito':>7}{'fallo':>7}{'error':>7}{'reint':>7}{'gt2':>6}")
    for r in resultados:
        print(f"{r['carpeta']:<20}{r['rmc'] or '-':>4}{r['clientes']:>5}{r['tpm']:>8.1f}"
              f"{r['ms_promedio']:>9.1f}{r['ms_p95']:>9.1f}{r['ms_maximo']:>9.1f}"
              f"{r['exito']:>7}{r['fallo_negocio']:>7}{r['error']:>7}{r['reintentos']:>7}{r['por_gt2']:>6}")
    print("\nPor tipo (Tabla 1): promedio ms +- desviacion estandar, TPM")
    for r in resultados:
        partes = "  ".join(
            f"{t.lower()} {r[f'ms_{t.lower()}']:.1f}+-{r[f'desv_{t.lower()}']:.1f} ms "
            f"TPM {r[f'tpm_{t.lower()}']:.1f}"
            for t in TIPOS if r[f"n_{t.lower()}"])
        print(f"  {r['carpeta']:<20}{partes}")
    if "gt_tpm" in resultados[0]:
        print("\nMedido en el GT:")
        for r in resultados:
            print(f"  {r['carpeta']:<20}TPM {r['gt_tpm']:.1f}  promedio {r['gt_ms_promedio']:.1f} ms "
                  f"({r['gt_transacciones']} transacciones)")


def guardar_csv(resultados, ruta):
    """Una fila por corrida, todas las columnas: para Excel o para las tablas del informe."""
    columnas = list(resultados[0].keys())
    with open(ruta, "w", newline="", encoding="utf-8") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=columnas)
        escritor.writeheader()
        for r in resultados:
            escritor.writerow({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()})
    print(f"\nResumen guardado en {ruta}")


def graficar(resultados, carpeta_salida):
    """
    Dos graficas (una por medida, nunca dos ejes en la misma):
      grafica_tpm.png:    TPM contra numero de clientes, una linea por cantidad de RMC
      grafica_tiempo.png: tiempo promedio de respuesta contra clientes, una linea por RMC
    """
    try:
        import matplotlib
        matplotlib.use("Agg")                  # dibujar a archivo, sin abrir ventanas
        import matplotlib.pyplot as plt
    except ImportError:
        print("Para las graficas instala matplotlib:  pip install matplotlib")
        return

    con_rmc = [r for r in resultados if r["rmc"] is not None]
    if not con_rmc:
        print("Las graficas necesitan carpetas con nombre rmc<N>_<M>clientes")
        return
    cantidades_rmc = sorted({r["rmc"] for r in con_rmc})

    medidas = [("tpm", "Transacciones por minuto (TPM)", "TPM", "grafica_tpm.png"),
               ("ms_promedio", "Tiempo promedio de respuesta", "milisegundos", "grafica_tiempo.png")]
    for clave, titulo, eje_y, archivo in medidas:
        figura, eje = plt.subplots(figsize=(7, 4.2), dpi=150)
        figura.patch.set_facecolor("#fcfcfb")
        eje.set_facecolor("#fcfcfb")
        for i, n_rmc in enumerate(cantidades_rmc):
            puntos = sorted((r["clientes"], r[clave]) for r in con_rmc if r["rmc"] == n_rmc)
            x = [p[0] for p in puntos]
            y = [p[1] for p in puntos]
            color = COLORES[i % len(COLORES)]
            nombre = f"{n_rmc} RMC"
            eje.plot(x, y, color=color, linewidth=2, marker=MARCADORES[i % len(MARCADORES)],
                     markersize=7, markeredgecolor="#fcfcfb", markeredgewidth=1.5, label=nombre)
            # etiqueta directa al final de cada linea (en tinta de texto, no del color)
            eje.annotate(nombre, (x[-1], y[-1]), xytext=(8, 0), textcoords="offset points",
                         va="center", fontsize=9, color="#52514e")
        eje.set_title(titulo, loc="left", fontsize=12, color="#0b0b0b")
        eje.set_xlabel("Clientes concurrentes", color="#52514e")
        eje.set_ylabel(eje_y, color="#52514e")
        clientes = sorted({r["clientes"] for r in con_rmc})
        eje.set_xticks(clientes)
        eje.set_ylim(bottom=0)
        eje.grid(axis="y", color="#e4e3df", linewidth=0.8)
        for lado in ("top", "right"):
            eje.spines[lado].set_visible(False)
        for lado in ("left", "bottom"):
            eje.spines[lado].set_color("#c3c2b7")
        eje.tick_params(colors="#52514e")
        if len(cantidades_rmc) > 1:
            eje.legend(frameon=False, loc="upper left", fontsize=9)
        figura.tight_layout()
        ruta = Path(carpeta_salida) / archivo
        figura.savefig(ruta)
        plt.close(figura)
        print(f"Grafica guardada en {ruta}")


# ====================================================================== main
def main():
    parser = argparse.ArgumentParser(description="Metricas de las corridas de carga")
    parser.add_argument("carpetas", nargs="+", help="carpetas de resultados (acepta comodines: rmc*_*clientes)")
    parser.add_argument("--gt", help="CSV de metricas del GT (resultados/gt_metricas.csv en la maquina del GT)")
    parser.add_argument("--graficas", action="store_true", help="generar las graficas PNG (necesita matplotlib)")
    parser.add_argument("--salida", default=str(config.RAIZ / "resultados"), help="carpeta para el resumen y graficas")
    args = parser.parse_args()

    # En Windows PowerShell los comodines no se expanden solos: se expanden aqui
    carpetas = []
    for patron in args.carpetas:
        carpetas.extend(sorted(glob.glob(patron)) or [patron])

    resultados = []
    for carpeta in carpetas:
        filas = leer_corrida(carpeta)
        if not filas:
            print(f"{carpeta}: no tiene archivos cliente_*.csv, se omite")
            continue
        resultados.append(analizar(carpeta, filas, args.gt))
    if not resultados:
        print("No hay datos para analizar.")
        sys.exit(1)
    # orden: por cantidad de RMC y luego por clientes
    resultados.sort(key=lambda r: (r["rmc"] or 0, r["clientes"]))

    Path(args.salida).mkdir(parents=True, exist_ok=True)
    imprimir(resultados)
    guardar_csv(resultados, Path(args.salida) / "resumen_metricas.csv")
    if args.graficas:
        graficar(resultados, args.salida)


if __name__ == "__main__":
    main()
