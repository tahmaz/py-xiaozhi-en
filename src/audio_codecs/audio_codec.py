import queue
import threading
import time

import numpy as np
import opuslib
import pyaudio

from src.constants.constants import AudioConfig
from src.utils.logging_config import get_logger

logger = get_logger(__name__)


class AudioCodec:
    """Audio codec class for handling audio recording and playback (strict compatibility version)"""

    def __init__(self):
        self.audio = None
        self.input_stream = None
        self.output_stream = None
        self.opus_encoder = None
        self.opus_decoder = None
        # Set maximum queue size to prevent memory overflow (approximately 10 seconds of audio buffer)
        max_queue_size = int(10 * 1000 / AudioConfig.FRAME_DURATION)
        self.audio_decode_queue = queue.Queue(maxsize=max_queue_size)

        # State management (retaining original variable names)
        self._is_closing = False
        self._is_input_paused = False
        self._input_paused_lock = threading.Lock()
        self._stream_lock = threading.Lock()

        # Device index cache removed (not used)

        self._initialize_audio()

    def _initialize_audio(self):
        try:
            self.audio = pyaudio.PyAudio()

            # Initialize streams (optimized implementation)
            self.input_stream = self._create_stream(is_input=True)
            self.output_stream = self._create_stream(is_input=False)

            # Codec initialization (retaining original parameters)
            self.opus_encoder = opuslib.Encoder(
                AudioConfig.INPUT_SAMPLE_RATE,
                AudioConfig.CHANNELS,
                opuslib.APPLICATION_AUDIO,
            )
            self.opus_decoder = opuslib.Decoder(
                AudioConfig.OUTPUT_SAMPLE_RATE, AudioConfig.CHANNELS
            )

            logger.info("Audio device and codec initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize audio device: {e}")
            self.close()
            raise

    def _create_stream(self, is_input=True):
        """Stream creation logic."""
        params = {
            "format": pyaudio.paInt16,
            "channels": AudioConfig.CHANNELS,
            "rate": (
                AudioConfig.INPUT_SAMPLE_RATE
                if is_input
                else AudioConfig.OUTPUT_SAMPLE_RATE
            ),
            "input" if is_input else "output": True,
            "frames_per_buffer": (
                AudioConfig.INPUT_FRAME_SIZE
                if is_input
                else AudioConfig.OUTPUT_FRAME_SIZE
            ),
            "start": False,
        }

        return self.audio.open(**params)

    def _reinitialize_stream(self, is_input=True):
        """General stream reinitialization method."""
        if self._is_closing:
            return False if is_input else None

        try:
            stream_attr = "input_stream" if is_input else "output_stream"
            current_stream = getattr(self, stream_attr)

            if current_stream:
                try:
                    current_stream.stop_stream()
                    current_stream.close()
                except Exception:
                    pass

            new_stream = self._create_stream(is_input=is_input)
            setattr(self, stream_attr, new_stream)
            new_stream.start_stream()

            stream_type = "input" if is_input else "output"
            logger.info(f"Audio {stream_type} stream reinitialized successfully")
            return True if is_input else None
        except Exception as e:
            stream_type = "input" if is_input else "output"
            logger.error(f"Failed to reinitialize {stream_type} stream: {e}")
            if is_input:
                return False
            else:
                raise

    def pause_input(self):
        with self._input_paused_lock:
            self._is_input_paused = True
        logger.info("Audio input paused")

    def resume_input(self):
        with self._input_paused_lock:
            self._is_input_paused = False
        logger.info("Audio input resumed")

    def is_input_paused(self):
        with self._input_paused_lock:
            return self._is_input_paused

    def read_audio(self):
        """(Optimized buffer management)"""
        if self.is_input_paused():
            return None

        try:
            with self._stream_lock:
                # Stream status check optimization
                if not self.input_stream or not self.input_stream.is_active():
                    if not self._reinitialize_stream(is_input=True):
                        return None

                # Dynamic buffer adjustment - real-time performance optimization
                available = self.input_stream.get_read_available()
                if available > AudioConfig.INPUT_FRAME_SIZE * 2:  # Lower threshold from 3x to 2x
                    skip_samples = available - (
                        AudioConfig.INPUT_FRAME_SIZE * 1.5
                    )  # Reduce retention amount
                    if skip_samples > 0:  # Add safety check
                        self.input_stream.read(
                            int(skip_samples), exception_on_overflow=False  # Ensure integer
                        )
                        logger.debug(f"Skipped {skip_samples} samples to reduce latency")

                # Read data
                data = self.input_stream.read(
                    AudioConfig.INPUT_FRAME_SIZE, exception_on_overflow=False
                )

                # Data validation
                if len(data) != AudioConfig.INPUT_FRAME_SIZE * 2:
                    logger.warning("Abnormal audio data length, resetting input stream")
                    self._reinitialize_stream(is_input=True)
                    return None

                return self.opus_encoder.encode(data, AudioConfig.INPUT_FRAME_SIZE)

        except Exception as e:
            logger.error(f"Failed to read audio: {e}")
            self._reinitialize_stream(is_input=True)
            return None

    def play_audio(self):
        """Play audio (simplified version, discard on decode failure)"""
        try:
            if self.audio_decode_queue.empty():
                return

            # Process audio data one by one, discard on failure
            processed_count = 0
            max_process_per_call = 5  # Limit the number of frames processed per call to avoid blocking

            while (
                not self.audio_decode_queue.empty()
                and processed_count < max_process_per_call
            ):
                try:
                    opus_data = self.audio_decode_queue.get_nowait()

                    # Decode audio data, discard on failure
                    try:
                        pcm = self.opus_decoder.decode(
                            opus_data, AudioConfig.OUTPUT_FRAME_SIZE
                        )
                    except opuslib.OpusError as e:
                        logger.warning(f"Audio decoding failed, discarding frame: {e}")
                        processed_count += 1
                        continue

                    # Play audio data, discard on failure
                    try:
                        with self._stream_lock:
                            if self.output_stream and self.output_stream.is_active():
                                self.output_stream.write(
                                    np.frombuffer(pcm, dtype=np.int16).tobytes()
                                )
                            else:
                                logger.warning("Output stream not active, discarding frame")
                    except OSError as e:
                        logger.warning(f"Audio playback failed, discarding frame: {e}")
                        if "Stream closed" in str(e):
                            self._reinitialize_stream(is_input=False)

                    processed_count += 1

                except queue.Empty:
                    break

        except Exception as e:
            logger.error(f"Unexpected error during audio playback: {e}")

    def close(self):
        """(Optimized resource release order and thread safety)"""
        if self._is_closing:
            return

        self._is_closing = True
        logger.info("Starting to close audio codec...")

        try:
            # Clear queue first
            self.clear_audio_queue()

            # Safely stop and close streams
            with self._stream_lock:
                # Close input stream first
                if self.input_stream:
                    try:
                        if (
                            hasattr(self.input_stream, "is_active")
                            and self.input_stream.is_active()
                        ):
                            self.input_stream.stop_stream()
                        self.input_stream.close()
                    except Exception as e:
                        logger.warning(f"Failed to close input stream: {e}")
                    finally:
                        self.input_stream = None

                # Then close output stream
                if self.output_stream:
                    try:
                        if (
                            hasattr(self.output_stream, "is_active")
                            and self.output_stream.is_active()
                        ):
                            self.output_stream.stop_stream()
                        self.output_stream.close()
                    except Exception as e:
                        logger.warning(f"Failed to close output stream: {e}")
                    finally:
                        self.output_stream = None

                # Finally release PyAudio
                if self.audio:
                    try:
                        self.audio.terminate()
                    except Exception as e:
                        logger.warning(f"Failed to release PyAudio: {e}")
                    finally:
                        self.audio = None

            # Clean up codecs
            self.opus_encoder = None
            self.opus_decoder = None

            logger.info("Audio resources fully released")
        except Exception as e:
            logger.error(f"Error occurred during audio codec shutdown: {e}")
        # Removed redundant state reset

    def write_audio(self, opus_data):
        """Write Opus data to playback queue, handling queue full scenarios."""
        try:
            # Non-blocking queue insertion
            self.audio_decode_queue.put_nowait(opus_data)
        except queue.Full:
            # If queue is full, remove oldest data and add new data
            logger.warning("Audio playback queue is full, discarding oldest audio frame")
            try:
                self.audio_decode_queue.get_nowait()  # Remove oldest
                self.audio_decode_queue.put_nowait(opus_data)  # Add new
            except queue.Empty:
                # If queue suddenly becomes empty, add directly
                self.audio_decode_queue.put_nowait(opus_data)

    # has_pending_audio method removed (can directly use not audio_decode_queue.empty())

    def get_queue_status(self):
        """Get queue status information (simplified version)"""
        queue_size = self.audio_decode_queue.qsize()
        max_size = self.audio_decode_queue.maxsize
        return {
            "current_size": queue_size,
            "max_size": max_size,
            "is_empty": queue_size == 0,
        }

    def wait_for_audio_complete(self, timeout=5.0):
        """Wait for audio playback to complete (simplified version)"""
        start = time.time()
        while not self.audio_decode_queue.empty() and time.time() - start < timeout:
            time.sleep(0.1)

        if not self.audio_decode_queue.empty():
            remaining = self.audio_decode_queue.qsize()
            logger.warning(f"Audio playback timed out, remaining queue: {remaining} frames")

    def clear_audio_queue(self):
        with self._stream_lock:
            cleared_count = 0
            while not self.audio_decode_queue.empty():
                try:
                    self.audio_decode_queue.get_nowait()
                    cleared_count += 1
                except queue.Empty:
                    break
            if cleared_count > 0:
                logger.info(f"Cleared audio queue, discarded {cleared_count} audio frames")

    # start_streams method removed (redundant functionality, can directly call each stream's start_stream)

    def stop_streams(self):
        """Safely stop streams (optimized error handling)"""
        with self._stream_lock:
            for name, stream in [
                ("input", self.input_stream),
                ("output", self.output_stream),
            ]:
                if stream:
                    try:
                        # Use hasattr to avoid calling is_active on closed streams
                        if hasattr(stream, "is_active") and stream.is_active():
                            stream.stop_stream()
                    except Exception as e:
                        # Use warning level since this is not a critical error
                        logger.warning(f"Failed to stop {name} stream: {e}")

    def __del__(self):
        self.close()
