import asyncio
import hashlib
import os
import shutil
import tarfile
import tempfile
from logging import getLogger

import httpx

from globals.paths import ENGINES_DIR

from .base_downloader import BaseDownloader

logger = getLogger(__name__)

# Motor sonata (servidor gRPC de las voces Piper) empaquetado en la release
# fija «motores» de VeTube: mismo esquema que el modelo Kokoro con la release
# tts-models de k2-fsa. Etiqueta fija y no «latest» para que la URL no cambie
# nunca, y en .tar.bz2 por simetría con el paquete Kokoro.
SONATA_MOTOR_URL = "https://github.com/metalalchemist/VeTube/releases/download/motores/sonata-x64.tar.bz2"
SONATA_SHA256_URL = SONATA_MOTOR_URL + ".sha256"
# El paquete se genera con `tar -C .../64 sonata`, así que sus miembros cuelgan
# de esta carpeta y es la que se muda entera a engines/ al final.
CARPETA_MOTOR = "sonata"
# Tamaños medidos de esta versión del paquete (la release fija no cambia), para
# la barra de progreso y el aviso de espacio en disco.
TAMANO_DESCARGA = 20539335
TAMANO_EXTRAIDO = 49331043
# Lo mínimo que debe existir tras extraer para dar la instalación por buena: el
# servidor y sus datos de fonemización (espeak-ng-data va DENTRO del paquete).
FICHERO_CLAVE = "sonata-grpc.exe"
CARPETA_CLAVE = "espeak-ng-data"


class SonataManager(BaseDownloader):
    """Descarga e instala el motor sonata en engines/, con progreso 0-100:
    0-90 descarga, 90-99 extracción, 100 instalado. Cancelable en todo momento.
    Mismo esquema que KokoroManager; además verifica la firma SHA-256 que
    acompaña al paquete en la release."""

    def __init__(self):
        super().__init__()
        self.cancelado = False

    def cancelar(self):
        """Puede llamarse desde cualquier hilo: la descarga y la extracción
        comprueban esta bandera y abortan limpiamente."""
        self.cancelado = True

    def destino_final(self):
        return str(ENGINES_DIR / CARPETA_MOTOR)

    def hay_espacio_suficiente(self, temp_dir):
        """Comprueba el espacio libre antes de empezar: el paquete y su
        extracción conviven en el temporal antes de mudarse a engines/."""
        try:
            libre_temp = shutil.disk_usage(temp_dir).free
            # El disco del destino real (engines/ vive junto al ejecutable),
            # no el directorio de trabajo: el cwd del proceso puede estar en
            # otro disco (accesos directos, relanzamientos del actualizador).
            libre_destino = shutil.disk_usage(str(ENGINES_DIR.parent)).free
        except Exception:
            return True  # Si no se puede medir, dejamos que lo intente
        return (
            libre_temp > TAMANO_DESCARGA + TAMANO_EXTRAIDO
            and libre_destino > TAMANO_EXTRAIDO
        )

    async def instalar_motor(self, progress_callback=None):
        """Descarga el paquete, lo verifica, lo extrae en un temporal y lo
        mueve a engines/. Devuelve {'success': bool, 'cancelado': bool,
        'data': detalle}.

        La bandera de cancelación NO se reinicia aquí: esta corrutina empieza a
        correr cuando el bucle de red le hace sitio, y quien cancele entre medias
        (el bucle también atiende los chats) se habría quedado sin efecto. La
        reinicia quien lanza la descarga, antes de encolarla."""
        temp_dir = tempfile.mkdtemp(prefix="vetube_sonata_")
        tar_path = os.path.join(temp_dir, CARPETA_MOTOR + ".tar.bz2")
        try:
            if not self.hay_espacio_suficiente(temp_dir):
                necesario_mb = (TAMANO_DESCARGA + TAMANO_EXTRAIDO) // (1024 * 1024)
                return {
                    "success": False,
                    "cancelado": False,
                    "data": _(
                        "No hay suficiente espacio libre en disco (se necesitan unos %d MB)."
                    )
                    % necesario_mb,
                }

            res = await self._descargar(SONATA_MOTOR_URL, tar_path, progress_callback)
            if not res["success"]:
                return res

            firma = await self._descargar_firma()
            # La firma pudo interrumpirse por cancelación: el contrato es
            # «cancelable en todo momento», también en esta etapa intermedia.
            if self.cancelado:
                return {"success": False, "cancelado": True, "data": ""}

            # La verificación y la extracción tardan: fuera del bucle de red,
            # que mientras tanto sigue atendiendo los chats.
            return await asyncio.to_thread(
                self._verificar_extraer_e_instalar,
                tar_path,
                temp_dir,
                firma,
                progress_callback,
            )
        except Exception as e:
            logger.error("Fallo al instalar el motor sonata", exc_info=True)
            return {"success": False, "cancelado": False, "data": str(e)}
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _descargar(self, url, dest_path, progress_callback):
        """La descarga en sí la hace BaseDownloader.download_file: aquí solo se
        le pasan las particularidades de este paquete y se traduce su resultado
        al formato con 'cancelado' que espera el resto del instalador."""
        res = await self.download_file(
            url,
            dest_path,
            progress_callback=progress_callback,
            cancel_check=lambda: self.cancelado,
            # El cliente central no tiene timeout: aquí ponemos uno de lectura
            # para que una conexión congelada termine en error visible en vez
            # de dejar la descarga (y al usuario) esperando para siempre.
            timeout=httpx.Timeout(60.0, connect=15.0),
            total_estimado=TAMANO_DESCARGA,
            tope_progreso=90,
        )
        if res.get("cancelado"):
            # Sin detalle: al cancelar no se le enseña ningún mensaje al usuario.
            return {"success": False, "cancelado": True, "data": ""}
        if res.get("status_code"):
            # Mensaje propio: el de la clase base lleva la URL cruda dentro y
            # este se le enseña al usuario en un cuadro de diálogo.
            return {
                "success": False,
                "cancelado": False,
                "data": _("el servidor de descargas respondió con el error HTTP %d.")
                % res["status_code"],
            }
        return {"success": res["success"], "cancelado": False, "data": res["data"]}

    async def _descargar_firma(self):
        """La firma SHA-256 publicada junto al paquete (formato «hash *nombre»).
        Devuelve el hash en minúsculas, o None si no se pudo obtener: la firma
        protege de una descarga corrupta, pero su ausencia momentánea no debe
        impedir instalar el motor (mismo criterio que el paquete Kokoro, que no
        tiene firma ninguna)."""
        from utils.network import network_manager as network

        try:
            # Como tarea vigilada y no como await directo: la bandera de
            # cancelación debe seguir mandando también aquí («cancelable en
            # todo momento»); un await directo la ignoraría hasta 30 segundos.
            tarea = asyncio.ensure_future(
                network.client.get(
                    SONATA_SHA256_URL,
                    follow_redirects=True,
                    timeout=httpx.Timeout(30.0, connect=15.0),
                )
            )
            while not tarea.done():
                if self.cancelado:
                    tarea.cancel()
                    return None
                await asyncio.sleep(0.2)
            respuesta = tarea.result()
            if respuesta.status_code != 200:
                logger.warning(
                    "HTTP %s al descargar la firma del motor sonata; se instala sin verificar",
                    respuesta.status_code,
                )
                return None
            return respuesta.text.split()[0].strip().lower()
        except Exception:
            logger.warning(
                "No se pudo descargar la firma del motor sonata; se instala sin verificar",
                exc_info=True,
            )
            return None

    def _verificar_extraer_e_instalar(self, tar_path, temp_dir, firma, progress_callback):
        """Corre en un hilo aparte. Verifica la firma, extrae en el temporal,
        comprueba y mueve la carpeta completa a engines/ (así nunca queda una
        instalación a medias)."""
        if firma:
            sha = hashlib.sha256()
            with open(tar_path, "rb") as f:
                while True:
                    if self.cancelado:
                        return {"success": False, "cancelado": True, "data": ""}
                    bloque = f.read(1024 * 1024)
                    if not bloque:
                        break
                    sha.update(bloque)
            if sha.hexdigest().lower() != firma:
                return {
                    "success": False,
                    "cancelado": False,
                    "data": _(
                        "el paquete descargado no supera la comprobación de integridad. Inténtalo de nuevo."
                    ),
                }

        dir_extraccion = os.path.join(temp_dir, "extraido")
        extraido = 0
        ultimo_avance = -1
        # Iteración en streaming: una sola pasada de descompresión, con los
        # ficheros copiados por bloques para que la barra avance también dentro
        # del ejecutable de 26 MB y la cancelación siga respondiendo (mismo
        # esquema, y mismas razones, que el instalador de Kokoro).
        with tarfile.open(tar_path, "r:bz2") as tar:
            for miembro in tar:
                if self.cancelado:
                    return {"success": False, "cancelado": True, "data": ""}
                if not self._miembro_seguro(miembro):
                    logger.warning(
                        "Miembro sospechoso ignorado en el paquete sonata: %s",
                        miembro.name,
                    )
                    continue
                ruta_miembro = os.path.join(
                    dir_extraccion, *miembro.name.replace("\\", "/").split("/")
                )
                if miembro.isdir():
                    os.makedirs(ruta_miembro, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(ruta_miembro), exist_ok=True)
                fuente = tar.extractfile(miembro)
                if fuente is None:
                    continue
                with fuente, open(ruta_miembro, "wb") as destino_f:
                    while True:
                        if self.cancelado:
                            return {"success": False, "cancelado": True, "data": ""}
                        bloque = fuente.read(1024 * 1024)
                        if not bloque:
                            break
                        destino_f.write(bloque)
                        extraido += len(bloque)
                        avance = 90 + min(9, int(extraido / TAMANO_EXTRAIDO * 10))
                        if progress_callback and avance != ultimo_avance:
                            ultimo_avance = avance
                            progress_callback(avance)

        origen = os.path.join(dir_extraccion, CARPETA_MOTOR)
        if not os.path.isfile(os.path.join(origen, FICHERO_CLAVE)):
            return {
                "success": False,
                "cancelado": False,
                "data": _("El paquete descargado está incompleto (falta %s).")
                % FICHERO_CLAVE,
            }
        if not os.path.isdir(os.path.join(origen, CARPETA_CLAVE)):
            return {
                "success": False,
                "cancelado": False,
                "data": _("El paquete descargado está incompleto (falta %s).")
                % CARPETA_CLAVE,
            }

        destino = self.destino_final()
        if os.path.isdir(destino):
            shutil.rmtree(destino)
        self.ensure_dir(str(ENGINES_DIR))
        shutil.move(origen, destino)
        if progress_callback:
            progress_callback(100)
        return {"success": True, "cancelado": False, "data": destino}

    def _miembro_seguro(self, miembro):
        """Solo ficheros y carpetas con rutas relativas sanas (sin .., sin
        absolutas, sin unidad): defensa si el paquete llegara manipulado."""
        if not (miembro.isfile() or miembro.isdir()):
            return False
        nombre = miembro.name.replace("\\", "/")
        # La barra inicial se comprueba aparte: desde Python 3.13, isabs() en
        # Windows ya no considera absoluto «/etc/passwd» (sin unidad).
        if (
            nombre.startswith("/")
            or os.path.isabs(nombre)
            or (len(nombre) > 1 and nombre[1] == ":")
        ):
            return False
        return ".." not in nombre.split("/")
