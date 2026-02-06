#!/usr/bin/env python3
"""
Web Server for CFOperator
==========================

Serves the chat UI and provides WebSocket + HTTP APIs for:
- Chat with agent (infrastructure Q&A)
- Pending questions (bidirectional Q&A during investigations)
- System status

Runs in separate thread alongside OODA loop.
"""

import json
import logging
import threading
from typing import Dict, Any, Optional
from flask import Flask, request, jsonify, send_from_directory
from flask_sock import Sock
import time

logger = logging.getLogger("cfoperator.web")

class WebServer:
    """
    Web server for CFOperator UI and APIs.

    Provides:
    - Static file serving (UI)
    - WebSocket endpoint for real-time chat
    - HTTP APIs for chat and Q&A
    """

    def __init__(self, operator, host: str = "0.0.0.0", port: int = 8083):
        self.operator = operator
        self.host = host
        self.port = port

        # Flask app
        self.app = Flask(__name__, static_folder='ui', static_url_path='')
        self.sock = Sock(self.app)

        # WebSocket clients
        self.ws_clients = []

        # Setup routes
        self._setup_routes()

        logger.info(f"Web server initialized on {host}:{port}")

    def _setup_routes(self):
        """Setup Flask routes and WebSocket handlers."""

        # Static UI
        @self.app.route('/')
        def index():
            return send_from_directory('ui', 'index.html')

        # Health check
        @self.app.route('/api/health')
        def health():
            return jsonify({
                'status': 'ok',
                'version': '1.0.0',
                'current_investigation': self.operator.current_investigation is not None,
                'uptime_seconds': time.time() - self.operator.start_time if hasattr(self.operator, 'start_time') else 0
            })

        # Chat API (HTTP)
        @self.app.route('/api/chat', methods=['POST'])
        def api_chat():
            """
            Handle chat message from user.

            Request:
                {
                    "message": "Why did immich restart?",
                    "history": [...],
                    "backend": "auto|ollama|groq|gemini|anthropic"
                }

            Response:
                {
                    "response": "...",
                    "backend": "ollama",
                    "model": "qwen3:14b",
                    "tool_calls": 2
                }
            """
            data = request.json
            message = data.get('message', '')
            history = data.get('history', [])
            backend = data.get('backend', 'auto')

            if not message:
                return jsonify({'error': 'No message provided'}), 400

            try:
                # Delegate to operator's chat handler
                result = self.operator.handle_chat_message(message, history, backend)
                return jsonify(result)
            except Exception as e:
                logger.error(f"Error handling chat: {e}", exc_info=True)
                return jsonify({'error': str(e)}), 500

        # Q&A API (HTTP)
        @self.app.route('/api/qa', methods=['GET', 'POST'])
        def api_qa():
            """
            Get pending questions or submit answers.

            GET: List all pending questions
            POST: Submit answer to a question
            """
            if request.method == 'GET':
                # Get pending questions
                try:
                    questions = self.operator.kb.get_pending_questions()
                    return jsonify(questions)
                except Exception as e:
                    logger.error(f"Error fetching questions: {e}")
                    return jsonify({'error': str(e)}), 500
            else:
                # POST: Answer a question
                data = request.json
                question_id = data.get('question_id')
                answer = data.get('answer')

                if not question_id or not answer:
                    return jsonify({'error': 'Missing question_id or answer'}), 400

                try:
                    self.operator.answer_question(question_id, answer)
                    return jsonify({'success': True})
                except Exception as e:
                    logger.error(f"Error answering question: {e}")
                    return jsonify({'error': str(e)}), 500

        # WebSocket endpoint
        @self.sock.route('/ws')
        def websocket(ws):
            """
            WebSocket handler for real-time chat.

            Messages from client:
                {"type": "chat", "message": "...", "history": [...], "backend": "auto"}
                {"type": "answer", "question_id": 123, "answer": "..."}

            Messages to client:
                {"type": "chat", "text": "...", "backend": "ollama", "model": "qwen3:14b"}
                {"type": "question", "id": 123, "question": "...", "context": "..."}
                {"type": "tool_call", "tool_name": "prometheus_query", "input": {...}}
                {"type": "tool_result", "tool_name": "prometheus_query", "result": {...}}
            """
            logger.info("WebSocket client connected")
            self.ws_clients.append(ws)

            try:
                while True:
                    message = ws.receive()
                    if message is None:
                        break

                    try:
                        data = json.loads(message)
                        self._handle_ws_message(ws, data)
                    except json.JSONDecodeError:
                        ws.send(json.dumps({'error': 'Invalid JSON'}))
                    except Exception as e:
                        logger.error(f"Error handling WS message: {e}", exc_info=True)
                        ws.send(json.dumps({'error': str(e)}))
            finally:
                logger.info("WebSocket client disconnected")
                self.ws_clients.remove(ws)

    def _handle_ws_message(self, ws, data: Dict[str, Any]):
        """Handle incoming WebSocket message."""
        msg_type = data.get('type')

        if msg_type == 'chat':
            # User sent chat message
            message = data.get('message', '')
            history = data.get('history', [])
            backend = data.get('backend', 'auto')

            # Handle in background to not block WebSocket
            def handle_chat():
                try:
                    result = self.operator.handle_chat_message(message, history, backend)
                    ws.send(json.dumps({
                        'type': 'chat',
                        'text': result.get('response', ''),
                        'backend': result.get('backend', ''),
                        'model': result.get('model', '')
                    }))
                except Exception as e:
                    logger.error(f"Error in chat handler: {e}", exc_info=True)
                    ws.send(json.dumps({'type': 'error', 'error': str(e)}))

            thread = threading.Thread(target=handle_chat, daemon=True)
            thread.start()

        elif msg_type == 'answer':
            # User answered a question
            question_id = data.get('question_id')
            answer = data.get('answer')

            try:
                self.operator.answer_question(question_id, answer)
                ws.send(json.dumps({'type': 'ack', 'question_id': question_id}))
            except Exception as e:
                ws.send(json.dumps({'type': 'error', 'error': str(e)}))

    def broadcast(self, message: Dict[str, Any]):
        """Broadcast message to all connected WebSocket clients."""
        msg_json = json.dumps(message)
        for ws in self.ws_clients:
            try:
                ws.send(msg_json)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")

    def broadcast_question(self, question_id: int, question: str, context: str = '', investigation_id: Optional[int] = None):
        """Broadcast a pending question to all clients."""
        self.broadcast({
            'type': 'question',
            'id': question_id,
            'question': question,
            'context': context,
            'investigation_id': investigation_id
        })

    def broadcast_tool_call(self, tool_name: str, tool_input: Dict[str, Any]):
        """Broadcast tool execution to all clients."""
        self.broadcast({
            'type': 'tool_call',
            'tool_name': tool_name,
            'input': tool_input
        })

    def broadcast_tool_result(self, tool_name: str, result: Any):
        """Broadcast tool result to all clients."""
        self.broadcast({
            'type': 'tool_result',
            'tool_name': tool_name,
            'result': result
        })

    def run(self):
        """Start the Flask web server (blocking)."""
        logger.info(f"Starting web server on {self.host}:{self.port}")
        self.app.run(host=self.host, port=self.port, debug=False)

    def run_threaded(self):
        """Start the web server in a separate thread."""
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        logger.info("Web server thread started")
        return thread
