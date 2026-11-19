#!/usr/bin/env bash
# =====================================================================
#  Instala la persistencia y el servicio de cancelacion como servicios
#  de systemd en NBIG296. Con Restart=always, si el proceso muere (se cae,
#  kill -9, error), Linux lo vuelve a lanzar en 1 segundo. Los datos no se
#  pierden: estan en el archivo datos/srb.db.
#
#  Uso (en NBIG296, desde la raiz del proyecto):
#      bash despliegue/instalar_servicios.sh            # instala y arranca
#      bash despliegue/instalar_servicios.sh quitar     # detiene y desinstala
#
#  Ver lo que pasa:   journalctl -u srb-persistencia -f
#  Estado:            systemctl status srb-persistencia srb-cancelacion
# =====================================================================
set -e

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"        # carpeta del proyecto
PYTHON="$RAIZ/venv/bin/python"                    # python del entorno virtual
USUARIO="$(whoami)"
SERVICIOS="srb-persistencia srb-cancelacion"

if [ "$1" = "quitar" ]; then
    for s in $SERVICIOS; do
        sudo systemctl disable --now "$s" 2>/dev/null || true
        sudo rm -f "/etc/systemd/system/$s.service"
    done
    sudo systemctl daemon-reload
    echo "Servicios quitados."
    exit 0
fi

if [ ! -x "$PYTHON" ]; then
    echo "No existe $PYTHON. Crea el entorno primero:"
    echo "  python3 -m venv venv && venv/bin/pip install -r dependencias.txt"
    exit 1
fi

# Escribe un archivo .service. $1 = nombre, $2 = descripcion, $3 = modulo y argumentos
crear_servicio() {
    sudo tee "/etc/systemd/system/$1.service" > /dev/null <<EOF
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
User=$USUARIO
WorkingDirectory=$RAIZ
ExecStart=$PYTHON -m $3
# Si el proceso termina por cualquier razon, volver a lanzarlo en 1 s
Restart=always
RestartSec=1
# Que los print salgan de una vez en journalctl
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
}

crear_servicio srb-persistencia "SRB - Servicio de Persistencia (SQLite)" "capa_persistencia.main"
crear_servicio srb-cancelacion  "SRB - Servicio de Cancelaciones"         "capa_negocios.main cancelacion"

sudo systemctl daemon-reload
# enable: arrancar tambien cuando se prenda la maquina; --now: arrancar ya
sudo systemctl enable --now $SERVICIOS
systemctl --no-pager status $SERVICIOS | grep -E "●|Active:"
echo
echo "Listo. Para ver los logs: journalctl -u srb-persistencia -f"
