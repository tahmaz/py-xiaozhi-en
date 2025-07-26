import logging
import os
import platform
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from PyQt5.QtCore import (
    Q_ARG,
    QEvent,
    QMetaObject,
    QObject,
    QPropertyAnimation,
    Qt,
    QThread,
    QTimer,
    pyqtSlot,
)
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QMouseEvent,
    QMovie,
    QPainter,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QStyle,
    QStyleOptionSlider,
    QSystemTrayIcon,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from src.utils.config_manager import ConfigManager

# Handle pynput import based on operating system
try:
    if platform.system() == "Windows":
        from pynput import keyboard as pynput_keyboard
    elif os.environ.get("DISPLAY"):
        from pynput import keyboard as pynput_keyboard
    else:
        pynput_keyboard = None
except ImportError:
    pynput_keyboard = None

from abc import ABCMeta

from src.display.base_display import BaseDisplay


def restart_program():
    """Restart the current Python program, supporting packaged environments."""
    try:
        python = sys.executable
        print(f"Attempting to restart with command: {python} {sys.argv}")

        # Attempt to close Qt application, although execv will take over, this is more proper
        app = QApplication.instance()
        if app:
            app.quit()

        # Use different restart methods in packaged environments
        if getattr(sys, "frozen", False):
            # In packaged environment, use subprocess to start a new process
            import subprocess

            # Build complete command line
            if sys.platform.startswith("win"):
                # Windows: Use detached to create an independent process
                executable = os.path.abspath(sys.executable)
                subprocess.Popen(
                    [executable] + sys.argv[1:],
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                # Linux/Mac
                executable = os.path.abspath(sys.executable)
                subprocess.Popen([executable] + sys.argv[1:], start_new_session=True)

            # Exit current process
            sys.exit(0)
        else:
            # Non-packaged environment, use os.execv
            os.execv(python, [python] + sys.argv)
    except Exception as e:
        print(f"Failed to restart program: {e}")
        logging.getLogger("Display").error(f"Failed to restart program: {e}", exc_info=True)
        # If restart fails, choose to exit or notify user
        sys.exit(1)  # Or display an error message box


# Create compatible metaclass
class CombinedMeta(type(QObject), ABCMeta):
    pass


class GuiDisplay(BaseDisplay, QObject, metaclass=CombinedMeta):
    def __init__(self):
        # Important: Call super() to handle multiple inheritance
        super().__init__()
        QObject.__init__(self)  # Call QObject initialization

        # Initialize logger
        self.logger = logging.getLogger("Display")

        self.app = None
        self.root = None

        # Pre-initialized variables
        self.status_label = None
        self.emotion_label = None
        self.tts_text_label = None
        self.volume_scale = None
        self.manual_btn = None
        self.abort_btn = None
        self.auto_btn = None
        self.mode_btn = None
        self.mute = None
        self.stackedWidget = None
        self.nav_tab_bar = None

        # Add emotion animation object
        self.emotion_movie = None
        # Add variables related to emotion animation effects
        self.emotion_effect = None  # Emotion opacity effect
        self.emotion_animation = None  # Emotion animation object
        self.next_emotion_path = None  # Next emotion to display
        self.is_emotion_animating = False  # Whether emotion animation is in progress

        # Volume control related
        self.volume_label = None  # Volume percentage label
        self.volume_control_available = False  # Whether system volume control is available
        self.volume_controller_failed = False  # Flag for volume control failure

        self.is_listening = False  # Whether listening is active

        # Settings page widgets
        self.wakeWordEnableSwitch = None
        self.wakeWordsLineEdit = None
        self.saveSettingsButton = None
        # New network and device ID widget references
        self.deviceIdLineEdit = None
        self.wsProtocolComboBox = None
        self.wsAddressLineEdit = None
        self.wsTokenLineEdit = None
        # New OTA address widget references
        self.otaProtocolComboBox = None
        self.otaAddressLineEdit = None
        # Home Assistant widget references
        self.haProtocolComboBox = None
        self.ha_server = None
        self.ha_port = None
        self.ha_key = None
        self.Add_ha_devices = None

        self.is_muted = False
        self.pre_mute_volume = self.current_volume

        # Conversation mode flag
        self.auto_mode = False

        # Callback functions
        self.button_press_callback = None
        self.button_release_callback = None
        self.status_update_callback = None
        self.text_update_callback = None
        self.emotion_update_callback = None
        self.mode_callback = None
        self.auto_callback = None
        self.abort_callback = None
        self.send_text_callback = None

        # Update queue
        self.update_queue = queue.Queue()

        # Running flag
        self._running = True

        # Keyboard listener
        self.keyboard_listener = None
        # Add set of pressed keys
        self.pressed_keys = set()

        # Swipe gesture related
        self.last_mouse_pos = None

        # Save timer references to avoid destruction
        self.update_timer = None
        self.volume_update_timer = None

        # Animation related
        self.current_effect = None
        self.current_animation = None
        self.animation = None
        self.fade_widget = None
        self.animated_widget = None

        # Check if system volume control is available
        self.volume_control_available = (
            hasattr(self, "volume_controller") and self.volume_controller is not None
        )

        # Attempt to get system volume once to check if volume control is working
        self.get_current_volume()

        # New iotPage related variables
        self.devices_list = []
        self.device_labels = {}
        self.history_title = None
        self.iot_card = None
        self.ha_update_timer = None
        self.device_states = {}

        # New system tray related variables
        self.tray_icon = None
        self.tray_menu = None
        self.current_status = ""  # Current status for color change detection
        self.is_connected = True  # Connection status flag

    def eventFilter(self, source, event):
        if source == self.volume_scale and event.type() == QEvent.MouseButtonPress:
            if event.button() == Qt.LeftButton:
                slider = self.volume_scale
                opt = QStyleOptionSlider()
                slider.initStyleOption(opt)

                # Get the rectangle areas of the slider handle and groove
                handle_rect = slider.style().subControlRect(
                    QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, slider
                )
                groove_rect = slider.style().subControlRect(
                    QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, slider
                )

                # If clicked on the handle, let the default handler process dragging
                if handle_rect.contains(event.pos()):
                    return False

                # Calculate the click position relative to the groove
                if slider.orientation() == Qt.Horizontal:
                    # Ensure click is within the valid groove range
                    if (
                        event.pos().x() < groove_rect.left()
                        or event.pos().x() > groove_rect.right()
                    ):
                        return False  # Clicked outside the groove
                    pos = event.pos().x() - groove_rect.left()
                    max_pos = groove_rect.width()
                else:
                    if (
                        event.pos().y() < groove_rect.top()
                        or event.pos().y() > groove_rect.bottom()
                    ):
                        return False  # Clicked outside the groove
                    pos = groove_rect.bottom() - event.pos().y()
                    max_pos = groove_rect.height()

                if max_pos > 0:  # Avoid division by zero
                    value_range = slider.maximum() - slider.minimum()
                    # Calculate new value based on click position
                    new_value = slider.minimum() + round((value_range * pos) / max_pos)

                    # Directly set the slider value
                    slider.setValue(int(new_value))

                    return True  # Indicate event has been handled

        return super().eventFilter(source, event)

    def _setup_navigation(self):
        """Set up navigation tab bar (QTabBar)."""
        # Use addTab to add tabs
        self.nav_tab_bar.addTab("Chat")  # index 0
        self.nav_tab_bar.addTab("Device Management")  # index 1
        self.nav_tab_bar.addTab("Settings")  # index 2

        # Connect QTabBar's currentChanged signal to handler
        self.nav_tab_bar.currentChanged.connect(self._on_navigation_index_changed)

        # Set default selected tab (by index)
        self.nav_tab_bar.setCurrentIndex(0)  # Select first tab by default

    def _on_navigation_index_changed(self, index: int):
        """Handle navigation tab change (by index)."""
        # Map back to routeKey for reusing animation and loading logic
        index_to_routeKey = {
            0: "mainInterface",
            1: "iotInterface",
            2: "settingInterface",
        }
        routeKey = index_to_routeKey.get(index)

        if routeKey is None:
            self.logger.warning(f"Unknown navigation index: {index}")
            return

        target_index = index  # Use index directly
        if target_index == self.stackedWidget.currentIndex():
            return

        self.stackedWidget.setCurrentIndex(target_index)

        # If switching to settings page, load settings
        if routeKey == "settingInterface":
            self._load_settings()

        # If switching to device management page, load devices
        if routeKey == "iotInterface":
            self._load_iot_devices()

    def set_callbacks(
        self,
        press_callback: Optional[Callable] = None,
        release_callback: Optional[Callable] = None,
        status_callback: Optional[Callable] = None,
        text_callback: Optional[Callable] = None,
        emotion_callback: Optional[Callable] = None,
        mode_callback: Optional[Callable] = None,
        auto_callback: Optional[Callable] = None,
        abort_callback: Optional[Callable] = None,
        send_text_callback: Optional[Callable] = None,
    ):
        """Set callback functions."""
        self.button_press_callback = press_callback
        self.button_release_callback = release_callback
        self.status_update_callback = status_callback
        self.text_update_callback = text_callback
        self.emotion_update_callback = emotion_callback
        self.mode_callback = mode_callback
        self.auto_callback = auto_callback
        self.abort_callback = abort_callback
        self.send_text_callback = send_text_callback

        # Add status listener to application's state change callbacks after initialization
        # This allows updating the system tray icon when device state changes
        from src.application import Application

        app = Application.get_instance()
        if app:
            app.on_state_changed_callbacks.append(self._on_state_changed)

    def _on_state_changed(self, state):
        """Listen for device state changes."""
        # Set connection status flag
        from src.constants.constants import DeviceState

        # Check if connecting or connected
        # (CONNECTING, LISTENING, SPEAKING indicate connected)
        if state == DeviceState.CONNECTING:
            self.is_connected = True
        elif state in [DeviceState.LISTENING, DeviceState.SPEAKING]:
            self.is_connected = True
        elif state == DeviceState.IDLE:
            # Get protocol instance from application to check WebSocket connection status
            from src.application import Application

            app = Application.get_instance()
            if app and app.protocol:
                # Check if protocol is connected
                self.is_connected = app.protocol.is_audio_channel_opened()
            else:
                self.is_connected = False

        # Status update handling is done in update_status method

    def _process_updates(self):
        """Process update queue."""
        if not self._running:
            return

        try:
            while True:
                try:
                    # Non-blocking retrieval of updates
                    update_func = self.update_queue.get_nowait()
                    update_func()
                    self.update_queue.task_done()
                except queue.Empty:
                    break
        except Exception as e:
            self.logger.error(f"Error processing update queue: {e}")

    def _on_manual_button_press(self):
        """Handle manual mode button press event."""
        try:
            # Update button text to "Release to Stop"
            if self.manual_btn and self.manual_btn.isVisible():
                self.manual_btn.setText("Release to Stop")

            # Call callback function
            if self.button_press_callback:
                self.button_press_callback()
        except Exception as e:
            self.logger.error(f"Button press callback execution failed: {e}")

    def _on_manual_button_release(self):
        """Handle manual mode button release event."""
        try:
            # Update button text to "Press and Hold to Speak"
            if self.manual_btn and self.manual_btn.isVisible():
                self.manual_btn.setText("Press and Hold to Speak")

            # Call callback function
            if self.button_release_callback:
                self.button_release_callback()
        except Exception as e:
            self.logger.error(f"Button release callback execution failed: {e}")

    def _on_auto_button_click(self):
        """Handle auto mode button click event."""
        try:
            if self.auto_callback:
                self.auto_callback()
        except Exception as e:
            self.logger.error(f"Auto mode button callback execution failed: {e}")

    def _on_abort_button_click(self):
        """Handle abort button click event."""
        if self.abort_callback:
            self.abort_callback()

    def _on_mode_button_click(self):
        """Handle conversation mode toggle button click event."""
        try:
            # Check if mode can be toggled (ask application for current state via callback)
            if self.mode_callback:
                # If callback returns False, mode cannot be toggled
                if not self.mode_callback(not self.auto_mode):
                    return

            # Toggle mode
            self.auto_mode = not self.auto_mode

            # Update button display
            if self.auto_mode:
                # Switch to auto mode
                self.update_mode_button_status("Auto Conversation")

                # Hide manual button, show auto button
                self.update_queue.put(self._switch_to_auto_mode)
            else:
                # Switch to manual mode
                self.update_mode_button_status("Manual Conversation")

                # Hide auto button, show manual button
                self.update_queue.put(self._switch_to_manual_mode)

        except Exception as e:
            self.logger.error(f"Mode toggle button callback execution failed: {e}")

    def _switch_to_auto_mode(self):
        """UI update for switching to auto mode."""
        if self.manual_btn and self.auto_btn:
            self.manual_btn.hide()
            self.auto_btn.show()

    def _switch_to_manual_mode(self):
        """UI update for switching to manual mode."""
        if self.manual_btn and self.auto_btn:
            self.auto_btn.hide()
            self.manual_btn.show()

    def update_status(self, status: str):
        """Update status text (main status only)."""
        full_status_text = f"Status: {status}"
        self.update_queue.put(
            lambda: self._safe_update_label(self.status_label, full_status_text)
        )

        # Update system tray icon
        if status != self.current_status:
            self.current_status = status
            self.update_queue.put(lambda: self._update_tray_icon(status))

    def update_text(self, text: str):
        """Update TTS text."""
        self.update_queue.put(
            lambda: self._safe_update_label(self.tts_text_label, text)
        )

    def update_emotion(self, emotion_path: str):
        """Update emotion animation."""
        # Avoid redundant emotion updates if path is the same
        if (
            hasattr(self, "_last_emotion_path")
            and self._last_emotion_path == emotion_path
        ):
            return

        # Record the currently set path
        self._last_emotion_path = emotion_path

        # Ensure UI updates are handled in the main thread
        if QApplication.instance().thread() != QThread.currentThread():
            # If not in main thread, use signal-slot or QMetaObject to execute in main thread
            QMetaObject.invokeMethod(
                self,
                "_update_emotion_safely",
                Qt.QueuedConnection,
                Q_ARG(str, emotion_path),
            )
        else:
            # Already in main thread, execute directly
            self._update_emotion_safely(emotion_path)

    # Add slot function to safely update emotion in main thread
    @pyqtSlot(str)
    def _update_emotion_safely(self, emotion_path: str):
        """Safely update emotion in main thread to avoid threading issues."""
        if self.emotion_label:
            self.logger.info(f"Setting emotion GIF: {emotion_path}")
            try:
                self._set_emotion_gif(self.emotion_label, emotion_path)
            except Exception as e:
                self.logger.error(f"Error setting emotion GIF: {str(e)}")

    def _set_emotion_gif(self, label, gif_path):
        """Set emotion GIF animation with fade effect."""
        # Basic checks
        if not label or self.root.isHidden():
            return

        # Check if GIF is already displayed on the current label
        if hasattr(label, "current_gif_path") and label.current_gif_path == gif_path:
            return

        # Record current GIF path to label object
        label.current_gif_path = gif_path

        try:
            # If the same animation is already set and playing, do not reset
            if (
                self.emotion_movie
                and getattr(self.emotion_movie, "_gif_path", None) == gif_path
                and self.emotion_movie.state() == QMovie.Running
            ):
                return

            # If animation is in progress, record the next emotion to display
            if self.is_emotion_animating:
                self.next_emotion_path = gif_path
                return

            # Mark animation as in progress
            self.is_emotion_animating = True

            # If an animation is already playing, fade it out first
            if self.emotion_movie and label.movie() == self.emotion_movie:
                # Create opacity effect (if not already created)
                if not self.emotion_effect:
                    self.emotion_effect = QGraphicsOpacityEffect(label)
                    label.setGraphicsEffect(self.emotion_effect)
                    self.emotion_effect.setOpacity(1.0)

                # Create fade-out animation
                self.emotion_animation = QPropertyAnimation(
                    self.emotion_effect, b"opacity"
                )
                self.emotion_animation.setDuration(180)  # Set animation duration (ms)
                self.emotion_animation.setStartValue(1.0)
                self.emotion_animation.setEndValue(0.25)

                # After fade-out, set new GIF and start fade-in
                def on_fade_out_finished():
                    try:
                        # Stop current GIF
                        if self.emotion_movie:
                            self.emotion_movie.stop()

                        # Set new GIF and fade in
                        self._set_new_emotion_gif(label, gif_path)
                    except Exception as e:
                        self.logger.error(f"Failed to set GIF after fade-out: {e}")
                        self.is_emotion_animating = False

                # Connect fade-out finished signal
                self.emotion_animation.finished.connect(on_fade_out_finished)

                # Start fade-out animation
                self.emotion_animation.start()
            else:
                # If no previous animation, set new GIF and fade in
                self._set_new_emotion_gif(label, gif_path)

        except Exception as e:
            self.logger.error(f"Failed to update emotion GIF animation: {e}")
            # If GIF loading fails, try displaying default emotion
            try:
                label.setText("😊")
            except Exception:
                pass
            self.is_emotion_animating = False

    def _set_new_emotion_gif(self, label, gif_path):
        """Set new GIF animation and perform fade-in effect."""
        try:
            # Maintain GIF cache
            if not hasattr(self, "_gif_cache"):
                self._gif_cache = {}

            # Check if GIF is in cache
            if gif_path in self._gif_cache:
                movie = self._gif_cache[gif_path]
            else:
                # Log (only on first load)
                self.logger.info(f"Loading GIF file: {gif_path}")
                # Create animation object
                movie = QMovie(gif_path)
                if not movie.isValid():
                    self.logger.error(f"Invalid GIF file: {gif_path}")
                    label.setText("😊")
                    self.is_emotion_animating = False
                    return

                # Configure animation and store in cache
                movie.setCacheMode(QMovie.CacheAll)
                self._gif_cache[gif_path] = movie

            # Save GIF path to movie object for comparison
            movie._gif_path = gif_path

            # Connect signal
            movie.error.connect(
                lambda: self.logger.error(f"GIF playback error: {movie.lastError()}")
            )

            # Save new animation object
            self.emotion_movie = movie

            # Set label size policy
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            label.setAlignment(Qt.AlignCenter)

            # Set animation to label
            label.setMovie(movie)

            # Set QMovie speed to 105 for smoother animation (default is 100)
            movie.setSpeed(105)

            # Ensure opacity is 0 (fully transparent)
            if self.emotion_effect:
                self.emotion_effect.setOpacity(0.0)
            else:
                self.emotion_effect = QGraphicsOpacityEffect(label)
                label.setGraphicsEffect(self.emotion_effect)
                self.emotion_effect.setOpacity(0.0)

            # Start playing animation
            movie.start()

            # Create fade-in animation
            self.emotion_animation = QPropertyAnimation(self.emotion_effect, b"opacity")
            self.emotion_animation.setDuration(180)  # Fade-in duration (ms)
            self.emotion_animation.setStartValue(0.25)
            self.emotion_animation.setEndValue(1.0)

            # Check for next emotion to display after fade-in
            def on_fade_in_finished():
                self.is_emotion_animating = False
                # If there is a next emotion to display, switch to it
                if self.next_emotion_path:
                    next_path = self.next_emotion_path
                    self.next_emotion_path = None
                    self._set_emotion_gif(label, next_path)

            # Connect fade-in finished signal
            self.emotion_animation.finished.connect(on_fade_in_finished)

            # Start fade-in animation
            self.emotion_animation.start()

        except Exception as e:
            self.logger.error(f"Failed to set new GIF animation: {e}")
            self.is_emotion_animating = False
            # If setting fails, try displaying default emotion
            try:
                label.setText("😊")
            except Exception:
                pass

    def _safe_update_label(self, label, text):
        """Safely update label text."""
        if label and not self.root.isHidden():
            try:
                label.setText(text)
            except RuntimeError as e:
                self.logger.error(f"Failed to update label: {e}")

    def start_update_threads(self):
        """Start update thread."""
        # Initialize emotion cache
        self.last_emotion_path = None

        def update_loop():
            while self._running:
                try:
                    # Update status
                    if self.status_update_callback:
                        status = self.status_update_callback()
                        if status:
                            self.update_status(status)

                    # Update text
                    if self.text_update_callback:
                        text = self.text_update_callback()
                        if text:
                            self.update_text(text)

                    # Update emotion - only when emotion changes
                    if self.emotion_update_callback:
                        emotion = self.emotion_update_callback()
                        if emotion:
                            # Directly call update_emotion, which handles duplicate checks
                            self.update_emotion(emotion)

                except Exception as e:
                    self.logger.error(f"Update failed: {e}")
                time.sleep(0.1)

        threading.Thread(target=update_loop, daemon=True).start()

    def on_close(self):
        """Handle window close."""
        self._running = False

        # Ensure timers are stopped in main thread
        if QThread.currentThread() != QApplication.instance().thread():
            # If in non-main thread, use QMetaObject.invokeMethod to execute in main thread
            if self.update_timer:
                QMetaObject.invokeMethod(self.update_timer, "stop", Qt.QueuedConnection)

            if self.ha_update_timer:
                QMetaObject.invokeMethod(
                    self.ha_update_timer, "stop", Qt.QueuedConnection
                )
        else:
            # Already in main thread, stop directly
            if self.update_timer:
                self.update_timer.stop()

            if self.ha_update_timer:
                self.ha_update_timer.stop()

        if self.tray_icon:
            self.tray_icon.hide()
        if self.root:
            self.root.close()
        self.stop_keyboard_listener()

    def start(self):
        """Start GUI."""
        try:
            # Ensure QApplication instance is created in main thread
            self.app = QApplication.instance()
            if self.app is None:
                self.app = QApplication(sys.argv)

            # Set default UI font
            default_font = QFont("ASLantTermuxFont Mono", 12)
            self.app.setFont(default_font)

            # Load UI file
            from PyQt5 import uic

            self.root = QWidget()
            ui_path = Path(__file__).parent / "gui_display.ui"
            if not ui_path.exists():
                self.logger.error(f"UI file not found: {ui_path}")
                raise FileNotFoundError(f"UI file not found: {ui_path}")

            uic.loadUi(str(ui_path), self.root)

            # Get UI widgets
            self.status_label = self.root.findChild(QLabel, "status_label")
            self.emotion_label = self.root.findChild(QLabel, "emotion_label")
            self.tts_text_label = self.root.findChild(QLabel, "tts_text_label")
            self.manual_btn = self.root.findChild(QPushButton, "manual_btn")
            self.abort_btn = self.root.findChild(QPushButton, "abort_btn")
            self.auto_btn = self.root.findChild(QPushButton, "auto_btn")
            self.mode_btn = self.root.findChild(QPushButton, "mode_btn")

            # Add shortcut hint label
            try:
                # Find main page layout
                main_page = self.root.findChild(QWidget, "mainPage")
                if main_page:
                    main_layout = main_page.layout()
                    if main_layout:
                        # Create shortcut hint label
                        shortcut_label = QLabel(
                            "Shortcuts: Alt+Shift+V (Press to Speak) | Alt+Shift+A (Auto Conversation) | "
                            "Alt+Shift+X (Interrupt) | Alt+Shift+M (Toggle Mode)"
                        )

                        shortcut_label.setStyleSheet(
                            """
                            font-size: 10px;
                            color: #666;
                            background-color: #f5f5f5;
                            border-radius: 4px;
                            padding: 3px;
                            margin: 2px;
                        """
                        )
                        shortcut_label.setAlignment(Qt.AlignCenter)
                        # Add label to the end of the layout
                        main_layout.addWidget(shortcut_label)
                        self.logger.info("Added shortcut hint label")
            except Exception as e:
                self.logger.warning(f"Failed to add shortcut hint label: {e}")

            # Get IOT page widget
            self.iot_card = self.root.findChild(
                QFrame, "iotPage"
            )  # Note: Use "iotPage" as ID
            if self.iot_card is None:
                # If iotPage not found, try other possible names
                self.iot_card = self.root.findChild(QFrame, "iot_card")
                if self.iot_card is None:
                    # If still not found, try getting second page from stackedWidget as iot_card
                    self.stackedWidget = self.root.findChild(
                        QStackedWidget, "stackedWidget"
                    )
                    if self.stackedWidget and self.stackedWidget.count() > 1:
                        self.iot_card = self.stackedWidget.widget(
                            1
                        )  # Index 1 is the second page
                        self.logger.info(
                            f"Using second page of stackedWidget as iot_card: {self.iot_card}"
                        )
                    else:
                        self.logger.warning("Unable to find iot_card, IOT device functionality will be unavailable")
            else:
                self.logger.info(f"Found iot_card: {self.iot_card}")

            # Volume control page
            self.volume_page = self.root.findChild(QWidget, "volume_page")

            # Volume control widgets
            self.volume_scale = self.root.findChild(QSlider, "volume_scale")
            self.mute = self.root.findChild(QPushButton, "mute")

            if self.mute:
                self.mute.setCheckable(True)
                self.mute.clicked.connect(self._on_mute_click)

            # Get or create volume percentage label
            self.volume_label = self.root.findChild(QLabel, "volume_label")
            if not self.volume_label and self.volume_scale:
                # If no volume label in UI, dynamically create one
                volume_layout = self.root.findChild(QHBoxLayout, "volume_layout")
                if volume_layout:
                    self.volume_label = QLabel(f"{self.current_volume}%")
                    self.volume_label.setObjectName("volume_label")
                    self.volume_label.setMinimumWidth(40)
                    self.volume_label.setAlignment(Qt.AlignCenter)
                    volume_layout.addWidget(self.volume_label)

            # Set widget states based on volume control availability
            volume_control_working = (
                self.volume_control_available and not self.volume_controller_failed
            )
            if not volume_control_working:
                self.logger.warning("System does not support volume control or control failed, volume control disabled")
                # Disable volume-related widgets
                if self.volume_scale:
                    self.volume_scale.setEnabled(False)
                if self.mute:
                    self.mute.setEnabled(False)
                if self.volume_label:
                    self.volume_label.setText("Unavailable")
            else:
                # Normally set volume slider initial value
                if self.volume_scale:
                    self.volume_scale.setRange(0, 100)
                    self.volume_scale.setValue(self.current_volume)
                    self.volume_scale.valueChanged.connect(self._on_volume_change)
                    self.volume_scale.installEventFilter(self)  # Install event filter
                # Update volume percentage display
                if self.volume_label:
                    self.volume_label.setText(f"{self.current_volume}%")

            # Get settings page widgets
            self.wakeWordEnableSwitch = self.root.findChild(
                QCheckBox, "wakeWordEnableSwitch"
            )
            self.wakeWordsLineEdit = self.root.findChild(QLineEdit, "wakeWordsLineEdit")
            self.saveSettingsButton = self.root.findChild(
                QPushButton, "saveSettingsButton"
            )
            # Get new widgets
            # Replace with standard PyQt widgets
            self.deviceIdLineEdit = self.root.findChild(QLineEdit, "deviceIdLineEdit")
            self.wsProtocolComboBox = self.root.findChild(
                QComboBox, "wsProtocolComboBox"
            )
            self.wsAddressLineEdit = self.root.findChild(QLineEdit, "wsAddressLineEdit")
            self.wsTokenLineEdit = self.root.findChild(QLineEdit, "wsTokenLineEdit")
            # Home Assistant widget references
            self.haProtocolComboBox = self.root.findChild(
                QComboBox, "haProtocolComboBox"
            )
            self.ha_server = self.root.findChild(QLineEdit, "ha_server")
            self.ha_port = self.root.findChild(QLineEdit, "ha_port")
            self.ha_key = self.root.findChild(QLineEdit, "ha_key")
            self.Add_ha_devices = self.root.findChild(QPushButton, "Add_ha_devices")

            # Get OTA-related widgets
            self.otaProtocolComboBox = self.root.findChild(
                QComboBox, "otaProtocolComboBox"
            )
            self.otaAddressLineEdit = self.root.findChild(
                QLineEdit, "otaAddressLineEdit"
            )

            # Explicitly add ComboBox options to prevent UI file loading issues
            if self.wsProtocolComboBox:
                # Clear first to avoid duplicates (if .ui file also loaded options)
                self.wsProtocolComboBox.clear()
                self.wsProtocolComboBox.addItems(["wss://", "ws://"])

            # Explicitly add OTA ComboBox options
            if self.otaProtocolComboBox:
                self.otaProtocolComboBox.clear()
                self.otaProtocolComboBox.addItems(["https://", "http://"])

            # Explicitly add Home Assistant protocol ComboBox options
            if self.haProtocolComboBox:
                self.haProtocolComboBox.clear()
                self.haProtocolComboBox.addItems(["http://", "https://"])

            # Get navigation widgets
            self.stackedWidget = self.root.findChild(QStackedWidget, "stackedWidget")
            self.nav_tab_bar = self.root.findChild(QTabBar, "nav_tab_bar")

            # Initialize navigation tab bar
            self._setup_navigation()

            # Connect button events
            if self.manual_btn:
                self.manual_btn.pressed.connect(self._on_manual_button_press)
                self.manual_btn.released.connect(self._on_manual_button_release)
            if self.abort_btn:
                self.abort_btn.clicked.connect(self._on_abort_button_click)
            if self.auto_btn:
                self.auto_btn.clicked.connect(self._on_auto_button_click)
                # Hide auto mode button by default
                self.auto_btn.hide()
            if self.mode_btn:
                self.mode_btn.clicked.connect(self._on_mode_button_click)

            # Initialize text input and send button
            self.text_input = self.root.findChild(QLineEdit, "text_input")
            self.send_btn = self.root.findChild(QPushButton, "send_btn")
            if self.text_input and self.send_btn:
                self.send_btn.clicked.connect(self._on_send_button_click)
                # Bind Enter key to send text
                self.text_input.returnPressed.connect(self._on_send_button_click)

            # Connect settings save button event
            if self.saveSettingsButton:
                self.saveSettingsButton.clicked.connect(self._save_settings)

            # Connect Home Assistant device import button event
            if self.Add_ha_devices:
                self.Add_ha_devices.clicked.connect(self._on_add_ha_devices_click)

            # Set mouse events
            self.root.mousePressEvent = self.mousePressEvent
            self.root.mouseReleaseEvent = self.mouseReleaseEvent

            # Set window close event
            self.root.closeEvent = self._closeEvent

            # Initialize system tray
            self._setup_tray_icon()

            # Start keyboard listener
            self.start_keyboard_listener()

            # Start update thread
            self.start_update_threads()

            # Timer to process update queue
            self.update_timer = QTimer()
            self.update_timer.timeout.connect(self._process_updates)
            self.update_timer.start(100)

            # Run main loop in main thread
            self.logger.info("Starting GUI main loop")
            self.root.show()
            # self.root.showFullScreen() # Full-screen display

        except Exception as e:
            self.logger.error(f"GUI startup failed: {e}", exc_info=True)
            # Fallback to CLI mode
            print(f"GUI startup failed: {e}, please try using CLI mode")
            raise

    def _setup_tray_icon(self):
        """Set up system tray icon."""
        try:
            # Check if system supports system tray
            if not QSystemTrayIcon.isSystemTrayAvailable():
                self.logger.warning("System does not support system tray functionality")
                return

            # Create tray menu
            self.tray_menu = QMenu()

            # Add menu items
            show_action = QAction("Show Main Window", self.root)
            show_action.triggered.connect(self._show_main_window)
            self.tray_menu.addAction(show_action)

            # Add separator
            self.tray_menu.addSeparator()

            # Add quit menu item
            quit_action = QAction("Exit Program", self.root)
            quit_action.triggered.connect(self._quit_application)
            self.tray_menu.addAction(quit_action)

            # Create system tray icon
            self.tray_icon = QSystemTrayIcon(self.root)
            self.tray_icon.setContextMenu(self.tray_menu)

            # Connect tray icon events
            self.tray_icon.activated.connect(self._tray_icon_activated)

            # Set initial icon to green
            self._update_tray_icon("Idle")

            # Show system tray icon
            self.tray_icon.show()
            self.logger.info("System tray icon initialized")

        except Exception as e:
            self.logger.error(f"Failed to initialize system tray icon: {e}", exc_info=True)

    def _update_tray_icon(self, status):
        """Update tray icon color based on status.

        Green: Started/Idle state
        Yellow: Listening state
        Blue: Speaking state
        Red: Error state
        Gray: Disconnected state
        """
        if not self.tray_icon:
            return

        try:
            icon_color = self._get_status_color(status)

            # Create icon with specified color
            pixmap = QPixmap(16, 16)
            pixmap.fill(Qt.transparent)

            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(QBrush(icon_color))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(2, 2, 12, 12)
            painter.end()

            # Set icon
            self.tray_icon.setIcon(QIcon(pixmap))

            # Set tooltip text
            tooltip = f"XiaoZhi AI Assistant - {status}"
            self.tray_icon.setToolTip(tooltip)

        except Exception as e:
            self.logger.error(f"Failed to update system tray icon: {e}")

    def _get_status_color(self, status):
        """Return color corresponding to status."""
        if not self.is_connected:
            return QColor(128, 128, 128)  # Gray - Disconnected

        if "Error" in status:
            return QColor(255, 0, 0)  # Red - Error state

        elif "Listening" in status:
            return QColor(255, 200, 0)  # Yellow - Listening state

        elif "Speaking" in status:
            return QColor(0, 120, 255)  # Blue - Speaking state

        else:
            return QColor(0, 180, 0)  # Green - Idle/Started state

    def _tray_icon_activated(self, reason):
        """Handle tray icon click event."""
        if reason == QSystemTrayIcon.Trigger:  # Single click
            self._show_main_window()

    def _show_main_window(self):
        """Show main window."""
        if self.root:
            if self.root.isMinimized():
                self.root.showNormal()
            if not self.root.isVisible():
                self.root.show()
            self.root.activateWindow()
            self.root.raise_()

    def _quit_application(self):
        """Quit application."""
        self._running = False
        # Stop all threads and timers
        if self.update_timer:
            self.update_timer.stop()

        if self.ha_update_timer:
            self.ha_update_timer.stop()

        # Stop keyboard listener
        self.stop_keyboard_listener()

        # Hide tray icon
        if self.tray_icon:
            self.tray_icon.hide()

        # Quit application
        QApplication.quit()

    def _closeEvent(self, event):
        """Handle window close event."""
        # Minimize to system tray instead of exiting
        if self.tray_icon and self.tray_icon.isVisible():
            self.root.hide()
            self.tray_icon.showMessage(
                "XiaoZhi AI Assistant",
                "The program is still running. Click the tray icon to reopen the window.",
                QSystemTrayIcon.Information,
                2000,
            )
            event.ignore()
        else:
            # If system tray is unavailable, close normally
            self._quit_application()
            event.accept()

    def update_mode_button_status(self, text: str):
        """Update mode button status."""
        self.update_queue.put(lambda: self._safe_update_button(self.mode_btn, text))

    def update_button_status(self, text: str):
        """Update button status - retained to meet abstract base class requirements."""
        # Update the appropriate button based on current mode
        if self.auto_mode:
            self.update_queue.put(lambda: self._safe_update_button(self.auto_btn, text))
        else:
            # In manual mode, button text is controlled directly by press/release events
            pass

    def _safe_update_button(self, button, text):
        """Safely update button text."""
        if button and not self.root.isHidden():
            try:
                button.setText(text)
            except RuntimeError as e:
                self.logger.error(f"Failed to update button: {e}")

    def _on_volume_change(self, value):
        """Handle volume slider change with throttling."""

        def update_volume():
            self.update_volume(value)

        # Cancel previous timer
        if (
            hasattr(self, "volume_update_timer")
            and self.volume_update_timer
            and self.volume_update_timer.isActive()
        ):
            self.volume_update_timer.stop()

        # Set new timer to update volume after 300ms
        self.volume_update_timer = QTimer()
        self.volume_update_timer.setSingleShot(True)
        self.volume_update_timer.timeout.connect(update_volume)
        self.volume_update_timer.start(300)

    def update_volume(self, volume: int):
        """Override parent's update_volume method to ensure UI synchronization."""
        # Check if volume control is available
        if not self.volume_control_available or self.volume_controller_failed:
            return

        # Call parent's update_volume method to update system volume
        super().update_volume(volume)

        # Update UI volume slider and label
        if not self.root.isHidden():
            try:
                if self.volume_scale:
                    self.volume_scale.setValue(volume)
                if self.volume_label:
                    self.volume_label.setText(f"{volume}%")
            except RuntimeError as e:
                self.logger.error(f"Failed to update volume UI: {e}")

    def is_combo(self, *keys):
        """Check if a combination of keys is pressed."""
        return all(k in self.pressed_keys for k in keys)

    def start_keyboard_listener(self):
        """Start keyboard listener."""
        # If pynput is unavailable, log warning and return
        if pynput_keyboard is None:
            self.logger.warning(
                "Keyboard listener unavailable: pynput library failed to load. Shortcut functionality will be unavailable."
            )
            return

        try:

            def on_press(key):
                try:
                    # Record pressed keys
                    if (
                        key == pynput_keyboard.Key.alt_l
                        or key == pynput_keyboard.Key.alt_r
                    ):
                        self.pressed_keys.add("alt")
                    elif (
                        key == pynput_keyboard.Key.shift_l
                        or key == pynput_keyboard.Key.shift_r
                    ):
                        self.pressed_keys.add("shift")
                    elif hasattr(key, "char") and key.char:
                        self.pressed_keys.add(key.char.lower())

                    # Long-press to speak - handle in manual mode
                    if not self.auto_mode and self.is_combo("alt", "shift", "v"):
                        if self.button_press_callback:
                            self.button_press_callback()
                            if self.manual_btn:
                                self.update_queue.put(
                                    lambda: self._safe_update_button(
                                        self.manual_btn, "Release to Stop"
                                    )
                                )

                    # Auto conversation mode
                    if self.is_combo("alt", "shift", "a"):
                        if self.auto_callback:
                            self.auto_callback()

                    # Interrupt
                    if self.is_combo("alt", "shift", "x"):
                        if self.abort_callback:
                            self.abort_callback()

                    # Mode toggle
                    if self.is_combo("alt", "shift", "m"):
                        self._on_mode_button_click()

                except Exception as e:
                    self.logger.error(f"Keyboard event handling error: {e}")

            def on_release(key):
                try:
                    # Clear released keys
                    if (
                        key == pynput_keyboard.Key.alt_l
                        or key == pynput_keyboard.Key.alt_r
                    ):
                        self.pressed_keys.discard("alt")
                    elif (
                        key == pynput_keyboard.Key.shift_l
                        or key == pynput_keyboard.Key.shift_r
                    ):
                        self.pressed_keys.discard("shift")
                    elif hasattr(key, "char") and key.char:
                        self.pressed_keys.discard(key.char.lower())

                    # Release keys, stop voice input (only in manual mode)
                    if not self.auto_mode and not self.is_combo("alt", "shift", "v"):
                        if self.button_release_callback:
                            self.button_release_callback()
                            if self.manual_btn:
                                self.update_queue.put(
                                    lambda: self._safe_update_button(
                                        self.manual_btn, "Press and Hold to Speak"
                                    )
                                )
                except Exception as e:
                    self.logger.error(f"Keyboard event handling error: {e}")

            # Create and start listener
            self.keyboard_listener = pynput_keyboard.Listener(
                on_press=on_press, on_release=on_release
            )
            self.keyboard_listener.start()
            self.logger.info("Keyboard listener initialized successfully")
        except Exception as e:
            self.logger.error(f"Failed to initialize keyboard listener: {e}")

    def stop_keyboard_listener(self):
        """Stop keyboard listener."""
        if self.keyboard_listener:
            try:
                self.keyboard_listener.stop()
                self.keyboard_listener = None
                self.logger.info("Keyboard listener stopped")
            except Exception as e:
                self.logger.error(f"Failed to stop keyboard listener: {e}")

    def mousePressEvent(self, event: QMouseEvent):
        """Handle mouse press event."""
        if event.button() == Qt.LeftButton:
            self.last_mouse_pos = event.pos()

    def mouseReleaseEvent(self, event: QMouseEvent):
        """Handle mouse release event (modified to use QTabBar index)."""
        if event.button() == Qt.LeftButton and self.last_mouse_pos is not None:
            delta = event.pos().x() - self.last_mouse_pos.x()
            self.last_mouse_pos = None

            if abs(delta) > 100:  # Swipe threshold
                current_index = (
                    self.nav_tab_bar.currentIndex() if self.nav_tab_bar else 0
                )
                tab_count = self.nav_tab_bar.count() if self.nav_tab_bar else 0

                if delta > 0 and current_index > 0:  # Swipe right
                    new_index = current_index - 1
                    if self.nav_tab_bar:
                        self.nav_tab_bar.setCurrentIndex(new_index)
                elif delta < 0 and current_index < tab_count - 1:  # Swipe left
                    new_index = current_index + 1
                    if self.nav_tab_bar:
                        self.nav_tab_bar.setCurrentIndex(new_index)

    def _on_mute_click(self):
        """Handle mute button click event (using isChecked state)."""
        try:
            if (
                not self.volume_control_available
                or self.volume_controller_failed
                or not self.mute
            ):
                return

            self.is_muted = self.mute.isChecked()  # Get button checked state

            if self.is_muted:
                # Save current volume and set to 0
                self.pre_mute_volume = self.current_volume
                self.update_volume(0)
                self.mute.setText("Unmute")  # Update text
                if self.volume_label:
                    self.volume_label.setText("Muted")  # Or "0%"
            else:
                # Restore previous volume
                self.update_volume(self.pre_mute_volume)
                self.mute.setText("Click to Mute")  # Restore text
                if self.volume_label:
                    self.volume_label.setText(f"{self.pre_mute_volume}%")

        except Exception as e:
            self.logger.error(f"Failed to handle mute button click event: {e}")

    def _load_settings(self):
        """Load configuration file and update settings page UI (using ConfigManager)."""
        try:
            # Use ConfigManager to get configuration
            config_manager = ConfigManager.get_instance()

            # Get wake word configuration
            use_wake_word = config_manager.get_config(
                "WAKE_WORD_OPTIONS.USE_WAKE_WORD", False
            )
            wake_words = config_manager.get_config("WAKE_WORD_OPTIONS.WAKE_WORDS", [])

            if self.wakeWordEnableSwitch:
                self.wakeWordEnableSwitch.setChecked(use_wake_word)

            if self.wakeWordsLineEdit:
                self.wakeWordsLineEdit.setText(", ".join(wake_words))

            # Get system options
            device_id = config_manager.get_config("SYSTEM_OPTIONS.DEVICE_ID", "")
            websocket_url = config_manager.get_config(
                "SYSTEM_OPTIONS.NETWORK.WEBSOCKET_URL", ""
            )
            websocket_token = config_manager.get_config(
                "SYSTEM_OPTIONS.NETWORK.WEBSOCKET_ACCESS_TOKEN", ""
            )
            ota_url = config_manager.get_config(
                "SYSTEM_OPTIONS.NETWORK.OTA_VERSION_URL", ""
            )

            if self.deviceIdLineEdit:
                self.deviceIdLineEdit.setText(device_id)

            # Parse WebSocket URL and set protocol and address
            if websocket_url and self.wsProtocolComboBox and self.wsAddressLineEdit:
                try:
                    parsed_url = urlparse(websocket_url)
                    protocol = parsed_url.scheme

                    # Preserve trailing slash in URL
                    address = parsed_url.netloc + parsed_url.path

                    # Ensure address does not start with protocol
                    if address.startswith(f"{protocol}://"):
                        address = address[len(f"{protocol}://") :]

                    index = self.wsProtocolComboBox.findText(
                        f"{protocol}://", Qt.MatchFixedString
                    )
                    if index >= 0:
                        self.wsProtocolComboBox.setCurrentIndex(index)
                    else:
                        self.logger.warning(f"Unknown WebSocket protocol: {protocol}")
                        self.wsProtocolComboBox.setCurrentIndex(0)  # Default to wss

                    self.wsAddressLineEdit.setText(address)
                except Exception as e:
                    self.logger.error(
                        f"Error parsing WebSocket URL: {websocket_url} - {e}"
                    )
                    self.wsProtocolComboBox.setCurrentIndex(0)
                    self.wsAddressLineEdit.clear()

            if self.wsTokenLineEdit:
                self.wsTokenLineEdit.setText(websocket_token)

            # Parse OTA URL and set protocol and address
            if ota_url and self.otaProtocolComboBox and self.otaAddressLineEdit:
                try:
                    parsed_url = urlparse(ota_url)
                    protocol = parsed_url.scheme

                    # Preserve trailing slash in URL
                    address = parsed_url.netloc + parsed_url.path

                    # Ensure address does not start with protocol
                    if address.startswith(f"{protocol}://"):
                        address = address[len(f"{protocol}://") :]

                    if protocol == "https":
                        self.otaProtocolComboBox.setCurrentIndex(0)
                    elif protocol == "http":
                        self.otaProtocolComboBox.setCurrentIndex(1)
                    else:
                        self.logger.warning(f"Unknown OTA protocol: {protocol}")
                        self.otaProtocolComboBox.setCurrentIndex(0)  # Default to https

                    self.otaAddressLineEdit.setText(address)
                except Exception as e:
                    self.logger.error(f"Error parsing OTA URL: {ota_url} - {e}")
                    self.otaProtocolComboBox.setCurrentIndex(0)
                    self.otaAddressLineEdit.clear()

            # Load Home Assistant configuration
            # Load Home Assistant configuration
            ha_options = config_manager.get_config("HOME_ASSISTANT", {})
            ha_url = ha_options.get("URL", "")
            ha_token = ha_options.get("TOKEN", "")

            # Parse Home Assistant URL and set protocol and address
            if ha_url and self.haProtocolComboBox and self.ha_server:
                try:
                    parsed_url = urlparse(ha_url)
                    protocol = parsed_url.scheme
                    port = parsed_url.port
                    # Address part does not include port
                    address = parsed_url.netloc
                    if ":" in address:  # If address contains port number
                        address = address.split(":")[0]

                    # Set protocol
                    if protocol == "https":
                        self.haProtocolComboBox.setCurrentIndex(1)
                    else:  # http or other protocols, default to http
                        self.haProtocolComboBox.setCurrentIndex(0)

                    # Set address
                    self.ha_server.setText(address)

                    # Set port (if available)
                    if port and self.ha_port:
                        self.ha_port.setText(str(port))
                except Exception as e:
                    self.logger.error(f"Error parsing Home Assistant URL: {ha_url} - {e}")
                    # Use default values on error
                    self.haProtocolComboBox.setCurrentIndex(0)  # Default to http
                    self.ha_server.clear()

            # Set Home Assistant Token
            if self.ha_key:
                self.ha_key.setText(ha_token)

        except Exception as e:
            self.logger.error(f"Error loading configuration file: {e}", exc_info=True)
            QMessageBox.critical(self.root, "Error", f"Failed to load settings: {e}")

    def _save_settings(self):
        """Save changes from the settings page to the configuration file (using ConfigManager)"""
        try:
            # Get ConfigManager instance
            config_manager = ConfigManager.get_instance()

            # Collect all configuration values from the UI
            # Wake word configuration
            use_wake_word = (
                self.wakeWordEnableSwitch.isChecked()
                if self.wakeWordEnableSwitch
                else False
            )
            wake_words_text = (
                self.wakeWordsLineEdit.text() if self.wakeWordsLineEdit else ""
            )
            wake_words = [
                word.strip() for word in wake_words_text.split(",") if word.strip()
            ]

            # System options
            new_device_id = (
                self.deviceIdLineEdit.text() if self.deviceIdLineEdit else ""
            )
            selected_protocol_text = (
                self.wsProtocolComboBox.currentText()
                if self.wsProtocolComboBox
                else "wss://"
            )
            selected_protocol = selected_protocol_text.replace("://", "")
            new_ws_address = (
                self.wsAddressLineEdit.text() if self.wsAddressLineEdit else ""
            )
            new_ws_token = self.wsTokenLineEdit.text() if self.wsTokenLineEdit else ""

            # OTA address configuration
            selected_ota_protocol_text = (
                self.otaProtocolComboBox.currentText()
                if self.otaProtocolComboBox
                else "https://"
            )
            selected_ota_protocol = selected_ota_protocol_text.replace("://", "")
            new_ota_address = (
                self.otaAddressLineEdit.text() if self.otaAddressLineEdit else ""
            )

            # Ensure address does not start with /
            if new_ws_address.startswith("/"):
                new_ws_address = new_ws_address[1:]

            # Construct WebSocket URL
            new_websocket_url = f"{selected_protocol}://{new_ws_address}"
            if new_websocket_url and not new_websocket_url.endswith("/"):
                new_websocket_url += "/"

            # Construct OTA URL
            new_ota_url = f"{selected_ota_protocol}://{new_ota_address}"
            if new_ota_url and not new_ota_url.endswith("/"):
                new_ota_url += "/"

            # Home Assistant configuration
            ha_protocol = (
                self.haProtocolComboBox.currentText().replace("://", "")
                if self.haProtocolComboBox
                else "http"
            )
            ha_server = self.ha_server.text() if self.ha_server else ""
            ha_port = self.ha_port.text() if self.ha_port else ""
            ha_key = self.ha_key.text() if self.ha_key else ""

            # Build Home Assistant URL
            if ha_server:
                ha_url = f"{ha_protocol}://{ha_server}"
                if ha_port:
                    ha_url += f":{ha_port}"
            else:
                ha_url = ""

            # Get the complete current configuration
            current_config = config_manager._config.copy()

            # Get the latest device list via ConfigManager
            try:
                # Re-obtain ConfigManager instance to ensure the latest configuration
                fresh_config_manager = ConfigManager.get_instance()
                latest_devices = fresh_config_manager.get_config(
                    "HOME_ASSISTANT.DEVICES", []
                )
                self.logger.info(f"Read {len(latest_devices)} devices from configuration manager")
            except Exception as e:
                self.logger.error(f"Failed to read device list via configuration manager: {e}")
                # If reading fails, use the in-memory device list
                if (
                    "HOME_ASSISTANT" in current_config
                    and "DEVICES" in current_config["HOME_ASSISTANT"]
                ):
                    latest_devices = current_config["HOME_ASSISTANT"]["DEVICES"]
                else:
                    latest_devices = []

            # Update configuration object (without writing to file)
            # 1. Update wake word configuration
            if "WAKE_WORD_OPTIONS" not in current_config:
                current_config["WAKE_WORD_OPTIONS"] = {}
            current_config["WAKE_WORD_OPTIONS"]["USE_WAKE_WORD"] = use_wake_word
            current_config["WAKE_WORD_OPTIONS"]["WAKE_WORDS"] = wake_words

            # 2. Update system options
            if "SYSTEM_OPTIONS" not in current_config:
                current_config["SYSTEM_OPTIONS"] = {}
            current_config["SYSTEM_OPTIONS"]["DEVICE_ID"] = new_device_id

            if "NETWORK" not in current_config["SYSTEM_OPTIONS"]:
                current_config["SYSTEM_OPTIONS"]["NETWORK"] = {}
            current_config["SYSTEM_OPTIONS"]["NETWORK"][
                "WEBSOCKET_URL"
            ] = new_websocket_url
            current_config["SYSTEM_OPTIONS"]["NETWORK"][
                "WEBSOCKET_ACCESS_TOKEN"
            ] = new_ws_token
            current_config["SYSTEM_OPTIONS"]["NETWORK"]["OTA_VERSION_URL"] = new_ota_url

            # 3. Update Home Assistant configuration
            if "HOME_ASSISTANT" not in current_config:
                current_config["HOME_ASSISTANT"] = {}
            current_config["HOME_ASSISTANT"]["URL"] = ha_url
            current_config["HOME_ASSISTANT"]["TOKEN"] = ha_key

            # Use the latest device list
            current_config["HOME_ASSISTANT"]["DEVICES"] = latest_devices

            # Save the entire configuration at once
            save_success = config_manager._save_config(current_config)

            if save_success:
                self.logger.info("Settings successfully saved to config.json")
                reply = QMessageBox.question(
                    self.root,
                    "Save Successful",
                    "Settings have been saved.\nSome settings require restarting the application to take effect.\n\nWould you like to restart now?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )

                if reply == QMessageBox.Yes:
                    self.logger.info("User chose to restart the application.")
                    restart_program()
            else:
                raise Exception("Failed to save configuration file")

        except Exception as e:
            self.logger.error(f"Unknown error occurred while saving settings: {e}", exc_info=True)
            QMessageBox.critical(self.root, "Error", f"Failed to save settings: {e}")

    def _on_add_ha_devices_click(self):
        """Handle the click event for the Add Home Assistant Devices button."""
        try:
            self.logger.info("Launching Home Assistant device manager...")

            # Use resource_finder to locate the script path
            from src.utils.resource_finder import get_project_root

            project_root = get_project_root()
            script_path = project_root / "src" / "ui" / "ha_device_manager" / "index.py"

            if not script_path.exists():
                self.logger.error(f"Device manager script does not exist: {script_path}")
                QMessageBox.critical(self.root, "Error", "Device manager script does not exist")
                return

            # Build command and execute
            cmd = [sys.executable, str(script_path)]

            # Launch new process using subprocess
            import subprocess

            subprocess.Popen(cmd)

        except Exception as e:
            self.logger.error(f"Failed to launch Home Assistant device manager: {e}", exc_info=True)
            QMessageBox.critical(self.root, "Error", f"Failed to launch device manager: {e}")

    def _on_send_button_click(self):
        """Handle the click event for the send text button."""
        if not self.text_input or not self.send_text_callback:
            return

        text = self.text_input.text().strip()
        if not text:
            return

        # Clear the input field
        self.text_input.clear()

        # Get the application's event loop and run the coroutine
        from src.application import Application

        app = Application.get_instance()
        if app and app.loop:
            import asyncio

            asyncio.run_coroutine_threadsafe(self.send_text_callback(text), app.loop)
        else:
            self.logger.error("Application instance or event loop is unavailable")

    def _load_iot_devices(self):
        """Load and display the Home Assistant device list."""
        try:
            # Clear the existing device list
            if hasattr(self, "devices_list") and self.devices_list:
                for widget in self.devices_list:
                    widget.deleteLater()
                self.devices_list = []

            # Clear device state label references
            self.device_labels = {}

            # Get the device layout
            if self.iot_card:
                # Record the original title text for later restoration
                title_text = ""
                if self.history_title:
                    title_text = self.history_title.text()

                # Set self.history_title to None to avoid reference errors during layout clearing
                self.history_title = None

                # Get the existing layout and remove all child widgets
                old_layout = self.iot_card.layout()
                if old_layout:
                    # Clear all widgets in the layout
                    while old_layout.count():
                        item = old_layout.takeAt(0)
                        widget = item.widget()
                        if widget:
                            widget.deleteLater()

                    # Reuse the existing layout
                    new_layout = old_layout
                else:
                    # If no existing layout, create a new one
                    new_layout = QVBoxLayout()
                    self.iot_card.setLayout(new_layout)

                # Reset layout properties
                new_layout.setContentsMargins(2, 2, 2, 2)  # Further reduce margins
                new_layout.setSpacing(2)  # Further reduce spacing between widgets

                # Create title
                self.history_title = QLabel(title_text)
                self.history_title.setFont(
                    QFont(self.app.font().family(), 12)
                )  # Reduce font size
                self.history_title.setAlignment(Qt.AlignCenter)  # Center alignment
                self.history_title.setContentsMargins(5, 2, 0, 2)  # Set title margins
                self.history_title.setMaximumHeight(25)  # Reduce title height
                new_layout.addWidget(self.history_title)

                # Attempt to load device list via ConfigManager
                try:
                    config_manager = ConfigManager.get_instance()
                    devices = config_manager.get_config("HOME_ASSISTANT.DEVICES", [])

                    # Update title
                    self.history_title.setText(f"Connected Devices ({len(devices)})")

                    # Create scroll area
                    scroll_area = QScrollArea()
                    scroll_area.setWidgetResizable(True)
                    scroll_area.setFrameShape(QFrame.NoFrame)  # Remove border
                    scroll_area.setStyleSheet("background: transparent;")  # Transparent background

                    # Create content container for scroll area
                    container = QWidget()
                    container.setStyleSheet("background: transparent;")  # Transparent background

                    # Create grid layout, set to top alignment
                    grid_layout = QGridLayout(container)
                    grid_layout.setContentsMargins(3, 3, 3, 3)  # Increase margins
                    grid_layout.setSpacing(8)  # Increase grid spacing
                    grid_layout.setAlignment(Qt.AlignTop)  # Set top alignment

                    # Set number of cards per row
                    cards_per_row = 3  # Display 3 device cards per row

                    # Iterate through devices and add to grid layout
                    for i, device in enumerate(devices):
                        entity_id = device.get("entity_id", "")
                        friendly_name = device.get("friendly_name", "")

                        # Parse friendly_name - extract location and device name
                        location = friendly_name
                        device_name = ""
                        if "," in friendly_name:
                            parts = friendly_name.split(",", 1)
                            location = parts[0].strip()
                            device_name = parts[1].strip()

                        # Create device card (using QFrame instead of CardWidget)
                        device_card = QFrame()
                        device_card.setMinimumHeight(90)  # Increase minimum height
                        device_card.setMaximumHeight(150)  # Increase maximum height to accommodate wrapped text
                        device_card.setMinimumWidth(200)  # Increase width
                        device_card.setProperty("entity_id", entity_id)  # Store entity_id
                        # Set card style - light background, rounded corners, shadow effect
                        device_card.setStyleSheet(
                            """
                            QFrame {
                                border-radius: 5px;
                                background-color: rgba(255, 255, 255, 0.7);
                                border: none;
                            }
                        """
                        )

                        card_layout = QVBoxLayout(device_card)
                        card_layout.setContentsMargins(10, 8, 10, 8)  # Inner margins
                        card_layout.setSpacing(2)  # Widget spacing

                        # Device name - displayed on first line (bold) with wrapping
                        device_name_label = QLabel(f"<b>{device_name}</b>")
                        device_name_label.setFont(QFont(self.app.font().family(), 14))
                        device_name_label.setWordWrap(True)  # Enable auto-wrapping
                        device_name_label.setMinimumHeight(20)  # Set minimum height
                        device_name_label.setSizePolicy(
                            QSizePolicy.Expanding, QSizePolicy.Minimum
                        )  # Horizontal expansion, vertical minimum
                        card_layout.addWidget(device_name_label)

                        # Device location - displayed on second line (non-bold)
                        location_label = QLabel(f"{location}")
                        location_label.setFont(QFont(self.app.font().family(), 12))
                        location_label.setStyleSheet("color: #666666;")
                        card_layout.addWidget(location_label)

                        # Add separator line
                        line = QFrame()
                        line.setFrameShape(QFrame.HLine)
                        line.setFrameShadow(QFrame.Sunken)
                        line.setStyleSheet("background-color: #E0E0E0;")
                        line.setMaximumHeight(1)
                        card_layout.addWidget(line)

                        # Device status - set default status based on device type
                        state_text = "Unknown"
                        if "light" in entity_id:
                            state_text = "Off"
                            status_display = f"Status: {state_text}"
                        elif "sensor" in entity_id:
                            if "temperature" in entity_id:
                                state_text = "0℃"
                                status_display = state_text
                            elif "humidity" in entity_id:
                                state_text = "0%"
                                status_display = state_text
                            else:
                                state_text = "Normal"
                                status_display = f"Status: {state_text}"
                        elif "switch" in entity_id:
                            state_text = "Off"
                            status_display = f"Status: {state_text}"
                        elif "button" in entity_id:
                            state_text = "Available"
                            status_display = f"Status: {state_text}"
                        else:
                            status_display = state_text

                        # Directly display status value
                        state_label = QLabel(status_display)
                        state_label.setFont(QFont(self.app.font().family(), 14))
                        state_label.setStyleSheet(
                            "color: #2196F3; border: none;"
                        )  # Add no-border style
                        card_layout.addWidget(state_label)

                        # Save state label reference
                        self.device_labels[entity_id] = state_label

                        # Calculate row and column position
                        row = i // cards_per_row
                        col = i % cards_per_row

                        # Add card to grid layout
                        grid_layout.addWidget(device_card, row, col)

                        # Save reference for later cleanup
                        self.devices_list.append(device_card)

                    # Set scroll area content
                    container.setLayout(grid_layout)
                    scroll_area.setWidget(container)

                    # Add scroll area to main layout
                    new_layout.addWidget(scroll_area)

                    # Set scroll area style
                    scroll_area.setStyleSheet(
                        """
                        QScrollArea {
                            border: none;
                            background-color: transparent;
                        }
                        QScrollBar:vertical {
                            border: none;
                            background-color: #F5F5F5;
                            width: 8px;
                            border-radius: 4px;
                        }
                        QScrollBar::handle:vertical {
                            background-color: #BDBDBD;
                            border-radius: 4px;
                        }
                        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                            height: 0px;
                        }
                    """
                    )

                    # Stop existing update timer (if any)
                    if self.ha_update_timer and self.ha_update_timer.isActive():
                        self.ha_update_timer.stop()

                    # Create and start a timer to update device states every 1 second
                    self.ha_update_timer = QTimer()
                    self.ha_update_timer.timeout.connect(self._update_device_states)
                    self.ha_update_timer.start(1000)  # Update every 1 second

                    # Perform an immediate update
                    self._update_device_states()

                except Exception as e:
                    # If loading devices fails, create an error prompt layout
                    self.logger.error(f"Failed to read device configuration: {e}")
                    self.history_title = QLabel("Failed to Load Device Configuration")
                    self.history_title.setFont(
                        QFont(self.app.font().family(), 14, QFont.Bold)
                    )
                    self.history_title.setAlignment(Qt.AlignCenter)
                    new_layout.addWidget(self.history_title)

                    error_label = QLabel(f"Error Message: {str(e)}")
                    error_label.setWordWrap(True)
                    error_label.setStyleSheet("color: red;")
                    new_layout.addWidget(error_label)

        except Exception as e:
            self.logger.error(f"Failed to load IOT devices: {e}", exc_info=True)
            try:
                # Attempt to restore the interface on error
                old_layout = self.iot_card.layout()

                # If there is an existing layout, clear it
                if old_layout:
                    while old_layout.count():
                        item = old_layout.takeAt(0)
                        widget = item.widget()
                        if widget:
                            widget.deleteLater()

                    # Use existing layout
                    new_layout = old_layout
                else:
                    # Create new layout
                    new_layout = QVBoxLayout()
                    self.iot_card.setLayout(new_layout)

                self.history_title = QLabel("Failed to Load Devices")
                self.history_title.setFont(
                    QFont(self.app.font().family(), 14, QFont.Bold)
                )
                self.history_title.setAlignment(Qt.AlignCenter)
                new_layout.addWidget(self.history_title)

                error_label = QLabel(f"Error Message: {str(e)}")
                error_label.setWordWrap(True)
                error_label.setStyleSheet("color: red;")
                new_layout.addWidget(error_label)

            except Exception as e2:
                self.logger.error(f"Failed to restore interface: {e2}", exc_info=True)

    def _update_device_states(self):
        """Update Home Assistant device states."""
        # Check if currently on the IOT interface
        if not self.stackedWidget or self.stackedWidget.currentIndex() != 1:
            return

        # Get Home Assistant connection information via ConfigManager
        try:
            config_manager = ConfigManager.get_instance()
            ha_url = config_manager.get_config("HOME_ASSISTANT.URL", "")
            ha_token = config_manager.get_config("HOME_ASSISTANT.TOKEN", "")

            if not ha_url or not ha_token:
                self.logger.warning("Home Assistant URL or Token not configured, unable to update device states")
                return

            # Query state for each device
            for entity_id, label in self.device_labels.items():
                threading.Thread(
                    target=self._fetch_device_state,
                    args=(ha_url, ha_token, entity_id, label),
                    daemon=True,
                ).start()

        except Exception as e:
            self.logger.error(f"Failed to update Home Assistant device states: {e}", exc_info=True)

    def _fetch_device_state(self, ha_url, ha_token, entity_id, label):
        """Fetch the state of a single device."""
        import requests

        try:
            # Construct API request URL
            api_url = f"{ha_url}/api/states/{entity_id}"
            headers = {
                "Authorization": f"Bearer {ha_token}",
                "Content-Type": "application/json",
            }

            # Send request
            response = requests.get(api_url, headers=headers, timeout=5)

            if response.status_code == 200:
                state_data = response.json()
                state = state_data.get("state", "unknown")

                # Update device state
                self.device_states[entity_id] = state

                # Update UI
                self._update_device_ui(entity_id, state, label)
            else:
                self.logger.warning(
                    f"Failed to fetch device state: {entity_id}, status code: {response.status_code}"
                )

        except requests.RequestException as e:
            self.logger.error(f"Failed to request Home Assistant API: {e}")
        except Exception as e:
            self.logger.error(f"Error processing device state: {e}")

    def _update_device_ui(self, entity_id, state, label):
        """Update device UI display."""
        # Perform UI update in the main thread
        self.update_queue.put(
            lambda: self._safe_update_device_label(entity_id, state, label)
        )

    def _safe_update_device_label(self, entity_id, state, label):
        """Safely update device state label."""
        if not label or self.root.isHidden():
            return

        try:
            display_state = state  # Default to raw state

            # Format state display based on device type
            if "light" in entity_id or "switch" in entity_id:
                if state == "on":
                    display_state = "Status: On"
                    label.setStyleSheet(
                        "color: #4CAF50; border: none;"
                    )  # Green for on, no border
                else:
                    display_state = "Status: Off"
                    label.setStyleSheet(
                        "color: #9E9E9E; border: none;"
                    )  # Gray for off, no border
            elif "temperature" in entity_id:
                try:
                    temp = float(state)
                    display_state = f"{temp:.1f}℃"
                    label.setStyleSheet(
                        "color: #FF9800; border: none;"
                    )  # Orange for temperature, no border
                except ValueError:
                    display_state = state
            elif "humidity" in entity_id:
                try:
                    humidity = float(state)
                    display_state = f"{humidity:.0f}%"
                    label.setStyleSheet(
                        "color: #03A9F4; border: none;"
                    )  # Light blue for humidity, no border
                except ValueError:
                    display_state = state
            elif "battery" in entity_id:
                try:
                    battery = float(state)
                    display_state = f"{battery:.0f}%"
                    # Set color based on battery level
                    if battery < 20:
                        label.setStyleSheet(
                            "color: #F44336; border: none;"
                        )  # Red for low battery, no border
                    else:
                        label.setStyleSheet(
                            "color: #4CAF50; border: none;"
                        )  # Green for normal battery, no border
                except ValueError:
                    display_state = state
            else:
                display_state = f"Status: {state}"
                label.setStyleSheet("color: #2196F3; border: none;")  # Default color, no border

            # Display state value
            label.setText(f"{display_state}")
        except RuntimeError as e:
            self.logger.error(f"Failed to update device state label: {e}")
