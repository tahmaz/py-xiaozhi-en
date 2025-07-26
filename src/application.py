import asyncio
import json
import platform
import sys
import threading
import time
from pathlib import Path

from src.constants.constants import (
    AbortReason,
    AudioConfig,
    DeviceState,
    EventType,
    ListeningMode,
)
from src.display import cli_display, gui_display
from src.protocols.mqtt_protocol import MqttProtocol
from src.protocols.websocket_protocol import WebsocketProtocol
from src.utils.common_utils import handle_verification_code
from src.utils.config_manager import ConfigManager
from src.utils.logging_config import get_logger

# Handle opus dynamic library before importing opuslib
from src.utils.opus_loader import setup_opus

setup_opus()

# Configure logging
logger = get_logger(__name__)

# Now import opuslib
try:
    import opuslib  # noqa: F401
except Exception as e:
    logger.critical("Failed to import opuslib: %s", e, exc_info=True)
    logger.critical("Please ensure the opus dynamic library is correctly installed or located in the correct path")
    sys.exit(1)


class Application:
    _instance = None

    @classmethod
    def get_instance(cls):
        """Get singleton instance."""
        if cls._instance is None:
            logger.debug("Creating Application singleton instance")
            cls._instance = Application()
        return cls._instance

    def __init__(self):
        """Initialize the application."""
        # Ensure singleton pattern
        if Application._instance is not None:
            logger.error("Attempting to create multiple instances of Application")
            raise Exception("Application is a singleton class, please use get_instance() to obtain the instance")
        Application._instance = self

        logger.debug("Initializing Application instance")
        # Get configuration manager instance
        self.config = ConfigManager.get_instance()
        self.config._initialize_mqtt_info()
        # State variables
        self.device_state = DeviceState.IDLE
        self.voice_detected = False
        self.keep_listening = False
        self.aborted = False
        self.current_text = ""
        self.current_emotion = "neutral"

        # Audio processing related
        self.audio_codec = None  # Will be initialized in _initialize_audio
        self._tts_lock = threading.Lock()
        # Since Display's playback state is only used by GUI and inconvenient for Music_player, this flag indicates TTS is speaking
        self.is_tts_playing = False

        # Event loop and threads
        self.loop = asyncio.new_event_loop()
        self.loop_thread = None
        self.running = False
        self.input_event_thread = None
        self.output_event_thread = None

        # Task queue and lock
        self.main_tasks = []
        self.mutex = threading.Lock()

        # Protocol instance
        self.protocol = None

        # Callback functions
        self.on_state_changed_callbacks = []

        # Initialize event objects
        self.events = {
            EventType.SCHEDULE_EVENT: threading.Event(),
            EventType.AUDIO_INPUT_READY_EVENT: threading.Event(),
            EventType.AUDIO_OUTPUT_READY_EVENT: threading.Event(),
        }

        # Create display interface
        self.display = None

        # Add wake word detector
        self.wake_word_detector = None
        logger.debug("Application instance initialization completed")

    def run(self, **kwargs):
        """Start the application."""
        logger.info("Starting application with parameters: %s", kwargs)
        mode = kwargs.get("mode", "gui")
        protocol = kwargs.get("protocol", "websocket")

        # Start main loop thread
        logger.debug("Starting main loop thread")
        main_loop_thread = threading.Thread(target=self._main_loop)
        main_loop_thread.daemon = True
        main_loop_thread.start()

        # Initialize communication protocol
        logger.debug("Setting protocol type: %s", protocol)
        self.set_protocol_type(protocol)

        # Create and start event loop thread
        logger.debug("Starting event loop thread")
        self.loop_thread = threading.Thread(target=self._run_event_loop)
        self.loop_thread.daemon = True
        self.loop_thread.start()

        # Wait for the event loop to be ready
        time.sleep(0.1)

        # Initialize application (remove automatic connection)
        logger.debug("Initializing application components")
        asyncio.run_coroutine_threadsafe(self._initialize_without_connect(), self.loop)

        # Initialize IoT devices
        self._initialize_iot_devices()

        logger.debug("Setting display type: %s", mode)
        self.set_display_type(mode)
        # Start GUI
        logger.debug("Starting display interface")
        self.display.start()

    def _run_event_loop(self):
        """Thread function to run the event loop."""
        logger.debug("Setting up and starting event loop")
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def set_is_tts_playing(self, value: bool):
        with self._tts_lock:
            self.is_tts_playing = value

    def get_is_tts_playing(self) -> bool:
        with self._tts_lock:
            return self.is_tts_playing

    async def _initialize_without_connect(self):
        """Initialize application components (without establishing connection)."""
        logger.info("Initializing application components...")

        # Set device state to idle
        logger.debug("Setting initial device state to IDLE")
        self.schedule(lambda: self.set_device_state(DeviceState.IDLE))

        # Initialize audio codec
        logger.debug("Initializing audio codec")
        self._initialize_audio()

        # Initialize and start wake word detection
        self._initialize_wake_word_detector()

        # Set network protocol callbacks (MQTT and WebSocket)
        logger.debug("Setting protocol callback functions")
        self.protocol.on_network_error = self._on_network_error
        self.protocol.on_incoming_audio = self._on_incoming_audio
        self.protocol.on_incoming_json = self._on_incoming_json
        self.protocol.on_audio_channel_opened = self._on_audio_channel_opened
        self.protocol.on_audio_channel_closed = self._on_audio_channel_closed

        logger.info("Application components initialization completed")

    def _initialize_audio(self):
        """Initialize audio devices and codec."""
        try:
            logger.debug("Starting audio codec initialization")
            from src.audio_codecs.audio_codec import AudioCodec

            self.audio_codec = AudioCodec()
            logger.info("Audio codec initialized successfully")

            # Log volume control status
            has_volume_control = (
                hasattr(self.display, "volume_controller")
                and self.display.volume_controller
            )
            if has_volume_control:
                logger.info("System volume control is enabled")
            else:
                logger.info("System volume control is not enabled, using simulated volume control")

        except Exception as e:
            logger.error("Failed to initialize audio device: %s", e, exc_info=True)
            self.alert("Error", f"Failed to initialize audio device: {e}")

    def set_protocol_type(self, protocol_type: str):
        """Set protocol type."""
        logger.debug("Setting protocol type: %s", protocol_type)
        if protocol_type == "mqtt":
            self.protocol = MqttProtocol(self.loop)
            logger.debug("MQTT protocol instance created")
        else:  # websocket
            self.protocol = WebsocketProtocol()
            logger.debug("WebSocket protocol instance created")

    def set_display_type(self, mode: str):
        """Initialize display interface."""
        logger.debug("Setting display interface type: %s", mode)
        # Manage different display modes through the adapter concept
        if mode == "gui":
            self.display = gui_display.GuiDisplay()
            logger.debug("GUI display interface created")
            self.display.set_callbacks(
                press_callback=self.start_listening,
                release_callback=self.stop_listening,
                status_callback=self._get_status_text,
                text_callback=self._get_current_text,
                emotion_callback=self._get_current_emotion,
                mode_callback=self._on_mode_changed,
                auto_callback=self.toggle_chat_state,
                abort_callback=lambda: self.abort_speaking(
                    AbortReason.WAKE_WORD_DETECTED
                ),
                send_text_callback=self._send_text_tts,
            )
        else:
            self.display = cli_display.CliDisplay()
            logger.debug("CLI display interface created")
            self.display.set_callbacks(
                auto_callback=self.toggle_chat_state,
                abort_callback=lambda: self.abort_speaking(
                    AbortReason.WAKE_WORD_DETECTED
                ),
                status_callback=self._get_status_text,
                text_callback=self._get_current_text,
                emotion_callback=self._get_current_emotion,
                send_text_callback=self._send_text_tts,
            )
        logger.debug("Display interface callback functions set")

    def _main_loop(self):
        """Application main loop."""
        logger.info("Main loop started")
        self.running = True

        while self.running:
            # Wait for events
            for event_type, event in self.events.items():
                if event.is_set():
                    event.clear()
                    logger.debug("Processing event: %s", event_type)

                    if event_type == EventType.AUDIO_INPUT_READY_EVENT:
                        self._handle_input_audio()
                    elif event_type == EventType.AUDIO_OUTPUT_READY_EVENT:
                        self._handle_output_audio()
                    elif event_type == EventType.SCHEDULE_EVENT:
                        self._process_scheduled_tasks()

            # Short sleep to avoid high CPU usage
            time.sleep(0.01)

    def _process_scheduled_tasks(self):
        """Process scheduled tasks."""
        with self.mutex:
            tasks = self.main_tasks.copy()
            self.main_tasks.clear()

        logger.debug("Processing %d scheduled tasks", len(tasks))
        for task in tasks:
            try:
                task()
            except Exception as e:
                logger.error("Error executing scheduled task: %s", e, exc_info=True)

    def schedule(self, callback):
        """Schedule a task to the main loop."""
        with self.mutex:
            self.main_tasks.append(callback)
        self.events[EventType.SCHEDULE_EVENT].set()

    def _handle_input_audio(self):
        """Handle audio input."""
        if self.device_state != DeviceState.LISTENING:
            return

        # Read and send audio data
        encoded_data = self.audio_codec.read_audio()
        if encoded_data and self.protocol and self.protocol.is_audio_channel_opened():
            asyncio.run_coroutine_threadsafe(
                self.protocol.send_audio(encoded_data), self.loop
            )

    async def _send_text_tts(self, text):
        """Send text via wake word."""
        if not self.protocol.is_audio_channel_opened():
            await self.protocol.open_audio_channel()

        await self.protocol.send_wake_word_detected(text)

    def _handle_output_audio(self):
        """Handle audio output."""
        if self.device_state != DeviceState.SPEAKING:
            return
        self.set_is_tts_playing(True)  # Start playback
        self.audio_codec.play_audio()

    def _on_network_error(self, error_message=None):
        """Network error callback."""
        if error_message:
            logger.error(error_message)

        self.keep_listening = False
        self.schedule(lambda: self.set_device_state(DeviceState.IDLE))
        # Resume wake word detection
        if self.wake_word_detector and self.wake_word_detector.paused:
            self.wake_word_detector.resume()

        if self.device_state != DeviceState.CONNECTING:
            logger.info("Connection disconnection detected")
            self.schedule(lambda: self.set_device_state(DeviceState.IDLE))

            # Close existing connection without closing audio stream
            if self.protocol:
                asyncio.run_coroutine_threadsafe(
                    self.protocol.close_audio_channel(), self.loop
                )

    def _on_incoming_audio(self, data):
        """Receive audio data callback."""
        if self.device_state == DeviceState.SPEAKING:
            self.audio_codec.write_audio(data)
            self.events[EventType.AUDIO_OUTPUT_READY_EVENT].set()

    def _on_incoming_json(self, json_data):
        """Receive JSON data callback."""
        try:
            if not json_data:
                return

            # Parse JSON data
            if isinstance(json_data, str):
                data = json.loads(json_data)
            else:
                data = json_data
            # Handle different message types
            msg_type = data.get("type", "")
            if msg_type == "tts":
                self._handle_tts_message(data)
            elif msg_type == "stt":
                self._handle_stt_message(data)
            elif msg_type == "llm":
                self._handle_llm_message(data)
            elif msg_type == "iot":
                self._handle_iot_message(data)
            else:
                logger.warning(f"Received unknown message type: {msg_type}")
        except Exception as e:
            logger.error(f"Error processing JSON message: {e}")

    def _handle_tts_message(self, data):
        """Handle TTS message."""
        state = data.get("state", "")
        if state == "start":
            self.schedule(lambda: self._handle_tts_start())
        elif state == "stop":
            self.schedule(lambda: self._handle_tts_stop())
        elif state == "sentence_start":
            text = data.get("text", "")
            if text:
                logger.info(f"<< {text}")
                self.schedule(lambda: self.set_chat_message("assistant", text))

                # Check for verification code information
                import re

                match = re.search(r"((?:\d\s*){6,})", text)
                if match:
                    self.schedule(lambda: handle_verification_code(text))

    def _handle_tts_start(self):
        """Handle TTS start event."""
        self.aborted = False
        self.set_is_tts_playing(True)  # Start playback
        # Clear any existing old audio data
        self.audio_codec.clear_audio_queue()

        if (
            self.device_state == DeviceState.IDLE
            or self.device_state == DeviceState.LISTENING
        ):
            self.schedule(lambda: self.set_device_state(DeviceState.SPEAKING))

        # Commented out code to resume VAD detector
        # if hasattr(self, 'vad_detector') and self.vad_detector:
        #     self.vad_detector.resume()

    def _handle_tts_stop(self):
        """Handle TTS stop event."""
        if self.device_state == DeviceState.SPEAKING:
            # Give audio playback a buffer time to ensure all audio is played
            def delayed_state_change():
                # Wait until the audio queue is empty
                # Increase wait attempts to ensure audio is fully played
                max_wait_attempts = 30  # Increase number of wait attempts
                wait_interval = 0.1  # Time interval for each wait
                attempts = 0

                # Wait until queue is empty or max attempts exceeded
                while (
                    not self.audio_codec.audio_decode_queue.empty()
                    and attempts < max_wait_attempts
                ):
                    time.sleep(wait_interval)
                    attempts += 1

                # Ensure all data is played out
                # Add extra wait time to ensure final data is processed
                if self.get_is_tts_playing():
                    time.sleep(0.5)

                # Set TTS playback state to False
                self.set_is_tts_playing(False)

                # State transition
                if self.keep_listening:
                    asyncio.run_coroutine_threadsafe(
                        self.protocol.send_start_listening(ListeningMode.AUTO_STOP),
                        self.loop,
                    )
                    self.schedule(lambda: self.set_device_state(DeviceState.LISTENING))
                else:
                    self.schedule(lambda: self.set_device_state(DeviceState.IDLE))

            # --- Force reinitialize input stream ---
            if platform.system() == "Linux":
                try:
                    if self.audio_codec:
                        self.audio_codec._reinitialize_stream(
                            is_input=True
                        )  # Call reinitialize
                    else:
                        logger.warning(
                            "Cannot force reinitialization, audio_codec is None."
                        )
                except Exception as force_reinit_e:
                    logger.error(
                        f"Forced reinitialization failed: {force_reinit_e}",
                        exc_info=True,
                    )
                    self.schedule(lambda: self.set_device_state(DeviceState.IDLE))
                    if self.wake_word_detector and self.wake_word_detector.paused:
                        self.wake_word_detector.resume()
                    return
            # --- End force reinitialization ---

            # Schedule delayed execution
            # threading.Thread(target=delayed_state_change, daemon=True).start()
            self.schedule(delayed_state_change)

    def _handle_stt_message(self, data):
        """Handle STT message."""
        text = data.get("text", "")
        if text:
            logger.info(f">> {text}")
            self.schedule(lambda: self.set_chat_message("user", text))

    def _handle_llm_message(self, data):
        """Handle LLM message."""
        emotion = data.get("emotion", "")
        if emotion:
            self.schedule(lambda: self.set_emotion(emotion))

    async def _on_audio_channel_opened(self):
        """Audio channel opened callback."""
        logger.info("Audio channel opened")
        self.schedule(lambda: self._start_audio_streams())

        # Send IoT device descriptors
        from src.iot.thing_manager import ThingManager

        thing_manager = ThingManager.get_instance()
        asyncio.run_coroutine_threadsafe(
            self.protocol.send_iot_descriptors(thing_manager.get_descriptors_json()),
            self.loop,
        )
        self._update_iot_states(False)

    def _start_audio_streams(self):
        """Start audio streams."""
        try:
            # No longer close and reopen streams, just ensure they are active
            if (
                self.audio_codec.input_stream
                and not self.audio_codec.input_stream.is_active()
            ):
                try:
                    self.audio_codec.input_stream.start_stream()
                except Exception as e:
                    logger.warning(f"Error starting input stream: {e}")
                    # Reinitialize only on error
                    self.audio_codec._reinitialize_stream(is_input=True)

            if (
                self.audio_codec.output_stream
                and not self.audio_codec.output_stream.is_active()
            ):
                try:
                    self.audio_codec.output_stream.start_stream()
                except Exception as e:
                    logger.warning(f"Error starting output stream: {e}")
                    # Reinitialize only on error
                    self.audio_codec._reinitialize_stream(is_input=False)

            # Set event triggers
            if (
                self.input_event_thread is None
                or not self.input_event_thread.is_alive()
            ):
                self.input_event_thread = threading.Thread(
                    target=self._audio_input_event_trigger, daemon=True
                )
                self.input_event_thread.start()
                logger.info("Input event trigger thread started")

            # Check output event thread
            if (
                self.output_event_thread is None
                or not self.output_event_thread.is_alive()
            ):
                self.output_event_thread = threading.Thread(
                    target=self._audio_output_event_trigger, daemon=True
                )
                self.output_event_thread.start()
                logger.info("Output event trigger thread started")

            logger.info("Audio streams started")
        except Exception as e:
            logger.error(f"Failed to start audio streams: {e}")

    def _audio_input_event_trigger(self):
        """Audio input event trigger."""
        while self.running:
            try:
                # Trigger input event only in active listening state
                if (
                    self.device_state == DeviceState.LISTENING
                    and self.audio_codec.input_stream
                ):
                    self.events[EventType.AUDIO_INPUT_READY_EVENT].set()
            except OSError as e:
                logger.error(f"Audio input stream error: {e}")
                # Do not exit loop, continue trying
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"Audio input event trigger error: {e}")
                time.sleep(0.5)

            # Ensure high enough trigger frequency even with large frame lengths
            # Use 20ms as max trigger interval to ensure sufficient sampling rate even with 60ms frame length
            sleep_time = min(20, AudioConfig.FRAME_DURATION) / 1000
            time.sleep(sleep_time)  # Trigger based on frame duration, ensuring minimum frequency

    def _audio_output_event_trigger(self):
        """Audio output event trigger."""
        while self.running:
            try:
                # Ensure output stream is active
                if (
                    self.device_state == DeviceState.SPEAKING
                    and self.audio_codec
                    and self.audio_codec.output_stream
                ):
                    # If output stream is not active, try to reactivate
                    if not self.audio_codec.output_stream.is_active():
                        try:
                            self.audio_codec.output_stream.start_stream()
                        except Exception as e:
                            logger.warning(f"Failed to start output stream, attempting reinitialization: {e}")
                            self.audio_codec._reinitialize_stream(is_input=False)

                    # Trigger event only when there is data in the queue
                    if not self.audio_codec.audio_decode_queue.empty():
                        self.events[EventType.AUDIO_OUTPUT_READY_EVENT].set()
            except Exception as e:
                logger.error(f"Audio output event trigger error: {e}")

            time.sleep(0.02)  # Slightly extend check interval

    async def _on_audio_channel_closed(self):
        """Audio channel closed callback."""
        logger.info("Audio channel closed")
        # Set to idle state without closing audio streams
        self.schedule(lambda: self.set_device_state(DeviceState.IDLE))
        self.keep_listening = False

        # Ensure wake word detection works normally
        if self.wake_word_detector:
            if not self.wake_word_detector.is_running():
                logger.info("Starting wake word detection in idle state")
                # Require AudioCodec instance
                if hasattr(self, "audio_codec") and self.audio_codec:
                    success = self.wake_word_detector.start(self.audio_codec)
                    if not success:
                        logger.error("Wake word detector failed to start, disabling wake word functionality")
                        self.config.update_config(
                            "WAKE_WORD_OPTIONS.USE_WAKE_WORD", False
                        )
                        self.wake_word_detector = None
                else:
                    logger.error("Audio codec unavailable, unable to start wake word detector")
                    self.config.update_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD", False)
                    self.wake_word_detector = None
            elif self.wake_word_detector.paused:
                logger.info("Resuming wake word detection in idle state")
                self.wake_word_detector.resume()

    def set_device_state(self, state):
        """Set device state."""
        if self.device_state == state:
            return

        self.device_state = state

        # Perform actions based on state
        if state == DeviceState.IDLE:
            self.display.update_status("Idle")
            # self.display.update_emotion("😶")
            self.set_emotion("neutral")
            # Resume wake word detection (with safety checks)
            if (
                self.wake_word_detector
                and hasattr(self.wake_word_detector, "paused")
                and self.wake_word_detector.paused
            ):
                self.wake_word_detector.resume()
                logger.info("Wake word detection resumed")
            # Resume audio input stream
            if self.audio_codec and self.audio_codec.is_input_paused():
                self.audio_codec.resume_input()
        elif state == DeviceState.CONNECTING:
            self.display.update_status("Connecting...")
        elif state == DeviceState.LISTENING:
            self.display.update_status("Listening...")
            self.set_emotion("neutral")
            self._update_iot_states(True)
            # Pause wake word detection (with safety checks)
            if (
                self.wake_word_detector
                and hasattr(self.wake_word_detector, "is_running")
                and self.wake_word_detector.is_running()
            ):
                self.wake_word_detector.pause()
                logger.info("Wake word detection paused")
            # Ensure audio input stream is active
            if self.audio_codec:
                if self.audio_codec.is_input_paused():
                    self.audio_codec.resume_input()
        elif state == DeviceState.SPEAKING:
            self.display.update_status("Speaking...")
            if (
                self.wake_word_detector
                and hasattr(self.wake_word_detector, "paused")
                and self.wake_word_detector.paused
            ):
                self.wake_word_detector.resume()

        # Notify state change
        for callback in self.on_state_changed_callbacks:
            try:
                callback(state)
            except Exception as e:
                logger.error(f"Error executing state change callback: {e}")

    def _get_status_text(self):
        """Get current status text."""
        states = {
            DeviceState.IDLE: "Idle",
            DeviceState.CONNECTING: "Connecting...",
            DeviceState.LISTENING: "Listening...",
            DeviceState.SPEAKING: "Speaking...",
        }
        return states.get(self.device_state, "Unknown")

    def _get_current_text(self):
        """Get current display text."""
        return self.current_text

    def _get_current_emotion(self):
        """Get current emotion."""
        # If emotion hasn't changed, return cached path
        if (
            hasattr(self, "_last_emotion")
            and self._last_emotion == self.current_emotion
        ):
            return self._last_emotion_path

        # Get base path
        if getattr(sys, "frozen", False):
            # Packaged environment
            if hasattr(sys, "_MEIPASS"):
                base_path = Path(sys._MEIPASS)
            else:
                base_path = Path(sys.executable).parent
        else:
            # Development environment
            base_path = Path(__file__).parent.parent

        emotion_dir = base_path / "assets" / "emojis"

        emotions = {
            "neutral": str(emotion_dir / "neutral.gif"),
            "happy": str(emotion_dir / "happy.gif"),
            "laughing": str(emotion_dir / "laughing.gif"),
            "funny": str(emotion_dir / "funny.gif"),
            "sad": str(emotion_dir / "sad.gif"),
            "angry": str(emotion_dir / "angry.gif"),
            "crying": str(emotion_dir / "crying.gif"),
            "loving": str(emotion_dir / "loving.gif"),
            "embarrassed": str(emotion_dir / "embarrassed.gif"),
            "surprised": str(emotion_dir / "surprised.gif"),
            "shocked": str(emotion_dir / "shocked.gif"),
            "thinking": str(emotion_dir / "thinking.gif"),
            "winking": str(emotion_dir / "winking.gif"),
            "cool": str(emotion_dir / "cool.gif"),
            "relaxed": str(emotion_dir / "relaxed.gif"),
            "delicious": str(emotion_dir / "delicious.gif"),
            "kissy": str(emotion_dir / "kissy.gif"),
            "confident": str(emotion_dir / "confident.gif"),
            "sleepy": str(emotion_dir / "sleepy.gif"),
            "silly": str(emotion_dir / "silly.gif"),
            "confused": str(emotion_dir / "confused.gif"),
        }

        # Save current emotion and corresponding path
        self._last_emotion = self.current_emotion
        self._last_emotion_path = emotions.get(
            self.current_emotion, str(emotion_dir / "neutral.gif")
        )

        logger.debug(f"Emotion path: {self._last_emotion_path}")
        return self._last_emotion_path

    def set_chat_message(self, role, message):
        """Set chat message."""
        self.current_text = message
        # Update display
        if self.display:
            self.display.update_text(message)

    def set_emotion(self, emotion):
        """Set emotion."""
        self.current_emotion = emotion
        # Update display
        if self.display:
            self.display.update_emotion(self._get_current_emotion())

    def start_listening(self):
        """Start listening."""
        self.schedule(self._start_listening_impl)

    def _start_listening_impl(self):
        """Implementation of start listening."""
        if not self.protocol:
            logger.error("Protocol not initialized")
            return

        self.keep_listening = False

        # Check if wake word detector exists
        if self.wake_word_detector:
            self.wake_word_detector.pause()

        if self.device_state == DeviceState.IDLE:
            self.schedule(
                lambda: self.set_device_state(DeviceState.CONNECTING)
            )  # Set device state to connecting
            # Try to open audio channel
            if not self.protocol.is_audio_channel_opened():
                try:
                    # Wait for async operation to complete
                    future = asyncio.run_coroutine_threadsafe(
                        self.protocol.open_audio_channel(), self.loop
                    )
                    # Wait for operation to complete and get result
                    success = future.result(timeout=10.0)  # Add timeout

                    if not success:
                        self.alert("Error", "Failed to open audio channel")  # Show error alert
                        self.schedule(lambda: self.set_device_state(DeviceState.IDLE))
                        return

                except Exception as e:
                    logger.error(f"Error opening audio channel: {e}")
                    self.alert("Error", f"Failed to open audio channel: {str(e)}")
                    self.schedule(lambda: self.set_device_state(DeviceState.IDLE))
                    return

            # --- Force reinitialize input stream ---
            try:
                if self.audio_codec:
                    self.audio_codec._reinitialize_stream(
                        is_input=True
                    )  # Call reinitialize
                else:
                    logger.warning(
                        "Cannot force reinitialization, audio_codec is None."
                    )
            except Exception as force_reinit_e:
                logger.error(
                    f"Forced reinitialization failed: {force_reinit_e}", exc_info=True
                )
                self.schedule(lambda: self.set_device_state(DeviceState.IDLE))
                if self.wake_word_detector and self.wake_word_detector.paused:
                    self.wake_word_detector.resume()
                return
            # --- End force reinitialization ---

            asyncio.run_coroutine_threadsafe(
                self.protocol.send_start_listening(ListeningMode.MANUAL), self.loop
            )
            self.schedule(lambda: self.set_device_state(DeviceState.LISTENING))
        elif self.device_state == DeviceState.SPEAKING:
            if not self.aborted:
                self.abort_speaking(AbortReason.WAKE_WORD_DETECTED)

    async def _open_audio_channel_and_start_manual_listening(self):
        """Open audio channel and start manual listening."""
        if not await self.protocol.open_audio_channel():
            self.schedule(lambda: self.set_device_state(DeviceState.IDLE))
            self.alert("Error", "Failed to open audio channel")
            return

        await self.protocol.send_start_listening(ListeningMode.MANUAL)
        self.schedule(lambda: self.set_device_state(DeviceState.LISTENING))

    def toggle_chat_state(self):
        """Toggle chat state."""
        # Check if wake word detector exists
        if self.wake_word_detector:
            self.wake_word_detector.pause()
        self.schedule(self._toggle_chat_state_impl)

    def _toggle_chat_state_impl(self):
        """Implementation of toggle chat state."""
        # Check if protocol is initialized
        if not self.protocol:
            logger.error("Protocol not initialized")
            return

        # If device is in idle state, try to connect and start listening
        if self.device_state == DeviceState.IDLE:
            self.schedule(
                lambda: self.set_device_state(DeviceState.CONNECTING)
            )  # Set device state to connecting

            # Use thread to handle connection operation to avoid blocking
            def connect_and_listen():
                # Try to open audio channel
                if not self.protocol.is_audio_channel_opened():
                    try:
                        # Wait for async operation to complete
                        future = asyncio.run_coroutine_threadsafe(
                            self.protocol.open_audio_channel(), self.loop
                        )
                        # Wait for operation to complete and get result, using shorter timeout
                        try:
                            success = future.result(timeout=5.0)
                        except asyncio.TimeoutError:
                            logger.error("Opening audio channel timed out")
                            self.schedule(
                                lambda: self.set_device_state(DeviceState.IDLE)
                            )
                            self.alert("Error", "Opening audio channel timed out")
                            return
                        except Exception as e:
                            logger.error(f"Unknown error opening audio channel: {e}")
                            self.schedule(
                                lambda: self.set_device_state(DeviceState.IDLE)
                            )
                            self.alert("Error", f"Failed to open audio channel: {str(e)}")
                            return

                        if not success:
                            self.alert("Error", "Failed to open audio channel")  # Show error alert
                            self.schedule(
                                lambda: self.set_device_state(DeviceState.IDLE)
                            )
                            return

                    except Exception as e:
                        logger.error(f"Error opening audio channel: {e}")
                        self.alert("Error", f"Failed to open audio channel: {str(e)}")
                        self.schedule(lambda: self.set_device_state(DeviceState.IDLE))
                        return

                self.keep_listening = True  # Start listening
                # Start auto-stop listening mode
                try:
                    asyncio.run_coroutine_threadsafe(
                        self.protocol.send_start_listening(ListeningMode.AUTO_STOP),
                        self.loop,
                    )
                    self.schedule(lambda: self.set_device_state(DeviceState.LISTENING))
                except Exception as e:
                    logger.error(f"Error starting listening: {e}")
                    self.set_device_state(DeviceState.IDLE)
                    self.alert("Error", f"Failed to start listening: {str(e)}")

            # Start connection thread
            threading.Thread(target=connect_and_listen, daemon=True).start()

        # If device is speaking, stop current speech
        elif self.device_state == DeviceState.SPEAKING:
            self.abort_speaking(AbortReason.NONE)  # Abort speech

        # If device is listening, close audio channel
        elif self.device_state == DeviceState.LISTENING:
            # Use thread to handle close operation to avoid blocking
            def close_audio_channel():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self.protocol.close_audio_channel(), self.loop
                    )
                    future.result(timeout=3.0)  # Use shorter timeout
                except Exception as e:
                    logger.error(f"Error closing audio channel: {e}")

            threading.Thread(target=close_audio_channel, daemon=True).start()
            # Set to idle state immediately, without waiting for close completion
            self.schedule(lambda: self.set_device_state(DeviceState.IDLE))

    def stop_listening(self):
        """Stop listening."""
        self.schedule(self._stop_listening_impl)

    def _stop_listening_impl(self):
        """Implementation of stop listening."""
        if self.device_state == DeviceState.LISTENING:
            asyncio.run_coroutine_threadsafe(
                self.protocol.send_stop_listening(), self.loop
            )
            self.set_device_state(DeviceState.IDLE)

    def abort_speaking(self, reason):
        """Abort speech output."""
        # If already aborted, ignore repeated abort requests
        if self.aborted:
            logger.debug(f"Already aborted, ignoring repeated abort request: {reason}")
            return

        logger.info(f"Aborting speech output, reason: {reason}")
        self.aborted = True

        # Set TTS playback state to False
        self.set_is_tts_playing(False)

        # Clear audio queue immediately
        if self.audio_codec:
            self.audio_codec.clear_audio_queue()

        # If aborted due to wake word, pause wake word detector to avoid Vosk assertion errors
        if reason == AbortReason.WAKE_WORD_DETECTED and self.wake_word_detector:
            if (
                hasattr(self.wake_word_detector, "is_running")
                and self.wake_word_detector.is_running()
            ):
                # Pause wake word detector
                self.wake_word_detector.pause()
                logger.debug("Temporarily pausing wake word detector to avoid concurrent processing")
                # Short wait to ensure wake word detector has paused processing
                time.sleep(0.1)

        # Use thread to handle state change and async operations to avoid blocking main thread
        def process_abort():
            # Send abort command first
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.protocol.send_abort_speaking(reason), self.loop
                )
                # Use short timeout to avoid long blocking
                future.result(timeout=1.0)
            except Exception as e:
                logger.error(f"Error sending abort command: {e}")

            # Then set state
            # self.set_device_state(DeviceState.IDLE)
            self.schedule(lambda: self.set_device_state(DeviceState.IDLE))
            # If aborted due to wake word and auto-listening is enabled, enter recording mode automatically
            if (
                reason == AbortReason.WAKE_WORD_DETECTED
                and self.keep_listening
                and self.protocol.is_audio_channel_opened()
            ):
                # Short delay to ensure abort command is processed
                time.sleep(0.1)  # Shorten delay time
                self.schedule(lambda: self.toggle_chat_state())

        # Start processing thread
        threading.Thread(target=process_abort, daemon=True).start()

    def alert(self, title, message):
        """Display warning message."""
        logger.warning(f"Warning: {title}, {message}")
        # Display warning on GUI
        if self.display:
            self.display.update_text(f"{title}: {message}")

    def on_state_changed(self, callback):
        """Register state change callback."""
        self.on_state_changed_callbacks.append(callback)

    def shutdown(self):
        """Shut down the application."""
        logger.info("Shutting down application...")
        self.running = False

        # Close audio codec
        if self.audio_codec:
            self.audio_codec.close()

        # Close protocol
        if self.protocol:
            asyncio.run_coroutine_threadsafe(
                self.protocol.close_audio_channel(), self.loop
            )

        # Stop event loop
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

        # Wait for event loop thread to end
        if self.loop_thread and self.loop_thread.is_alive():
            self.loop_thread.join(timeout=1.0)

        # Stop wake word detection
        if self.wake_word_detector:
            self.wake_word_detector.stop()

        # Stop VAD detector
        # if hasattr(self, 'vad_detector') and self.vad_detector:
        #     self.vad_detector.stop()

        logger.info("Application shut down")

    def _on_mode_changed(self, auto_mode):
        """Handle conversation mode change."""
        # Allow mode switching only in IDLE state
        if self.device_state != DeviceState.IDLE:
            self.alert("Prompt", "Conversation mode can only be switched in idle state")
            return False

        self.keep_listening = auto_mode
        logger.info(f"Conversation mode switched to: {'Auto' if auto_mode else 'Manual'}")
        return True

    def _initialize_wake_word_detector(self):
        """Initialize wake word detector."""
        # First check if wake word functionality is enabled in configuration
        if not self.config.get_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD", False):
            logger.info("Wake word functionality is disabled in configuration, skipping initialization")
            self.wake_word_detector = None
            return

        try:
            from src.audio_processing.wake_word_detect import WakeWordDetector

            # Create detector instance
            self.wake_word_detector = WakeWordDetector()

            # If wake word detector is disabled (internal failure), update configuration
            if not getattr(self.wake_word_detector, "enabled", True):
                logger.warning("Wake word detector is disabled (internal failure)")
                self.config.update_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD", False)
                self.wake_word_detector = None
                return

            # Register wake word detection callback and error handling
            self.wake_word_detector.on_detected(self._on_wake_word_detected)

            # Use lambda to capture self instead of defining a separate function
            self.wake_word_detector.on_error = lambda error: (
                self._handle_wake_word_error(error)
            )

            logger.info("Wake word detector initialized successfully")

            # Start wake word detector
            self._start_wake_word_detector()

        except Exception as e:
            logger.error(f"Failed to initialize wake word detector: {e}")
            import traceback

            logger.error(traceback.format_exc())

            # Disable wake word functionality but do not affect other program functions
            self.config.update_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD", False)
            logger.info("Wake word functionality disabled due to initialization failure, but program will continue running")
            self.wake_word_detector = None

    def _handle_wake_word_error(self, error):
        """Handle wake word detector error."""
        logger.error(f"Wake word detection error: {error}")
        # Try restarting detector
        if self.device_state == DeviceState.IDLE:
            self.schedule(lambda: self._restart_wake_word_detector())

    def _start_wake_word_detector(self):
        """Start wake word detector."""
        if not self.wake_word_detector:
            return

        # Require audio codec to be initialized
        if hasattr(self, "audio_codec") and self.audio_codec:
            logger.info("Starting wake word detector with audio codec")
            success = self.wake_word_detector.start(self.audio_codec)
            if not success:
                logger.error("Wake word detector failed to start, disabling wake word functionality")
                self.config.update_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD", False)
                self.wake_word_detector = None
        else:
            logger.error("Audio codec unavailable, unable to start wake word detector")
            self.config.update_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD", False)
            self.wake_word_detector = None

    def _on_wake_word_detected(self, wake_word, full_text):
        """Wake word detection callback."""
        logger.info(f"Wake word detected: {wake_word} (full text: {full_text})")
        self.schedule(lambda: self._handle_wake_word_detected(wake_word))

    def _handle_wake_word_detected(self, wake_word):
        """Handle wake word detection event."""
        if self.device_state == DeviceState.IDLE:
            # Pause wake word detection
            if self.wake_word_detector:
                self.wake_word_detector.pause()

            # Start connection and listening
            self.schedule(lambda: self.set_device_state(DeviceState.CONNECTING))
            # Try connecting and opening audio channel
            asyncio.run_coroutine_threadsafe(
                self._connect_and_start_listening(wake_word), self.loop
            )
        elif self.device_state == DeviceState.SPEAKING:
            self.abort_speaking(AbortReason.WAKE_WORD_DETECTED)

    async def _connect_and_start_listening(self, wake_word):
        """Connect to server and start listening."""
        # First try connecting to server
        if not await self.protocol.connect():
            logger.error("Failed to connect to server")
            self.alert("Error", "Failed to connect to server")
            self.schedule(lambda: self.set_device_state(DeviceState.IDLE))
            # Resume wake word detection
            if self.wake_word_detector:
                self.wake_word_detector.resume()
            return

        # Then try opening audio channel
        if not await self.protocol.open_audio_channel():
            logger.error("Failed to open audio channel")
            self.schedule(lambda: self.set_device_state(DeviceState.IDLE))
            self.alert("Error", "Failed to open audio channel")
            # Resume wake word detection
            if self.wake_word_detector:
                self.wake_word_detector.resume()
            return

        await self.protocol.send_wake_word_detected(wake_word)
        # Set to auto-listening mode
        self.keep_listening = True
        await self.protocol.send_start_listening(ListeningMode.AUTO_STOP)
        self.schedule(lambda: self.set_device_state(DeviceState.LISTENING))

    def _restart_wake_word_detector(self):
        """Restart wake word detector (only supports AudioCodec shared stream mode)."""
        logger.info("Attempting to restart wake word detector")
        try:
            # Stop existing detector
            if self.wake_word_detector:
                self.wake_word_detector.stop()
                time.sleep(0.5)  # Allow some time for resource release

            # Require audio codec
            if hasattr(self, "audio_codec") and self.audio_codec:
                success = self.wake_word_detector.start(self.audio_codec)
                if success:
                    logger.info("Wake word detector restarted successfully with audio codec")
                else:
                    logger.error("Wake word detector restart failed, disabling wake word functionality")
                    self.config.update_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD", False)
                    self.wake_word_detector = None
            else:
                logger.error("Audio codec unavailable, unable to restart wake word detector")
                self.config.update_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD", False)
                self.wake_word_detector = None
        except Exception as e:
            logger.error(f"Failed to restart wake word detector: {e}")
            self.config.update_config("WAKE_WORD_OPTIONS.USE_WAKE_WORD", False)
            self.wake_word_detector = None

    def _initialize_iot_devices(self):
        """Initialize IoT devices."""
        from src.iot.thing_manager import ThingManager
        from src.iot.things.CameraVL.Camera import Camera

        # Import new countdown timer device
        from src.iot.things.countdown_timer import CountdownTimer
        from src.iot.things.lamp import Lamp
        from src.iot.things.music_player import MusicPlayer
        from src.iot.things.speaker import Speaker

        # Get IoT device manager instance
        thing_manager = ThingManager.get_instance()

        # Add devices
        thing_manager.add_thing(Lamp())
        thing_manager.add_thing(Speaker())
        thing_manager.add_thing(MusicPlayer())
        # Default disabled for the following example
        thing_manager.add_thing(Camera())

        # Add countdown timer device
        thing_manager.add_thing(CountdownTimer())
        logger.info("Added countdown timer device for timed command execution")

        # Register only if Home Assistant is configured
        if self.config.get_config("HOME_ASSISTANT.TOKEN"):
            # Import Home Assistant device control classes
            from src.iot.things.ha_control import (
                HomeAssistantButton,
                HomeAssistantLight,
                HomeAssistantNumber,
                HomeAssistantSwitch,
            )

            # Add Home Assistant devices
            ha_devices = self.config.get_config("HOME_ASSISTANT.DEVICES", [])
            for device in ha_devices:
                entity_id = device.get("entity_id")
                friendly_name = device.get("friendly_name")
                if entity_id:
                    # Determine device type based on entity ID
                    if entity_id.startswith("light."):
                        # Light device
                        thing_manager.add_thing(
                            HomeAssistantLight(entity_id, friendly_name)
                        )
                        logger.info(
                            f"Added Home Assistant light device: {friendly_name or entity_id}"
                        )
                    elif entity_id.startswith("switch."):
                        # Switch device
                        thing_manager.add_thing(
                            HomeAssistantSwitch(entity_id, friendly_name)
                        )
                        logger.info(
                            f"Added Home Assistant switch device: {friendly_name or entity_id}"
                        )
                    elif entity_id.startswith("number."):
                        # Number device (e.g., volume control)
                        thing_manager.add_thing(
                            HomeAssistantNumber(entity_id, friendly_name)
                        )
                        logger.info(
                            f"Added Home Assistant number device: {friendly_name or entity_id}"
                        )
                    elif entity_id.startswith("button."):
                        # Button device
                        thing_manager.add_thing(
                            HomeAssistantButton(entity_id, friendly_name)
                        )
                        logger.info(
                            f"Added Home Assistant button device: {friendly_name or entity_id}"
                        )
                    else:
                        # Default to light device
                        thing_manager.add_thing(
                            HomeAssistantLight(entity_id, friendly_name)
                        )
                        logger.info(
                            f"Added Home Assistant device (default treated as light): {friendly_name or entity_id}"
                        )

        logger.info("IoT devices initialization completed")

    def _handle_iot_message(self, data):
        """Handle IoT message."""
        from src.iot.thing_manager import ThingManager

        thing_manager = ThingManager.get_instance()

        commands = data.get("commands", [])
        for command in commands:
            try:
                result = thing_manager.invoke(command)
                logger.info(f"IoT command execution result: {result}")
                # self.schedule(lambda: self._update_iot_states())
            except Exception as e:
                logger.error(f"Failed to execute IoT command: {e}")

    def _update_iot_states(self, delta=None):
        """Update IoT device states.

        Args:
            delta: Whether to send only changed parts
                   - None: Use original behavior, always send all states
                   - True: Send only changed parts
                   - False: Send all states and reset cache
        """
        from src.iot.thing_manager import ThingManager

        thing_manager = ThingManager.get_instance()

        # Handle backward compatibility
        if delta is None:
            # Maintain original behavior: get all states and send
            states_json = thing_manager.get_states_json_str()  # Call old method

            # Send state update
            asyncio.run_coroutine_threadsafe(
                self.protocol.send_iot_states(states_json), self.loop
            )
            logger.info("IoT device states updated")
            return

        # Use new method to get states
        changed, states_json = thing_manager.get_states_json(delta=delta)
        # delta=False always sends, delta=True sends only if changed
        if not delta or changed:
            asyncio.run_coroutine_threadsafe(
                self.protocol.send_iot_states(states_json), self.loop
            )
            if delta:
                logger.info("IoT device states updated (incremental)")
            else:
                logger.info("IoT device states updated (full)")
        else:
            logger.debug("No changes in IoT device states, skipping update")

    def _update_wake_word_detector_stream(self):
        """Update wake word detector's audio stream."""
        if (
            self.wake_word_detector
            and self.audio_codec
            and self.wake_word_detector.is_running()
        ):
            # Directly reference input stream from AudioCodec instance
            if (
                self.audio_codec.input_stream
                and self.audio_codec.input_stream.is_active()
            ):
                self.wake_word_detector.stream = self.audio_codec.input_stream
                self.wake_word_detector.external_stream = True
                logger.info("Updated wake word detector's audio stream reference")
