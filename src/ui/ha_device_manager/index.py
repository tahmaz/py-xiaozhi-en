# index.py
# -*- coding: utf-8 -*-
"""
Home Assistant Device Manager - Graphical Interface
Used to query Home Assistant devices and add them to the configuration file
"""
import os
import sys
import json
import logging
from typing import Any, Dict, List, Optional

# Add project root to system path
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))
sys.path.append(project_root)

from src.utils.config_manager import ConfigManager

try:
    from PyQt5.QtCore import Qt, QThread, pyqtSignal
    from PyQt5.QtGui import QColor
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QMessageBox, QPushButton, QTableWidgetItem,
        QTabBar, QStackedWidget, QVBoxLayout, QHBoxLayout, QComboBox,
        QLineEdit, QTableWidget, QHeaderView, QWidget, QFrame
    )
except ImportError:
    print("Error: PyQt5 library is not installed")
    print("Please run: pip install PyQt5")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Error: requests library is not installed")
    print("Please run: pip install requests")
    sys.exit(1)

# Hypothetical Sweep import
from sweep_framework.core import SweepApplication

# Device type and icon mapping
DOMAIN_ICONS = {
    "light": "Light 💡",
    "switch": "Switch 🔌",
    "sensor": "Sensor 🌡️",
    "climate": "Climate ❄️",
    "fan": "Fan 💨",
    "media_player": "Media Player 📺",
    "camera": "Camera 📷",
    "cover": "Cover 🪟",
    "vacuum": "Vacuum Cleaner 🧹",
    "binary_sensor": "Binary Sensor 🔔",
    "lock": "Lock 🔒",
    "alarm_control_panel": "Alarm Control Panel 🚨",
    "automation": "Automation ⚙️",
    "script": "Script 📜",
}

class DeviceLoadThread(QThread):
    """Thread for loading devices."""
    devices_loaded = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def __init__(self, url, token, domain="all"):
        super().__init__()
        self.url = url
        self.token = token
        self.domain = domain
        self._is_running = True

    def run(self):
        try:
            if not self._is_running:
                return
            devices = self.get_device_list(self.url, self.token, self.domain)
            if not self._is_running:
                return
            self.devices_loaded.emit(devices)
        except Exception as e:
            if self._is_running:
                self.error_occurred.emit(str(e))

    def terminate(self):
        """Safely terminate the thread."""
        self._is_running = False
        super().terminate()

    def get_device_list(self, url: str, token: str, domain: str = "all") -> List[Dict[str, Any]]:
        """Fetch device list from Home Assistant API."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.get(f"{url}/api/states", headers=headers, timeout=10)
            if response.status_code != 200:
                self.error_occurred.emit(f"Error: Unable to retrieve device list (HTTP {response.status_code}): {response.text}")
                return []
            if not self._is_running:
                return []
            entities = response.json()
            domain_entities = []
            for entity in entities:
                if not self._is_running:
                    return []
                entity_id = entity.get("entity_id", "")
                entity_domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
                if domain == "all" or entity_domain == domain:
                    domain_entities.append({
                        "entity_id": entity_id,
                        "domain": entity_domain,
                        "friendly_name": entity.get("attributes", {}).get("friendly_name", entity_id),
                        "state": entity.get("state", "unknown"),
                    })
            domain_entities.sort(key=lambda x: (x["domain"], x["friendly_name"]))
            return domain_entities
        except Exception as e:
            if self._is_running:
                self.error_occurred.emit(f"Error: Failed to retrieve device list - {e}")
            return []

class HomeAssistantDeviceManager(QMainWindow):
    """Home Assistant Device Manager GUI."""

    def __init__(self):
        super().__init__()
        self.config = ConfigManager.get_instance()
        self.ha_url = self.config.get_config("HOME_ASSISTANT.URL", "")
        self.ha_token = self.config.get_config("HOME_ASSISTANT.TOKEN", "")
        if not self.ha_url or not self.ha_token:
            QMessageBox.critical(
                self,
                "Configuration Error",
                "Home Assistant configuration not found. Please ensure config/config.json contains valid\nHOME_ASSISTANT.URL and HOME_ASSISTANT.TOKEN",
            )
            sys.exit(1)
        self.added_devices = self.config.get_config("HOME_ASSISTANT.DEVICES", [])
        self.current_devices = []
        self.domain_mapping = {}
        self.threads = []
        self.load_thread = None
        self.logger = logging.getLogger("HADeviceManager")
        self.setup_ui()
        self.apply_stylesheet()
        self.init_ui()
        self.connect_signals()
        self.load_devices("all")
        self.setWindowTitle("Home Assistant Device Manager")

    def setup_ui(self):
        """Set up the UI programmatically."""
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(12, 12, 12, 12)
        self.main_layout.setSpacing(6)

        # Navigation TabBar
        self.nav_segment = QTabBar()
        self.nav_segment.setMinimumSize(180, 42)
        self.main_layout.addWidget(self.nav_segment)

        # Stacked Widget for pages
        self.stackedWidget = QStackedWidget()
        self.main_layout.addWidget(self.stackedWidget)

        # Available Devices Page
        self.available_page = QWidget()
        self.available_layout = QVBoxLayout(self.available_page)
        self.available_layout.setContentsMargins(0, 0, 0, 0)
        self.available_card = QFrame()
        self.available_card.setObjectName("available_card")
        self.available_card_layout = QVBoxLayout(self.available_card)
        self.available_layout.addWidget(self.available_card)

        # Filter Layout
        self.filter_layout = QHBoxLayout()
        self.domain_combo = QComboBox()
        self.domain_combo.setMinimumSize(120, 32)
        self.domain_combo.setMaximumSize(16777215, 32)
        self.domain_combo.setPlaceholderText("Select Device Type")
        self.filter_layout.addWidget(self.domain_combo)
        self.search_input = QLineEdit()
        self.search_input.setMinimumSize(0, 32)
        self.search_input.setMaximumSize(16777215, 32)
        self.search_input.setPlaceholderText("Search Devices (Name or ID)")
        self.filter_layout.addWidget(self.search_input)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setMinimumSize(80, 32)
        self.refresh_button.setMaximumSize(16777215, 32)
        self.filter_layout.addWidget(self.refresh_button)
        self.available_card_layout.addLayout(self.filter_layout)

        # Device Table
        self.device_table = QTableWidget()
        self.device_table.setSelectionMode(QTableWidget.SingleSelection)
        self.device_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.device_table.setColumnCount(4)
        self.device_table.setHorizontalHeaderLabels(["Prompt", "Device ID", "Type", "Status"])
        self.available_card_layout.addWidget(self.device_table)

        # Add Device Layout
        self.add_layout = QHBoxLayout()
        self.custom_name_input = QLineEdit()
        self.custom_name_input.setMinimumSize(0, 32)
        self.custom_name_input.setMaximumSize(16777215, 32)
        self.custom_name_input.setPlaceholderText("Custom Prompt (Optional)")
        self.add_layout.addWidget(self.custom_name_input)
        self.add_button = QPushButton("Add Selected Device")
        self.add_button.setMinimumSize(100, 32)
        self.add_button.setMaximumSize(16777215, 32)
        self.add_layout.addWidget(self.add_button)
        self.available_card_layout.addLayout(self.add_layout)
        self.stackedWidget.addWidget(self.available_page)

        # Added Devices Page
        self.added_page = QWidget()
        self.added_layout = QVBoxLayout(self.added_page)
        self.added_layout.setContentsMargins(0, 0, 0, 0)
        self.added_card = QFrame()
        self.added_card.setObjectName("added_card")
        self.added_card_layout = QVBoxLayout(self.added_card)
        self.added_device_table = QTableWidget()
        self.added_device_table.setSelectionMode(QTableWidget.SingleSelection)
        self.added_device_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.added_device_table.setColumnCount(3)
        self.added_device_table.setHorizontalHeaderLabels(["Prompt", "Device ID", "Actions"])
        self.added_card_layout.addWidget(self.added_device_table)
        self.added_layout.addWidget(self.added_card)
        self.stackedWidget.addWidget(self.added_page)

    def apply_stylesheet(self):
        """Apply modern stylesheet for a sleek UI."""
        stylesheet = """
            QMainWindow {
                background-color: #f5f7fa;
            }
            QFrame#available_card, QFrame#added_card {
                background-color: #ffffff;
                border-radius: 12px;
                border: 1px solid #e0e4e8;
                padding: 10px;
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
            }
            QTabBar::tab {
                background: #e8ecef;
                border: 1px solid #d1d5db;
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                padding: 10px 20px;
                margin-right: 4px;
                color: #374151;
                font-weight: 500;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                border-color: #d1d5db;
                margin-bottom: -1px;
                color: #1f2937;
                font-weight: 600;
            }
            QTabBar::tab:!selected {
                margin-top: 2px;
            }
            QComboBox, QLineEdit, QPushButton {
                padding: 8px 12px;
                border: 1px solid #d1d5db;
                border-radius: 6px;
                min-height: 24px;
                font-size: 11pt;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QLineEdit, QComboBox {
                background-color: #ffffff;
                color: #1f2937;
            }
            QComboBox:hover, QLineEdit:hover {
                border-color: #3b82f6;
            }
            QPushButton {
                background-color: #3b82f6;
                color: #ffffff;
                font-weight: 600;
                min-width: 80px;
                transition: background-color 0.2s ease;
            }
            QPushButton:hover {
                background-color: #2563eb;
            }
            QPushButton:pressed {
                background-color: #1d4ed8;
            }
            QPushButton#delete_button {
                background-color: #ef4444;
            }
            QPushButton#delete_button:hover {
                background-color: #dc2626;
            }
            QComboBox::drop-down {
                border: none;
                padding-right: 10px;
            }
            QComboBox::down-arrow {
                image: url(:/qt-project.org/styles/commonstyle/images/standardbutton-down-arrow-16.png);
                width: 14px;
                height: 14px;
            }
            QTableWidget {
                border: 1px solid #e0e4e8;
                border-radius: 6px;
                gridline-color: #e5e7eb;
                selection-background-color: #bfdbfe;
                selection-color: #1f2937;
                alternate-background-color: #f9fafb;
                font-size: 11pt;
            }
            QHeaderView::section {
                background-color: #f3f4f6;
                padding: 6px;
                border: 1px solid #e0e4e8;
                border-bottom: none;
                font-weight: 600;
                font-size: 11pt;
                color: #1f2937;
            }
            QScrollBar:vertical {
                border: 1px solid #d1d5db;
                background: #f5f7fa;
                width: 12px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #9ca3af;
                min-height: 20px;
                border-radius: 6px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
                background: none;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
            QScrollBar:horizontal {
                border: 1px solid #d1d5db;
                background: #f5f7fa;
                height: 12px;
                margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: #9ca3af;
                min-width: 20px;
                border-radius: 6px;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0;
                background: none;
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: none;
            }
        """
        self.setStyleSheet(stylesheet)
        self.logger.info("Applied modern stylesheet")

    def init_ui(self):
        """Initialize UI components."""
        try:
            self.device_table.verticalHeader().setVisible(False)
            self.device_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            self.device_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            self.device_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
            self.device_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
            self.added_device_table.verticalHeader().setVisible(False)
            self.added_device_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            self.added_device_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            self.added_device_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
            self._setup_navigation()
            self.search_input.textChanged.connect(self.filter_devices)
            self.domain_combo.clear()
            self.domain_mapping = {"All": "all"}
            self.domain_combo.addItem("All")
            domains = [
                ("light", "Light 💡"),
                ("switch", "Switch 🔌"),
                ("sensor", "Sensor 🌡️"),
                ("binary_sensor", "Binary Sensor 🔔"),
                ("climate", "Climate ❄️"),
                ("fan", "Fan 💨"),
                ("cover", "Cover 🪟"),
                ("media_player", "Media Player 📺"),
            ]
            for domain_id, domain_name in domains:
                self.domain_mapping[domain_name] = domain_id
                self.domain_combo.addItem(domain_name)
            self.domain_combo.setCurrentIndex(0)
            self.domain_combo.currentTextChanged.connect(self.domain_changed)
            self.load_devices("all")
        except Exception as e:
            self.logger.error(f"Failed to initialize UI: {str(e)}")
            raise

    def _setup_navigation(self):
        """Set up navigation bar using QTabBar."""
        self.logger.info("Starting navigation bar setup (QTabBar)")
        try:
            while self.nav_segment.count() > 0:
                self.nav_segment.removeTab(0)
            self.nav_segment.addTab("Available Devices")
            self.nav_segment.addTab("Added Devices")
            self._nav_keys = ["available", "added"]
            self.nav_segment.currentChanged.connect(self.on_page_changed_by_index)
            self.nav_segment.setCurrentIndex(0)
            self.logger.info("Navigation bar setup complete, default selected index 0 ('Available Devices')")
        except Exception as e:
            self.logger.error(f"Failed to set up navigation bar: {e}")
            QMessageBox.warning(self, "Warning", f"Failed to set up navigation bar: {e}")

    def closeEvent(self, event):
        """Handle window close event."""
        self.stop_all_threads()
        super().closeEvent(event)

    def stop_all_threads(self):
        """Stop all threads."""
        if self.load_thread and self.load_thread.isRunning():
            self.logger.info("Stopping current loading thread...")
            try:
                self.load_thread.terminate()
                if not self.load_thread.wait(1000):
                    self.logger.warning("Loading thread failed to stop within 1 second")
            except Exception as e:
                self.logger.error(f"Error stopping loading thread: {e}")
        for thread in self.threads[:]:
            if thread and thread.isRunning():
                self.logger.info(f"Stopping thread: {thread}")
                try:
                    if hasattr(thread, "terminate"):
                        thread.terminate()
                    if not thread.wait(1000):
                        self.logger.warning(f"Thread failed to stop within 1 second: {thread}")
                except Exception as e:
                    self.logger.error(f"Error stopping thread: {e}")
        self.threads.clear()
        self.load_thread = None

    def connect_signals(self):
        """Connect signals and slots."""
        self.domain_combo.currentTextChanged.connect(self.domain_changed)
        self.search_input.textChanged.connect(self.filter_devices)
        self.refresh_button.clicked.connect(self.refresh_devices)
        self.add_button.clicked.connect(self.add_selected_device)
        self.added_device_table.cellChanged.connect(self.on_prompt_edited)
        self.device_table.cellChanged.connect(self.on_available_device_prompt_edited)

    def on_page_changed_by_index(self, index: int):
        """Handle QTabBar tab switch."""
        try:
            routeKey = self._nav_keys[index]
            self.logger.info(f"Switching to page index {index}, key: {routeKey}")
            if routeKey == "available":
                self.stackedWidget.setCurrentIndex(0)
            elif routeKey == "added":
                self.stackedWidget.setCurrentIndex(1)
                self.reload_config()
                self.refresh_added_devices()
            else:
                self.logger.warning(f"Unknown navigation index: {index}, key: {routeKey}")
        except IndexError:
            self.logger.error(f"Navigation index out of bounds: {index}")
        except Exception as e:
            self.logger.error(f"Page switch processing failed: {e}")

    def reload_config(self):
        """Reload configuration from disk."""
        try:
            config_path = os.path.join(project_root, "config", "config.json")
            if not os.path.exists(config_path):
                self.logger.warning(f"Configuration file does not exist: {config_path}")
                return
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            if "HOME_ASSISTANT" in config_data and "DEVICES" in config_data["HOME_ASSISTANT"]:
                self.added_devices = config_data["HOME_ASSISTANT"]["DEVICES"]
                self.logger.info(f"Reloaded {len(self.added_devices)} devices from configuration file")
            else:
                self.added_devices = []
                self.logger.warning("No device configuration found in configuration file")
        except Exception as e:
            self.logger.error(f"Failed to reload configuration file: {e}")
            QMessageBox.warning(self, "Warning", f"Failed to reload configuration file: {e}")

    def domain_changed(self):
        """Handle domain selection change."""
        current_text = self.domain_combo.currentText()
        domain = self.domain_mapping.get(current_text, "all")
        self.load_devices(domain)

    def load_devices(self, domain):
        """Load device list."""
        self.search_input.clear()
        self.device_table.setRowCount(0)
        loading_row = self.device_table.rowCount()
        self.device_table.insertRow(loading_row)
        loading_item = QTableWidgetItem("Loading devices...")
        loading_item.setTextAlignment(Qt.AlignCenter)
        self.device_table.setItem(loading_row, 0, loading_item)
        self.device_table.setSpan(loading_row, 0, 1, 4)
        if self.load_thread and self.load_thread.isRunning():
            self.logger.info("Waiting for previous loading thread to complete...")
            if not self.load_thread.wait(1000):
                self.logger.warning("Previous loading thread did not complete in 1 second, forcibly terminated")
                if self.load_thread in self.threads:
                    self.threads.remove(self.load_thread)
                self.load_thread = None
        self.load_thread = DeviceLoadThread(self.ha_url, self.ha_token, domain)
        self.load_thread.devices_loaded.connect(self.update_device_table)
        self.load_thread.error_occurred.connect(self.show_error)
        self.load_thread.start()
        self.threads.append(self.load_thread)

    def update_device_table(self, devices):
        """Update device table."""
        sender = self.sender()
        if sender in self.threads:
            self.threads.remove(sender)
        self.current_devices = devices
        self.device_table.setRowCount(0)
        if not devices:
            no_device_row = self.device_table.rowCount()
            self.device_table.insertRow(no_device_row)
            no_device_item = QTableWidgetItem("No devices found")
            no_device_item.setTextAlignment(Qt.AlignCenter)
            self.device_table.setItem(no_device_row, 0, no_device_item)
            self.device_table.setSpan(no_device_row, 0, 1, 4)
            return
        for device in devices:
            row = self.device_table.rowCount()
            self.device_table.insertRow(row)
            friendly_name_item = QTableWidgetItem(device["friendly_name"])
            self.device_table.setItem(row, 0, friendly_name_item)
            entity_id_item = QTableWidgetItem(device["entity_id"])
            entity_id_item.setFlags(entity_id_item.flags() & ~Qt.ItemIsEditable)
            self.device_table.setItem(row, 1, entity_id_item)
            domain = device["domain"]
            domain_display = DOMAIN_ICONS.get(domain, domain)
            domain_item = QTableWidgetItem(domain_display)
            domain_item.setFlags(domain_item.flags() & ~Qt.ItemIsEditable)
            self.device_table.setItem(row, 2, domain_item)
            state_item = QTableWidgetItem(device["state"])
            state_item.setFlags(state_item.flags() & ~Qt.ItemIsEditable)
            self.device_table.setItem(row, 3, state_item)
            if any(d.get("entity_id") == device["entity_id"] for d in self.added_devices):
                for col in range(4):
                    item = self.device_table.item(row, col)
                    if item:
                        item.setBackground(QColor("#e5e7eb"))

    def refresh_devices(self):
        """Refresh device list."""
        current_text = self.domain_combo.currentText()
        domain = self.domain_mapping.get(current_text, "all")
        self.load_devices(domain)

    def filter_devices(self):
        """Filter devices based on search input."""
        search_text = self.search_input.text().lower()
        for row in range(self.device_table.rowCount()):
            show_row = True
            if search_text:
                prompt = self.device_table.item(row, 0).text().lower()
                entity_id = self.device_table.item(row, 1).text().lower()
                show_row = search_text in prompt or search_text in entity_id
            self.device_table.setRowHidden(row, not show_row)

    def add_selected_device(self):
        """Add selected device."""
        selected_indexes = self.device_table.selectedIndexes()
        if not selected_indexes:
            QMessageBox.warning(self, "Warning", "Please select a device first")
            return
        row = selected_indexes[0].row()
        if row < 0 or row >= self.device_table.rowCount():
            self.logger.warning(f"Invalid selected row: {row}")
            return
        if self.device_table.item(row, 1) is None:
            self.logger.warning(f"Selected row is not a valid device row: {row}")
            QMessageBox.warning(self, "Warning", "Please select a valid device row")
            return
        entity_id = self.device_table.item(row, 1).text()
        if any(d.get("entity_id") == entity_id for d in self.added_devices):
            QMessageBox.information(self, "Info", f"Device {entity_id} has already been added")
            return
        friendly_name = self.custom_name_input.text().strip() or self.device_table.item(row, 0).text()
        self.save_device_to_config(entity_id, friendly_name)
        added_tab_index = self._nav_keys.index("added")
        if added_tab_index is not None:
            self.nav_segment.setCurrentIndex(added_tab_index)
        else:
            self.reload_config()
            self.refresh_added_devices()
        self.refresh_devices()
        self.custom_name_input.clear()

    def refresh_added_devices(self):
        """Refresh added devices table."""
        try:
            self.added_device_table.cellChanged.disconnect(self.on_prompt_edited)
        except Exception:
            self.logger.warning("Error disconnecting cellChanged signal")
        self.added_device_table.setRowCount(0)
        if not self.added_devices:
            empty_row = self.added_device_table.rowCount()
            self.added_device_table.insertRow(empty_row)
            empty_item = QTableWidgetItem("No devices added")
            empty_item.setTextAlignment(Qt.AlignCenter)
            self.added_device_table.setItem(empty_row, 0, empty_item)
            self.added_device_table.setSpan(empty_row, 0, 1, 3)
            self.added_device_table.cellChanged.connect(self.on_prompt_edited)
            return
        for device in self.added_devices:
            row = self.added_device_table.rowCount()
            self.added_device_table.insertRow(row)
            friendly_name = device.get("friendly_name", "")
            friendly_name_item = QTableWidgetItem(friendly_name)
            self.added_device_table.setItem(row, 0, friendly_name_item)
            entity_id = device.get("entity_id", "")
            entity_id_item = QTableWidgetItem(entity_id)
            entity_id_item.setFlags(entity_id_item.flags() & ~Qt.ItemIsEditable)
            self.added_device_table.setItem(row, 1, entity_id_item)
            delete_button = QPushButton("Delete")
            delete_button.setObjectName("delete_button")
            delete_button.clicked.connect(lambda checked, r=row: self.delete_device(r))
            self.added_device_table.setCellWidget(row, 2, delete_button)
        self.added_device_table.cellChanged.connect(self.on_prompt_edited)

    def delete_device(self, row):
        """Delete device at specified row."""
        entity_id = self.added_device_table.item(row, 1).text()
        reply = QMessageBox.question(
            self,
            "Confirm Deletion",
            f"Are you sure you want to delete device {entity_id}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            success = self.delete_device_from_config(entity_id)
            if success:
                self.reload_config()
                self.refresh_added_devices()
                self.refresh_devices()

    def save_device_to_config(self, entity_id: str, friendly_name: Optional[str] = None) -> bool:
        """Save device to configuration file."""
        try:
            config_path = os.path.join(project_root, "config", "config.json")
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if "HOME_ASSISTANT" not in config:
                config["HOME_ASSISTANT"] = {}
            if "DEVICES" not in config["HOME_ASSISTANT"]:
                config["HOME_ASSISTANT"]["DEVICES"] = []
            for device in config["HOME_ASSISTANT"]["DEVICES"]:
                if device.get("entity_id") == entity_id:
                    if friendly_name and device.get("friendly_name") != friendly_name:
                        device["friendly_name"] = friendly_name
                        with open(config_path, "w", encoding="utf-8") as f:
                            json.dump(config, f, ensure_ascii=False, indent=2)
                        QMessageBox.information(
                            self,
                            "Update Successful",
                            f"Prompt for device {entity_id} updated to: {friendly_name}",
                        )
                    else:
                        QMessageBox.information(
                            self,
                            "Info",
                            f"Device {entity_id} already exists in the configuration",
                        )
                    return True
            new_device = {"entity_id": entity_id}
            if friendly_name:
                new_device["friendly_name"] = friendly_name
            config["HOME_ASSISTANT"]["DEVICES"].append(new_device)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            QMessageBox.information(
                self,
                "Addition Successful",
                f"Successfully added device: {entity_id}" + (f" (Prompt: {friendly_name})" if friendly_name else ""),
            )
            return True
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save configuration: {e}")
            return False

    def delete_device_from_config(self, entity_id: str) -> bool:
        """Delete device from configuration file."""
        try:
            config_path = os.path.join(project_root, "config", "config.json")
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if "HOME_ASSISTANT" not in config or "DEVICES" not in config["HOME_ASSISTANT"]:
                QMessageBox.warning(self, "Warning", "No Home Assistant devices exist in the configuration")
                return False
            devices = config["HOME_ASSISTANT"]["DEVICES"]
            initial_count = len(devices)
            config["HOME_ASSISTANT"]["DEVICES"] = [device for device in devices if device.get("entity_id") != entity_id]
            if len(config["HOME_ASSISTANT"]["DEVICES"]) < initial_count:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
                QMessageBox.information(self, "Deletion Successful", f"Successfully deleted device: {entity_id}")
                return True
            else:
                QMessageBox.warning(self, "Warning", f"Device not found: {entity_id}")
                return False
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete device: {e}")
            return False

    def show_error(self, error_message):
        """Display error message."""
        sender = self.sender()
        if sender in self.threads:
            self.threads.remove(sender)
        self.device_table.setRowCount(0)
        error_row = self.device_table.rowCount()
        self.device_table.insertRow(error_row)
        error_item = QTableWidgetItem(f"Loading failed: {error_message}")
        error_item.setTextAlignment(Qt.AlignCenter)
        self.device_table.setItem(error_row, 0, error_item)
        self.device_table.setSpan(error_row, 0, 1, 4)
        QMessageBox.critical(self, "Error", f"Failed to load devices: {error_message}")

    def on_prompt_edited(self, row, column):
        """Handle prompt edit in added devices table."""
        if column != 0:
            return
        entity_id = self.added_device_table.item(row, 1).text()
        new_prompt = self.added_device_table.item(row, 0).text()
        self.save_device_to_config(entity_id, new_prompt)

    def on_available_device_prompt_edited(self, row, column):
        """Handle prompt edit in available devices table."""
        if column != 0:
            return
        new_prompt = self.device_table.item(row, 0).text()
        if row in [index.row() for index in self.device_table.selectedIndexes()]:
            self.custom_name_input.setText(new_prompt)
            self.logger.info(f"Updated custom name input box: {new_prompt}")

def main():
    """Main function."""
    app = SweepApplication(sys.argv)
    window = HomeAssistantDeviceManager()
    window.setMinimumSize(800, 480)
    window.resize(800, 480)
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
