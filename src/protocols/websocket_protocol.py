# socket.py
# -*- coding: utf-8 -*-
"""
WebSocket Protocol Implementation
Handles WebSocket connections for audio and text communication.
"""
import asyncio
import json
import ssl

import websockets

from src.constants.constants import AudioConfig
from src.protocols.protocol import Protocol
from src.utils.config_manager import ConfigManager
from src.utils.logging_config import get_logger

# Create an unverified SSL context for WSS connections
ssl_context = ssl._create_unverified_context()

# Initialize logger
logger = get_logger(__name__)

class WebsocketProtocol(Protocol):
    def __init__(self):
        super().__init__()
        # Get configuration manager instance
        self.config = ConfigManager.get_instance()
        self.websocket = None
        self.connected = False
        self.hello_received = None  # Initialize as None
        self.WEBSOCKET_URL = self.config.get_config(
            "SYSTEM_OPTIONS.NETWORK.WEBSOCKET_URL"
        )
        access_token = self.config.get_config(
            "SYSTEM_OPTIONS.NETWORK.WEBSOCKET_ACCESS_TOKEN"
        )
        device_id = self.config.get_config("SYSTEM_OPTIONS.DEVICE_ID")
        client_id = self.config.get_config("SYSTEM_OPTIONS.CLIENT_ID")

        self.HEADERS = {
            "Authorization": f"Bearer {access_token}",
            "Protocol-Version": "1",
            "Device-Id": device_id,  # Device MAC address
            "Client-Id": client_id,
        }

    async def connect(self) -> bool:
        """Connect to the WebSocket server."""
        try:
            # Create Event in the correct event loop during connection
            self.hello_received = asyncio.Event()

            # Determine if SSL should be used
            current_ssl_context = None
            if self.WEBSOCKET_URL.startswith("wss://"):
                current_ssl_context = ssl_context

            # Establish WebSocket connection (compatible with different Python versions)
            try:
                # New syntax (Python 3.11+)
                self.websocket = await websockets.connect(
                    uri=self.WEBSOCKET_URL,
                    ssl=current_ssl_context,
                    additional_headers=self.HEADERS,
                )
            except TypeError:
                # Legacy syntax (earlier Python versions)
                self.websocket = await websockets.connect(
                    self.WEBSOCKET_URL,
                    ssl=current_ssl_context,
                    extra_headers=self.HEADERS,
                )

            # Start message handling loop
            asyncio.create_task(self._message_handler())

            # Send client hello message
            hello_message = {
                "type": "hello",
                "version": 1,
                "transport": "websocket",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": AudioConfig.INPUT_SAMPLE_RATE,
                    "channels": AudioConfig.CHANNELS,
                    "frame_duration": AudioConfig.FRAME_DURATION,
                },
            }
            await self.send_text(json.dumps(hello_message))

            # Wait for server hello response
            try:
                await asyncio.wait_for(self.hello_received.wait(), timeout=10.0)
                self.connected = True
                logger.info("Connected to WebSocket server")
                return True
            except asyncio.TimeoutError:
                logger.error("Timeout waiting for server hello response")
                if self.on_network_error:
                    self.on_network_error("Response timeout")
                return False

        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            if self.on_network_error:
                self.on_network_error(f"Unable to connect to server: {str(e)}")
            return False

    async def _message_handler(self):
        """Handle incoming WebSocket messages."""
        try:
            async for message in self.websocket:
                if isinstance(message, str):
                    try:
                        data = json.loads(message)
                        msg_type = data.get("type")
                        if msg_type == "hello":
                            # Handle server hello message
                            await self._handle_server_hello(data)
                        else:
                            if self.on_incoming_json:
                                self.on_incoming_json(data)
                    except json.JSONDecodeError as e:
                        logger.error(f"Invalid JSON message: {message}, error: {e}")
                elif self.on_incoming_audio:  # Handle binary audio data
                    self.on_incoming_audio(message)

        except websockets.ConnectionClosed:
            logger.info("WebSocket connection closed")
            self.connected = False
            if self.on_audio_channel_closed:
                # Ensure callback runs in the main thread
                await self.on_audio_channel_closed()
        except Exception as e:
            logger.error(f"Message handling error: {e}")
            self.connected = False
            if self.on_network_error:
                # Ensure error handling runs in the main thread
                self.on_network_error(f"Connection error: {str(e)}")

    async def send_audio(self, data: bytes):
        """Send audio data."""
        if not self.is_audio_channel_opened():
            return

        try:
            await self.websocket.send(data)
        except Exception as e:
            if self.on_network_error:
                self.on_network_error(f"Failed to send audio data: {str(e)}")

    async def send_text(self, message: str):
        """Send text message."""
        if self.websocket:
            try:
                await self.websocket.send(message)
            except Exception as e:
                logger.error(f"Failed to send text message: {e}")
                await self.close_audio_channel()
                if self.on_network_error:
                    self.on_network_error("Client closed")

    def is_audio_channel_opened(self) -> bool:
        """Check if the audio channel is open."""
        return self.websocket is not None and self.connected

    async def open_audio_channel(self) -> bool:
        """Establish WebSocket connection.

        If not connected, create a new WebSocket connection.
        Returns:
            bool: Whether the connection was successful
        """
        if not self.connected:
            return await self.connect()
        return True

    async def _handle_server_hello(self, data: dict):
        """Handle server's hello message."""
        try:
            # Verify transport method
            transport = data.get("transport")
            if not transport or transport != "websocket":
                logger.error(f"Unsupported transport method: {transport}")
                return
            print("Server connection returned initial configuration", data)

            # Set hello received event
            self.hello_received.set()

            # Notify that audio channel is opened
            if self.on_audio_channel_opened:
                await self.on_audio_channel_opened()

            logger.info("Successfully processed server hello message")

        except Exception as e:
            logger.error(f"Error processing server hello message: {e}")
            if self.on_network_error:
                self.on_network_error(f"Failed to process server response: {str(e)}")

    async def close_audio_channel(self):
        """Close the audio channel."""
        if self.websocket:
            try:
                await self.websocket.close()
                self.websocket = None
                self.connected = False
                if self.on_audio_channel_closed:
                    await self.on_audio_channel_closed()
            except Exception as e:
                logger.error(f"Failed to close WebSocket connection: {e}")
