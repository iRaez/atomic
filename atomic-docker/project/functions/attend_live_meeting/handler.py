import flask
import os
import sys
import importlib
import traceback
import asyncio # For running async functions from sync Flask route if needed

# Add parent 'functions' directory to sys.path for sibling imports
PACKAGE_PARENT = '..'
SCRIPT_DIR = os.path.dirname(os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__))))
FUNCTIONS_DIR_PATH = os.path.normpath(os.path.join(SCRIPT_DIR, PACKAGE_PARENT))
if FUNCTIONS_DIR_PATH not in sys.path:
    sys.path.append(FUNCTIONS_DIR_PATH)

# --- Module Imports & Initializations ---
ZoomAgent = None
note_utils_module = None # Keep a reference to the module
# process_live_audio_for_notion will be fetched from note_utils_module

# Remove direct os.environ.setdefault for API keys here.
# API keys will be passed in via request payload to the handler, then to note_utils functions.

try:
    from agents.zoom_agent import ZoomAgent as ImportedZoomAgent
    ZoomAgent = ImportedZoomAgent

    # Import note_utils once and use the module reference
    import note_utils as nu_module # Initial import
    note_utils_module = nu_module

    if ZoomAgent is None:
        print("Error: ZoomAgent failed to import.", file=sys.stderr)
    if note_utils_module is None or not hasattr(note_utils_module, 'process_live_audio_for_notion'):
        print("Error: note_utils module or process_live_audio_for_notion not found.", file=sys.stderr)

except ImportError as e:
    print(f"Critical Import Error in attend_live_meeting handler: {e}", file=sys.stderr)
    # In a real deployment, this might cause the server to fail startup if not handled by WSGI server.

app = flask.Flask(__name__)

# Standardized error response helper
def make_error_response(code: str, message: str, details: any = None, http_status: int = 500):
    return flask.jsonify({
        "ok": False,
        "error": {"code": code, "message": message, "details": details}
    }), http_status

@app.route('/', methods=['POST'])
async def attend_live_meeting_route(): # Made async
    if ZoomAgent is None or note_utils_module is None or not hasattr(note_utils_module, 'process_live_audio_for_notion'):
        return make_error_response("SERVICE_UNAVAILABLE", "Agent components not loaded.", http_status=503)

    payload = {}
    try:
        payload = flask.request.get_json()
        if not payload:
            return make_error_response("INVALID_PAYLOAD", "Request must be JSON.", http_status=400)

        action_input = payload.get('action_input', {})
        session_variables = payload.get('session_variables', {}) # For user_id
        # API keys should now come from the payload for per-request security/flexibility
        handler_input = payload.get('handler_input', {})

        platform = action_input.get('platform')
        meeting_identifier = action_input.get('meeting_identifier')
        notion_note_title = action_input.get('notion_note_title')

        # Optional params from action_input
        notion_source = action_input.get('notion_source', 'Live Meeting Transcription')
        linked_event_id = action_input.get('linked_event_id')
        notion_db_id = action_input.get('notion_db_id') # Optional DB ID for the note

        user_id = session_variables.get('x-hasura-user-id')

        # API Keys from handler_input (passed securely by caller)
        notion_api_token = handler_input.get('notion_api_token')
        deepgram_api_key = handler_input.get('deepgram_api_key')
        openai_api_key = handler_input.get('openai_api_key')

        required_params_map = {
            "platform": platform, "meeting_identifier": meeting_identifier,
            "notion_note_title": notion_note_title,
            "user_id (from session)": user_id,
            "notion_api_token (from handler_input)": notion_api_token,
            "deepgram_api_key (from handler_input)": deepgram_api_key,
            "openai_api_key (from handler_input)": openai_api_key,
        }
        missing_params = [k for k, v in required_params_map.items() if not v]
        if missing_params:
            return make_error_response("VALIDATION_ERROR", f"Missing required parameters: {', '.join(missing_params)}", http_status=400)

        if platform.lower() != "zoom":
            return make_error_response("NOT_IMPLEMENTED", f"Platform '{platform}' is not supported.", http_status=400)

        # Initialize Notion client via note_utils for this request
        # This sets the global `notion` client and default DB ID in `note_utils`
        init_notion_resp = note_utils_module.init_notion(notion_api_token, database_id=notion_db_id)
        if init_notion_resp["status"] != "success":
            return make_error_response(
                f"NOTION_INIT_ERROR_{init_notion_resp.get('code', 'UNKNOWN')}",
                init_notion_resp.get('message', "Failed to initialize Notion client."),
                init_notion_resp.get('details')
            )

        agent = ZoomAgent(user_id=user_id)
        processing_result = None # Define to ensure it's available in finally

        try:
            print(f"Handler: Attempting to join meeting {meeting_identifier} for user {user_id}", file=sys.stderr)
            join_success = agent.join_meeting(meeting_identifier)

            if not join_success:
                # join_meeting logs its own errors, this is for the HTTP response
                return make_error_response("JOIN_MEETING_FAILED", f"Agent failed to launch/join meeting: {meeting_identifier}.", http_status=500)

            print(f"Handler: Successfully joined {meeting_identifier}. Starting live processing.", file=sys.stderr)

            # Fetch the async function from the module
            process_live_audio_func = getattr(note_utils_module, 'process_live_audio_for_notion')

            processing_result = await process_live_audio_func(
                platform_module=agent, # ZoomAgent instance
                meeting_id=agent.current_meeting_id or meeting_identifier, # Use ID set by join_meeting
                notion_note_title=notion_note_title,
                deepgram_api_key=deepgram_api_key, # Pass key
                openai_api_key=openai_api_key,     # Pass key
                notion_db_id=notion_db_id,         # Pass optional DB ID (init_notion might have set default)
                notion_source=notion_source,
                linked_event_id=linked_event_id
            )

            if processing_result and processing_result.get("status") == "success":
                print(f"Handler: Notion page processed: {processing_result.get('data')}", file=sys.stderr)
                return flask.jsonify({"ok": True, "data": processing_result.get("data")}), 200
            else:
                err_msg = processing_result.get("message", "Processing failed") if processing_result else "Processing function returned None."
                err_code = processing_result.get("code", "PROCESSING_FAILED") if processing_result else "PROCESSING_RETURNED_NONE"
                err_details = processing_result.get("details") if processing_result else None
                print(f"Handler: Processing finished with error: {err_msg}", file=sys.stderr)
                return make_error_response(f"PYTHON_ERROR_{err_code}", err_msg, err_details, http_status=500)

        finally:
            if agent and agent.current_meeting_id: # Ensure agent was initialized and joined a meeting
                print(f"Handler: Live processing ended for {agent.current_meeting_id}. Ensuring agent leaves.", file=sys.stderr)
                await agent.leave_meeting() # This is now async

    except Exception as e:
        print(f"Critical error in attend_live_meeting_route: {str(e)}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return make_error_response("INTERNAL_SERVER_ERROR", f"An internal error occurred: {str(e)}", http_status=500)

if __name__ == '__main__':
    # For local testing, ensure an ASGI server like hypercorn is used for async routes.
    # Example: hypercorn handler:app -b 0.0.0.0:5000
    # Flask's built-in dev server has limited support for async that might work for simple tests,
    # but for true ASGI, a dedicated server is needed.
    print("Starting local Flask server for attend_live_meeting (async) handler on port 5000...", file=sys.stderr)

    # This basic app.run is for development and simple testing.
    # For production, use an ASGI server like Hypercorn: `hypercorn handler:app`
    # Flask 2.x+ can run async routes with its dev server.
    app.run(port=int(os.environ.get("FLASK_PORT", 5000)), debug=True)

[end of atomic-docker/project/functions/attend_live_meeting/handler.py]
