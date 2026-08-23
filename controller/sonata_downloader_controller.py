import wx

from globals.data_store import config, motor_de_interfaz
from globals.resources import lista_voces_piper
from servicios.sonata_manager import TAMANO_DESCARGA, SonataManager
from setup import reader
from TTS.sonata_handler import sonata_instalado
from ui.sonata_downloader import SonataDownloaderDialog
from utils.network import network_manager as network


class SonataDownloaderController:
    """Gemelo del descargador de Kokoro, pero para el motor sonata: el servidor
    de las voces Piper, que las instalaciones nuevas ya no traen en el build y
    se baja una sola vez desde la release fija «motores»."""

    def __init__(self, parent):
        self.manager = SonataManager()
        self.view = SonataDownloaderDialog(parent, TAMANO_DESCARGA // (1024 * 1024))
        self.descargando = False
        self.fase_instalacion = False
        self.cancelacion_pedida = False

        self.view.btn_descargar.Bind(wx.EVT_BUTTON, self.on_descargar)
        self.view.btn_cerrar.Bind(wx.EVT_BUTTON, self.on_cerrar)
        self.view.Bind(wx.EVT_CLOSE, self.on_close)

        if sonata_instalado():
            self.view.set_status(
                _("El motor de las voces Piper ya está instalado en este equipo.")
            )
            self.view.btn_descargar.Disable()
            self.view.btn_cerrar.SetFocus()
        else:
            self.view.set_status(_("Listo para descargar."))

    def on_descargar(self, event):
        self.descargando = True
        self.fase_instalacion = False
        self.cancelacion_pedida = False
        # Aquí y no dentro de instalar_motor: la corrutina no arranca hasta que
        # el bucle de red le hace sitio, y un Escape pulsado en ese hueco se
        # habría borrado al empezar ella.
        self.manager.cancelado = False
        self.view.btn_descargar.Disable()
        # El foco estaba en el botón recién deshabilitado: sin esto queda en el
        # limbo y un usuario de lector de pantalla ya no sabe dónde está.
        self.view.btn_cerrar.SetFocus()
        self.view.update_progress(0)
        self.view.set_status(_("Descargando el motor de voz..."))
        network.execute(self.manager.instalar_motor(self._progreso), self._al_terminar)

    def _progreso(self, avance):
        # Llega desde el hilo de red o desde el hilo de extracción.
        wx.CallAfter(self._aplicar_progreso, avance)

    def _aplicar_progreso(self, avance):
        if not self.view:
            return
        self.view.update_progress(avance)
        if avance >= 90 and not self.fase_instalacion:
            self.fase_instalacion = True
            self.view.set_status(_("Descarga completada. Instalando el paquete..."))

    def _al_terminar(self, resultado):
        self.descargando = False
        if not self.view:
            return
        if isinstance(resultado, Exception):
            exito, cancelado, detalle = False, False, str(resultado)
        else:
            exito = resultado.get("success", False)
            cancelado = resultado.get("cancelado", False)
            detalle = resultado.get("data", "")

        if exito:
            self.view.set_status(_("Instalación completada."))
            self._recargar_motor()
            wx.MessageBox(
                _(
                    "El motor de las voces Piper se ha instalado correctamente. Los mensajes ya pueden sonar con la voz elegida."
                ),
                _("Éxito"),
                parent=self.view,
            )
            self.view.EndModal(wx.ID_OK)
        elif cancelado:
            self.view.EndModal(wx.ID_CANCEL)
        else:
            self.view.update_progress(0)
            self.view.set_status(_("La instalación ha fallado."))
            self.view.btn_descargar.Enable()
            self.view.btn_descargar.SetFocus()
            wx.MessageBox(
                _("No se pudo instalar el motor de las voces Piper: %s") % detalle,
                _("Error"),
                parent=self.view,
            )

    def _recargar_motor(self):
        """Si Piper es el sistema activo, sustituye el respaldo SAPI del
        instante por el puente recién instalado y carga la voz elegida, para
        que funcione al momento, sin tener que reiniciar VeTube."""
        if motor_de_interfaz() != "piper":
            return
        # set_tts pasa por configurar_tts, que ahora sí encuentra el motor y
        # levanta el puente de verdad en lugar del respaldo.
        reader.set_tts("piper")
        if lista_voces_piper and lista_voces_piper[0] != _("No hay voces instaladas"):
            if not (0 <= config.get("voz", 0) < len(lista_voces_piper)):
                config["voz"] = 0
            from TTS.list_voices import obtener_ruta_voz

            model_path = obtener_ruta_voz(lista_voces_piper[config["voz"]])
            if model_path:
                reader._lector.load_model(model_path)
            # Import aquí y no en cabecera: app_utilitys importa setup, y este
            # módulo se importa desde los menús antes de que setup termine.
            from utils.app_utilitys import fijar_dispositivo_lector

            fijar_dispositivo_lector()

    def _pedir_cancelacion(self):
        """Primera petición: anuncia en voz alta y espera a que la tarea suelte
        el bloque en curso (normalmente una fracción de segundo). Segunda
        petición (red congelada que no suelta el control): salida de emergencia
        cerrando el diálogo; los callbacks tardíos ya quedan neutralizados por
        las guardas `if not self.view`. Devuelve True si hay que cerrar ya."""
        if self.cancelacion_pedida:
            return True
        self.cancelacion_pedida = True
        self.manager.cancelar()
        self.view.set_status(_("Cancelando la descarga..."))
        # Con _leer (voz secundaria) y no leer_aviso: la voz principal puede
        # ser justamente el respaldo del motor aún sin instalar.
        reader._leer.speak(_("Cancelando la descarga..."))
        return False

    def on_cerrar(self, event):
        if self.descargando and not self._pedir_cancelacion():
            return
        self.view.EndModal(wx.ID_CANCEL)

    def on_close(self, event):
        if self.descargando and not self._pedir_cancelacion():
            if event.CanVeto():
                event.Veto()
            return
        self.view.EndModal(wx.ID_CANCEL)

    def show(self):
        resultado = self.view.ShowModal()
        self.view.Destroy()
        return resultado
