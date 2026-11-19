"""
Proceso cliente. Dos modos:

  Caso de prueba (archivo de operaciones):
    python -m capa_cliente.main --id 1 --script pruebas/caso_basico.txt [--detalle]

  Carga continua (para las metricas):
    python -m capa_cliente.main --id 1 --carga --duracion 150 --por-minuto 20 \
        --consultas 50 --modificaciones 20 --cancelaciones 10
    (el porcentaje que sobra son reservas)
"""
import argparse

from config import configuracion as config
from capa_cliente.carga import generar_carga
from capa_cliente.cliente import Cliente
from capa_cliente.script import ejecutar_script


def main():
    # --- leer los argumentos de la linea de comandos
    parser = argparse.ArgumentParser(description="Cliente del Sistema de Reserva de Boletos")
    parser.add_argument("--id", type=int, required=True, help="identificador del cliente (mayor que 0)")
    modo = parser.add_mutually_exclusive_group(required=True)     # o --script o --carga, no ambos
    modo.add_argument("--script", help="archivo con las operaciones a ejecutar")
    modo.add_argument("--carga", action="store_true", help="generar carga continua")
    parser.add_argument("--detalle", action="store_true",
                        help="(script) en las consultas, mostrar las fechas que tienen reservas")
    parser.add_argument("--duracion", type=float, default=150, help="(carga) segundos, por defecto 150")
    parser.add_argument("--por-minuto", type=float, default=20, help="(carga) peticiones por minuto")
    parser.add_argument("--consultas", type=float, default=70,
                        help="(carga) porcentaje de consultas")
    parser.add_argument("--modificaciones", type=float, default=0,
                        help="(carga) porcentaje de modificaciones")
    parser.add_argument("--cancelaciones", type=float, default=0,
                        help="(carga) porcentaje de cancelaciones; el resto son reservas")
    parser.add_argument("--salida", help="(carga) CSV de resultados; por defecto resultados/cliente_<id>.csv")
    parser.add_argument("--semilla", type=int, help="(carga) semilla del azar, para repetir una corrida")
    parser.add_argument("--gts", help="GT separados por coma, primero el primario "
                                      "(ej. 10.43.99.232,10.43.100.84); por defecto GT_HOSTS del .env")
    args = parser.parse_args()

    # --- revisar que los numeros tengan sentido
    if args.id <= 0:
        parser.error("--id debe ser un entero mayor que 0")
    if args.carga and args.por_minuto <= 0:
        parser.error("--por-minuto debe ser mayor que 0")
    porcentajes = (args.consultas, args.modificaciones, args.cancelaciones)
    if args.carga and (min(porcentajes) < 0 or sum(porcentajes) > 100):
        parser.error("los porcentajes deben ser >= 0 y sumar maximo 100")

    # Si se dan --gts, reemplazan a GT_HOSTS del .env
    gts = [x.strip() for x in args.gts.split(",") if x.strip()] if args.gts else None
    cliente = Cliente(args.id, gts=gts)
    try:
        if args.script:
            resumen = ejecutar_script(cliente, args.script, args.detalle)
            print(f"[C{args.id}] terminado: {resumen}")
        else:
            salida = args.salida or config.RAIZ / "resultados" / f"cliente_{args.id}.csv"
            print(f"[C{args.id}] carga: {args.por_minuto:g}/min, {args.consultas:g}% consultas, "
                  f"{args.modificaciones:g}% modificaciones, {args.cancelaciones:g}% cancelaciones, "
                  f"{args.duracion:g} s -> {salida}", flush=True)
            enviadas = generar_carga(cliente, salida, args.duracion, args.por_minuto,
                                     args.consultas, args.modificaciones, args.cancelaciones,
                                     semilla=args.semilla)
            sin_aviso = cliente.esperar_notificaciones(10)    # avisos de cancelaciones pendientes
            print(f"[C{args.id}] terminado: {enviadas} peticiones, "
                  f"{len(cliente.notificaciones)} notificaciones recibidas, "
                  f"{cliente.total_reintentos} reintentos, {cliente.cambios_de_gt} cambios de GT"
                  + (f", {sin_aviso} sin aviso" if sin_aviso else ""), flush=True)
    except FileNotFoundError:
        print(f"No existe el archivo {args.script}")
    except RuntimeError as e:
        print(f"[C{args.id}] {e}")
    except KeyboardInterrupt:
        print(f"[C{args.id}] interrumpido")
    finally:
        cliente.cerrar()


if __name__ == "__main__":
    main()
