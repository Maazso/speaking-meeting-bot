"""WebSocket routes for the Speaking Meeting Bot API."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.connection import MEETING_DETAILS, PIPECAT_PROCESSES, registry
from core.process import start_pipecat_process, terminate_process_gracefully
from core.router import router as message_router
from meetingbaas_pipecat.utils.logger import logger
from utils.ngrok import LOCAL_DEV_MODE, log_ngrok_status, release_ngrok_url

websocket_router = APIRouter()


@websocket_router.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """Handle WebSocket connections from clients."""
    await registry.connect(websocket, client_id)
    logger.info(f"Client {client_id} connected")

    try:
        # Get meeting details from our in-memory storage
        if client_id not in MEETING_DETAILS:
            logger.error(f"No meeting details found for client {client_id}")
            await websocket.close(code=1008, reason="Missing meeting details")
            return

        # Get stored meeting details with fallbacks to ensure compatibility
        meeting_details = MEETING_DETAILS[client_id]
        meeting_url = meeting_details[0] if len(meeting_details) > 0 else None
        persona_name = meeting_details[1] if len(meeting_details) > 1 else None
        meetingbaas_bot_id = meeting_details[2] if len(meeting_details) > 2 else None
        enable_tools = meeting_details[3] if len(meeting_details) > 3 else False

        # Default to 24khz if not specified
        streaming_audio_frequency = (
            meeting_details[4] if len(meeting_details) > 4 else "24khz"
        )

        # Default to False if recorder_only not specified
        recorder_only = meeting_details[5] if len(meeting_details) > 5 else False

        logger.info(
            f"Retrieved meeting details for {client_id}: {meeting_url}, {persona_name}, {meetingbaas_bot_id}, {enable_tools}, {streaming_audio_frequency}, recorder_only={recorder_only}"
        )

        # Check if a Pipecat process is already running for this client
        if (
            client_id in PIPECAT_PROCESSES
            and PIPECAT_PROCESSES[client_id].poll() is None
        ):
            logger.info(f"Pipecat process already running for client {client_id}")
        else:
            # Start Pipecat process if not already running
            pipecat_websocket_url = f"ws://localhost:8766/pipecat/{client_id}"
            process = start_pipecat_process(
                client_id=client_id,
                websocket_url=pipecat_websocket_url,
                meeting_url=meeting_url,
                persona_name=persona_name,
                streaming_audio_frequency=streaming_audio_frequency,
                enable_tools=enable_tools,
                recorder_only=recorder_only,
            )

            # Store the process for cleanup
            PIPECAT_PROCESSES[client_id] = process

        # Process messages
        while True:
            message = await websocket.receive()
            if "bytes" in message:
                audio_data = message["bytes"]
                logger.debug(
                    f"Received audio data ({len(audio_data)} bytes) from client {client_id}"
                )
                await message_router.send_to_pipecat(audio_data, client_id)
            elif "text" in message:
                text_data = message["text"]
                logger.info(
                    f"Received text message from client {client_id}: {text_data[:100]}..."
                )
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for client {client_id}")
    except Exception as e:
        logger.error(f"Error in WebSocket connection: {e}")
    finally:
        # Clean up
        if client_id in PIPECAT_PROCESSES:
            process = PIPECAT_PROCESSES[client_id]
            if process and process.poll() is None:  # If process is still running
                try:
                    if terminate_process_gracefully(process, timeout=3.0):
                        logger.info(
                            f"Gracefully terminated Pipecat process for client {client_id}"
                        )
                    else:
                        logger.warning(
                            f"Had to forcefully kill Pipecat process for client {client_id}"
                        )
                except Exception as e:
                    logger.error(f"Error terminating process: {e}")
            # Remove from our storage
            PIPECAT_PROCESSES.pop(client_id, None)

        if client_id in MEETING_DETAILS:
            MEETING_DETAILS.pop(client_id, None)

        # Mark client as closing to prevent further message sending
        message_router.mark_closing(client_id)

        # Gracefully disconnect - wrapping in try/except to handle already closed connections
        try:
            await registry.disconnect(client_id)
            logger.info(f"Client {client_id} disconnected")
        except Exception as e:
            # Only log at debug level since this is expected during abrupt disconnections
            logger.debug(f"Error disconnecting client {client_id}: {e}")

        # Release ngrok URL
        if LOCAL_DEV_MODE:
            release_ngrok_url(client_id)
            log_ngrok_status()


@websocket_router.websocket("/pipecat/{client_id}")
async def pipecat_websocket(websocket: WebSocket, client_id: str):
    """Handle WebSocket connections from Pipecat."""
    await registry.connect(websocket, client_id, is_pipecat=True)
    try:
        while True:
            message = await websocket.receive()
            if "bytes" in message:
                data = message["bytes"]
                logger.debug(
                    f"Received binary data ({len(data)} bytes) from Pipecat client {client_id}"
                )
                # Forward Pipecat messages to client with conversion
                await message_router.send_from_pipecat(data, client_id)
            elif "text" in message:
                data = message["text"]
                logger.info(
                    f"Received text message from Pipecat client {client_id}: {data[:100]}..."
                )
    except WebSocketDisconnect:
        logger.info(f"Pipecat WebSocket disconnected for client {client_id}")
    except Exception as e:
        logger.error(
            f"Error in Pipecat WebSocket handler for client {client_id}: {str(e)}"
        )
    finally:
        # Mark client as closing before disconnecting
        message_router.mark_closing(client_id)

        try:
            await registry.disconnect(client_id, is_pipecat=True)
            logger.info(f"Pipecat client {client_id} disconnected")
        except Exception as e:
            # Log at debug level since this can happen during normal shutdown
            logger.debug(f"Error disconnecting Pipecat client {client_id}: {e}")

        # Release ngrok URL
        if LOCAL_DEV_MODE:
            release_ngrok_url(client_id)
            log_ngrok_status()
