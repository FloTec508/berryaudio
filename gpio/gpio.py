import logging
import asyncio
import serial
import lgpio

from core.actor import Actor
from core.types import Command, EncoderMode

logger = logging.getLogger(__name__)

INTERRUPT_PIN = 24
SOURCE_LOOKUP = {
    "bluetooth": 4,
    "linein": 5,
    "radio": 2,
    "spotify": 3,
    "storage": 1
}

class GpioExtension(Actor):
    def __init__(self, name, core, db, config):
        super().__init__()
        self._name = name
        self._core = core
        self._db = db
        self._config = config
        self._encoderCount = 0
        self._lastEncoderCount = 0
        self._loop = asyncio.get_running_loop()
        self._encoder_mode = EncoderMode.DIRECTION
        self._source = None
        self._lastBtnId = None
        
        # connect to IO expander
        self._serial = serial.Serial('/dev/ttyACM0', 115200, timeout=1)
        self.gpio_handle = lgpio.gpiochip_open(0)
        self.interrupt_pin = INTERRUPT_PIN

        # init GPIO for interrupt
        lgpio.gpio_claim_input(
            self.gpio_handle, self.interrupt_pin, lgpio.SET_PULL_DOWN
        )

        # register alert
        lgpio.gpio_claim_alert(
            self.gpio_handle,
            self.interrupt_pin,
            lgpio.FALLING_EDGE,  
        )

        # register callback
        # Handler running in seperate thread
        self.callback_id = lgpio.callback(
            self.gpio_handle,
            self.interrupt_pin,
            lgpio.FALLING_EDGE, 
            lambda *args: self._interruptHandler(),
        )

        logger.debug(f"GPIO {self.interrupt_pin} für RISING-Edge Interrupts konfiguriert.")
    
    def _interruptHandler(self):
        """Wird bei einer RISING_EDGE aufgerufen"""
        try:
            # while interrupt is still high, continue reading to prevent deadlock
            while lgpio.gpio_read(self.gpio_handle, self.interrupt_pin) == 0:
                logger.debug("Pin ist noch HIGH -> Hole weitere Daten vom STM32...")
                
                # 1. send Request
                self._serial.write(b"REQ\n")
                self._serial.flush()

                # 2. read answer
                line = self._serial.readline()
                if not line:
                    logger.warning("Timeout beim Lesen vom STM32, obwohl der Pin HIGH ist!")
                    break  # Verhindert eine Endlosschleife, falls der STM32 abstürzt
                
                text = line.decode('utf-8').strip()
                logger.debug(f"Recieved serial data: {text}")

                # 3. parse and process data
                commands = text.split(",")
                for command in commands:
                    parts = command.split(":")
                    if len(parts) != 2:
                        continue
                    asyncio.run_coroutine_threadsafe(self._handleCommand(parts[0], parts[1]), self._loop)
                    
            logger.debug("Pin is LOW. Buffer empty.")

        except Exception as e:
            logger.error(f"Error in Serial-I/O: {e}")

    async def _handleCommand(self, commandWord, data):
        if not commandWord:
            return
        
        if commandWord == "NAVENC":
            try:
                self._encoderCount = int(data)
            except ValueError:
                return

            while self._encoderCount != self._lastEncoderCount:
                i = 1 if self._encoderCount > self._lastEncoderCount else -1
                direction = "CW" if i == 1 else "CCW"
                self.on_encoder(direction)
                self._lastEncoderCount += i
        elif commandWord == "ACTVBTN":
            try:
                source = self._lookup_button_by_uri(int(data))
                await self._core.request("source.set", uri = source)
            except ValueError:
                return

    async def on_start(self):
        logger.info("Started")

    async def on_stop(self):
        if self.callback_id is not None:
            lgpio.callback_cancel(self.callback_id)
        if self._serial and self._serial.is_open:
            self._serial.close()
        if self.gpio_handle is not None:
            lgpio.gpiochip_close(self.gpio_handle)
        logger.info("Stopped")

    def on_set_encoder_mode(self, mode=EncoderMode.VOLUME):
        logger.debug(f"Encoder mode is '{mode}'")
        self._encoder_mode = mode
    
    async def on_event(self, message):
        if message and "event" in message:
            event = message["event"]
            logger.info(f"received Event: {event}")
            if event == "source_changed":
                self._source = message.get("source")
                if self._source and hasattr(self._source, "uri") and self._source.uri is not None:
                    btn_id = self._lookup_button_by_uri(self._source.uri)
                    logger.debug(f"Source uri: {self._source.uri}, button: {btn_id}")

                    # 1. Schutz vor Redundanz: Nur senden, wenn sich die Taste wirklich ändert
                    if btn_id is not None and btn_id != getattr(self, "_last_btn_id", None):
                        self._last_btn_id = btn_id
                     
                        
                     # 2. Blockierendes Serial-Write elegant in einen Thread auslagern
                        await asyncio.to_thread(self._send_to_expander, f"ACTVBTN:{btn_id}")
                    elif btn_id is None:
                        logger.warning(f"No button found for source {self._source}")


    def _lookup_button_by_uri(self, uri):
        """Compare URI with Lookup-Table"""
        if isinstance(uri, int):
            for source, btn_id in SOURCE_LOOKUP.items():
                if uri == btn_id:
                    return source
        else:
            for prefix, btn_id in SOURCE_LOOKUP.items():
                if uri.startswith(prefix):
                    return btn_id
        return None

    def _send_to_expander(self, command):
        try:
            payload = f"UPD,{command}\n".encode('utf-8')
            self._serial.write(payload)
            logger.debug(f"sent to expander: {payload}")
        except Exception as e:
            logger.error(f"error while sending command to expander: {e}")

    def on_encoder(self, direction):
        if self._encoder_mode == EncoderMode.VOLUME:
            _action = Command.VOLUME_UP if direction == "CW" else Command.VOLUME_DOWN
        elif self._encoder_mode == EncoderMode.DIRECTION:
            _action = Command.DOWN if direction == "CW" else Command.UP
        
        asyncio.run_coroutine_threadsafe(self._press_event(_action), self._loop)

    async def _press_event(self, action):
        await self._core.request("display.peppy_wakeup")
        await self._core.send(
            target=["web", "display", "command"], event="command", action=action
        )
