#!/usr/bin/env bash
# =====================================================================
#  Arranca o detiene la capa de negocio de UNA maquina (NBIG291 o NBIG381):
#  broker + GT + N workers RMC, cada uno en segundo plano con su log.
#
#  Uso (desde la raiz del proyecto):
#      bash despliegue/negocio.sh arrancar 2      # broker, GT y 2 RMC
#      bash despliegue/negocio.sh rmc 1           # agregar 1 RMC mas (escalar)
#      bash despliegue/negocio.sh estado          # que esta corriendo
#      bash despliegue/negocio.sh parar           # detener todo
#
#  Logs en logs/: broker.log, gt.log, rmc_1.log, rmc_2.log ...
#  Prueba de caida del GT:  kill -9 $(cat logs/gt.pid)
# =====================================================================
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$RAIZ/venv/bin/python}"     # se puede cambiar: PYTHON=python3 bash ...
LOGS="$RAIZ/logs"
mkdir -p "$LOGS"
cd "$RAIZ"

# Lanza un proceso en segundo plano y guarda su PID en logs/<nombre>.pid
lanzar() {
    nombre=$1; shift
    nohup "$PYTHON" -u -m "$@" > "$LOGS/$nombre.log" 2>&1 &
    echo $! > "$LOGS/$nombre.pid"
    echo "  $nombre  (pid $!)  -> logs/$nombre.log"
}

# Lanza N workers RMC numerados despues de los que ya hay
lanzar_rmc() {
    cantidad=${1:-1}
    ya=$(ls "$LOGS"/rmc_*.pid 2>/dev/null | wc -l)
    for i in $(seq $((ya + 1)) $((ya + cantidad))); do
        lanzar "rmc_$i" capa_negocios.main rmc
    done
}

case "$1" in
    arrancar)
        echo "Arrancando la capa de negocio:"
        lanzar broker capa_negocios.main broker
        sleep 0.5                                 # que el broker abra sus puertos primero
        lanzar gt capa_negocios.main gt
        lanzar_rmc "${2:-1}"
        ;;
    rmc)
        lanzar_rmc "${2:-1}"
        ;;
    estado)
        for f in "$LOGS"/*.pid; do
            [ -e "$f" ] || { echo "No hay nada corriendo."; break; }
            pid=$(cat "$f")
            if kill -0 "$pid" 2>/dev/null; then estado="corriendo"; else estado="DETENIDO"; fi
            echo "  $(basename "$f" .pid)  pid $pid  $estado"
        done
        ;;
    parar)
        for f in "$LOGS"/*.pid; do
            [ -e "$f" ] || continue
            kill "$(cat "$f")" 2>/dev/null && echo "  detenido $(basename "$f" .pid)"
            rm -f "$f"
        done
        ;;
    *)
        echo "uso: bash despliegue/negocio.sh [arrancar N | rmc N | estado | parar]"
        exit 1
        ;;
esac
