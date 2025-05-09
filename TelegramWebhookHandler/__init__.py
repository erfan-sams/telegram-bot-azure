# TelegramWebhookHandler/__init__.py

import logging
import os
import json
import asyncio
import datetime

import azure.functions as func

from telegram import Update
from telegram.constants import ChatAction # Make sure this is imported
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from openai import OpenAI

from azure.cosmos.aio import CosmosClient as AsyncCosmosClient
from azure.cosmos import PartitionKey, exceptions

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

logger = logging.getLogger("TelegramWebhookHandler")
# logger.setLevel(logging.DEBUG) # Uncomment for verbose local testing

# =============================================================================
# GLOBAL INITIALIZATION BLOCK
# =============================================================================
logger.info("Azure Function worker initializing (GLOBAL SCOPE)...")

TELEGRAM_BOT_TOKEN: str = None
OPENROUTER_API_KEY: str = None
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
COSMOS_DB_URI: str = None
COSMOS_DB_KEY: str = None

openrouter_client: OpenAI = None
async_container_client = None # Type will be AsyncCosmosClient.container_client
ptb_application: Application = None
ptb_bot_instance: ContextTypes.DEFAULT_TYPE.bot = None
ptb_init_lock = asyncio.Lock()

critical_secrets_missing: bool = False # DEFINED HERE

try:
    KEY_VAULT_URI = os.getenv("KEY_VAULT_URI")
    if KEY_VAULT_URI:
        logger.info(f"Attempting to load secrets from Azure Key Vault: {KEY_VAULT_URI}")
        try:
            credential = DefaultAzureCredential(); kv_secret_client = SecretClient(vault_url=KEY_VAULT_URI, credential=credential)
            TELEGRAM_BOT_TOKEN = kv_secret_client.get_secret("TELEGRAM-BOT-TOKEN").value
            OPENROUTER_API_KEY = kv_secret_client.get_secret("OPENROUTER-API-KEY").value
            COSMOS_DB_URI = kv_secret_client.get_secret("COSMOS-DB-URI").value
            COSMOS_DB_KEY = kv_secret_client.get_secret("COSMOS-DB-KEY").value
            logger.info("Successfully loaded secrets from Key Vault.")
        except Exception as e_kv_load:
            logger.error(f"ERROR loading secrets from Key Vault: {e_kv_load}. Fallback.", exc_info=True)
            TELEGRAM_BOT_TOKEN = None; OPENROUTER_API_KEY = None; COSMOS_DB_URI = None; COSMOS_DB_KEY = None
    else:
        logger.warning("KEY_VAULT_URI not set. Loading secrets directly from environment variables.")

    if not TELEGRAM_BOT_TOKEN: TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not OPENROUTER_API_KEY: OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
    if not COSMOS_DB_URI: COSMOS_DB_URI = os.getenv("COSMOS_DB_URI")
    if not COSMOS_DB_KEY: COSMOS_DB_KEY = os.getenv("COSMOS_DB_KEY")

    if not TELEGRAM_BOT_TOKEN: logger.critical("FATAL_INIT: TELEGRAM_BOT_TOKEN missing."); critical_secrets_missing = True
    if not OPENROUTER_API_KEY: logger.critical("FATAL_INIT: OPENROUTER_API_KEY missing."); critical_secrets_missing = True
    if not COSMOS_DB_URI: logger.critical("FATAL_INIT: COSMOS_DB_URI missing."); critical_secrets_missing = True
    if not COSMOS_DB_KEY: logger.critical("FATAL_INIT: COSMOS_DB_KEY missing."); critical_secrets_missing = True

    if not critical_secrets_missing:
        openrouter_client = OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
        logger.info("OpenRouter client initialized.")
        
        COSMOS_DATABASE_NAME = "TelegramBotDB"; COSMOS_CONTAINER_NAME = "ChatHistories"
        async_cosmos_client_instance = AsyncCosmosClient(COSMOS_DB_URI, credential=COSMOS_DB_KEY)
        async_database_client = async_cosmos_client_instance.get_database_client(COSMOS_DATABASE_NAME)
        async_container_client = async_database_client.get_container_client(COSMOS_CONTAINER_NAME)
        logger.info(f"Successfully initialized ASYNC clients for Cosmos DB: {COSMOS_DATABASE_NAME}/{COSMOS_CONTAINER_NAME}")
    else:
        logger.error("Skipping OpenAI/Cosmos client initialization due to missing critical secrets.")

except Exception as e_outer_init:
    logger.critical(f"FATAL UNHANDLED ERROR during global client/secret initialization: {e_outer_init}", exc_info=True)
    critical_secrets_missing = True
    openrouter_client = None; async_container_client = None; ptb_application = None; ptb_bot_instance = None

logger.info("Python Azure Function (TelegramWebhookHandler) global worker initialization sequence complete (PTB init deferred).")
# =============================================================================
# END OF GLOBAL INITIALIZATION BLOCK
# =============================================================================

SYSTEM_MESSAGE_CONTENT = ""
SYSTEM_MESSAGE = {"role": "system", "content": SYSTEM_MESSAGE_CONTENT}
MAX_CONVERSATION_MESSAGES = 20
MAX_USER_MESSAGE_LENGTH = 2000

# --- Helper Functions ---
async def get_chat_history_from_cosmos(chat_id_int: int) -> tuple[list[dict[str, str]], dict]:
    if not async_container_client: logger.error(f"ChatID {chat_id_int} - Async Cosmos DB client not available for get_chat_history."); return ([], {})
    chat_id_str = str(chat_id_int)
    try:
        item_response = await async_container_client.read_item(item=chat_id_str, partition_key=chat_id_str)
        logger.debug(f"ChatID {chat_id_str} - Document retrieved ASYNC from Cosmos DB.")
        history = item_response.get('history', []); metadata = {k: v for k, v in item_response.items() if k != 'history'}
        return (history if isinstance(history, list) else [], metadata)
    except exceptions.CosmosResourceNotFoundError: logger.info(f"ChatID {chat_id_str} - No document found ASYNC."); return ([], {})
    except Exception as e: logger.error(f"ChatID {chat_id_str} - Error reading ASYNC from Cosmos DB: {e}", exc_info=True); return ([], {})

async def save_chat_history_to_cosmos( chat_id_int: int, history_list: list[dict[str, str]], interacting_user_id: int, interacting_username: str, is_initial_creation_or_reset: bool = False, existing_metadata: dict = None ) -> None:
    if not async_container_client: logger.error(f"ChatID {chat_id_int} - Async Cosmos DB client not available for save_chat_history."); return
    chat_id_str = str(chat_id_int); user_id_str = str(interacting_user_id); username_to_store = interacting_username if interacting_username else "N/A"; current_utc_timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    item_body = { 'id': chat_id_str, 'chat_id': chat_id_str, 'history': history_list, 'last_interactor_user_id': user_id_str, 'last_interactor_username': username_to_store, 'last_updated_timestamp': current_utc_timestamp }
    if is_initial_creation_or_reset: item_body['creator_user_id'] = user_id_str; item_body['creator_username'] = username_to_store
    elif existing_metadata: item_body['creator_user_id'] = existing_metadata.get('creator_user_id', user_id_str); item_body['creator_username'] = existing_metadata.get('creator_username', username_to_store)
    else: item_body['creator_user_id'] = user_id_str; item_body['creator_username'] = username_to_store
    try:
        await async_container_client.upsert_item(body=item_body)
        logger.debug(f"ChatID {chat_id_str} - History saved/updated ASYNC by UserID {user_id_str}.")
    except Exception as e: logger.error(f"ChatID {chat_id_str} - Error saving/updating ASYNC history by UserID {user_id_str}: {e}", exc_info=True)

async def get_llm_response_from_openrouter(history_list: list[dict[str, str]], model_name: str = "meta-llama/llama-4-maverick") -> str:
    if not openrouter_client: logger.error("OpenRouter client not available."); return "Ah, my brain's not working (OpenRouter client error). Try later."
    log_history_preview = " | ".join([f"{msg['role']}: {msg['content'][:20]}" for msg in history_list[-3:]])
    logger.debug(f"Sending to OpenRouter model {model_name} (history preview: '...{log_history_preview[-100:]}')")
    try:
        chat_completion = openrouter_client.chat.completions.create( model=model_name, temperature=1, max_tokens=500, messages=history_list )
        response_content = chat_completion.choices[0].message.content
        logger.debug(f"OpenRouter response received (len: {len(response_content)}).")
        return response_content.strip()
    except Exception as e: logger.error(f"Error calling OpenRouter: {e}", exc_info=True); return "Ah, my brain's a bit fuzzy right now. Ask me later, 'kay?"

def _prepare_and_truncate_history_for_llm( full_history: list[dict[str, str]], max_messages: int, chat_id: int ) -> list[dict[str, str]]:
    if not full_history: return []
    processed_history_for_llm = full_history.copy(); system_message_obj = None; conversation_messages = []
    if processed_history_for_llm and processed_history_for_llm[0]["role"] == "system": system_message_obj = processed_history_for_llm[0]; conversation_messages = processed_history_for_llm[1:]
    else: conversation_messages = processed_history_for_llm;
    # Corrected: Only log warning if system message is missing AND history was not empty initially
    if not system_message_obj and processed_history_for_llm: logger.warning(f"ChatID {chat_id} - System message not found for LLM prep (history was not empty).")
    if len(conversation_messages) > max_messages:
        truncated_conversation = conversation_messages[-max_messages:]
        if system_message_obj: final_prepared_history = [system_message_obj] + truncated_conversation
        else: final_prepared_history = truncated_conversation
        logger.info(f"ChatID {chat_id} - History for LLM call truncated to last {max_messages} messages.")
    else:
        if system_message_obj: final_prepared_history = [system_message_obj] + conversation_messages
        else: final_prepared_history = conversation_messages
    return final_prepared_history

async def send_typing_periodically(context: ContextTypes.DEFAULT_TYPE, chat_id: int, stop_event: asyncio.Event):
    """Sends typing action every 4 seconds until stop_event is set."""
    while not stop_event.is_set():
        try:
            # Ensure context.bot is available. It should be if ptb_bot_instance is set.
            if context and context.bot:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                logger.debug(f"ChatID {chat_id} - Sent typing action.")
            else:
                logger.warning(f"ChatID {chat_id} - context.bot not available in send_typing_periodically. Stopping typing task.")
                break # Stop loop if bot instance isn't there
            # Wait for a duration less than the typical timeout (e.g., 4 seconds)
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            break # If wait_for completes, stop_event was set
        except asyncio.TimeoutError: pass # Expected, continue loop
        except Exception as e: logger.warning(f"ChatID {chat_id} - Error sending typing action: {e}. Stopping typing task."); break

# --- Telegram Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user; chat_id = update.message.chat_id; user_id = user.id; username = user.username
    logger.info(f"ChatID {chat_id} - UserID {user_id} (@{username}) used /start.")
    initial_history = [SYSTEM_MESSAGE.copy()]
    await save_chat_history_to_cosmos(chat_id, initial_history, user_id, username, is_initial_creation_or_reset=True)
    await update.message.reply_html(rf"Hey {user.mention_html()}! What's up?")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user; chat_id = update.message.chat_id; user_id = user.id; username = user.username
    logger.info(f"ChatID {chat_id} - UserID {user_id} (@{username}) used /clear.")
    cleared_history = [SYSTEM_MESSAGE.copy()]
    await save_chat_history_to_cosmos(chat_id, cleared_history, user_id, username, is_initial_creation_or_reset=True)
    await update.message.reply_text("Aight, memory wiped. Fresh start!")

async def llm_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text: return
    user_message_content = update.message.text; chat_id = update.message.chat_id; user = update.effective_user; user_id = user.id; username = user.username

    try:
        if context and context.bot: # Ensure bot instance is available
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            logger.debug(f"ChatID {chat_id} - Sent INITIAL typing action.")
        else:
            logger.warning(f"ChatID {chat_id} - context.bot not available for initial typing action.")
    except Exception as e_initial_typing:
        logger.warning(f"ChatID {chat_id} - Error sending initial typing action: {e_initial_typing}")

    if len(user_message_content) > MAX_USER_MESSAGE_LENGTH: logger.warning(f"ChatID {chat_id} - UserID {user_id} (@{username}) message > {MAX_USER_MESSAGE_LENGTH} chars. Rejecting."); await update.message.reply_text(f"Whoa there, Shakespeare! Keep it under {MAX_USER_MESSAGE_LENGTH} chars, 'kay?"); return
    logger.info(f"ChatID {chat_id} - UserID {user_id} (@{username}) sent: '{user_message_content[:50]}...'")

    retrieved_history, existing_doc_metadata = await get_chat_history_from_cosmos(chat_id)
    is_first_interaction_for_chat = not bool(retrieved_history) and not bool(existing_doc_metadata)
    
    current_turn_history = retrieved_history
    if is_first_interaction_for_chat: current_turn_history = [SYSTEM_MESSAGE.copy()]; logger.info(f"ChatID {chat_id} - UserID {user_id} - New history document. Initialized.")
    elif not current_turn_history and existing_doc_metadata: current_turn_history = [SYSTEM_MESSAGE.copy()]; logger.info(f"ChatID {chat_id} - UserID {user_id} - Existing document but empty history. Initialized.")

    current_turn_history.append({"role": "user", "content": user_message_content})
    history_for_llm = _prepare_and_truncate_history_for_llm(current_turn_history, MAX_CONVERSATION_MESSAGES, chat_id)
    
    stop_typing_event = asyncio.Event(); typing_task = None
    llm_reply_content = "Sorry, something went wrong while thinking..."
    try:
        typing_task = asyncio.create_task(send_typing_periodically(context, chat_id, stop_typing_event))
        llm_task = asyncio.create_task(get_llm_response_from_openrouter(history_for_llm))
        llm_reply_content = await llm_task
    except Exception as e_concurrent: logger.error(f"ChatID {chat_id} - Error during concurrent typing/LLM: {e_concurrent}", exc_info=True)
    finally:
        stop_typing_event.set()
        if typing_task:
            try: await asyncio.wait_for(typing_task, timeout=0.5)
            except asyncio.TimeoutError: logger.warning(f"ChatID {chat_id} - Typing task did not finish cleanly."); typing_task.cancel()
            except Exception as e_typing_cancel: logger.warning(f"ChatID {chat_id} - Exception stopping typing task: {e_typing_cancel}")

    current_turn_history.append({"role": "assistant", "content": llm_reply_content})
    await save_chat_history_to_cosmos( chat_id_int=chat_id, history_list=current_turn_history, interacting_user_id=user_id, interacting_username=username, is_initial_creation_or_reset=is_first_interaction_for_chat, existing_metadata=existing_doc_metadata if not is_first_interaction_for_chat else None )
    await update.message.reply_text(llm_reply_content)


# --- Function to perform the actual PTB initialization asynchronously ---
async def initialize_ptb_application():
    global ptb_application, ptb_bot_instance # Allow modification of global variables
    logger.info("Attempting lazy initialization of PTB application...")
    if TELEGRAM_BOT_TOKEN: # This should be True if critical_secrets_missing is False
        try:
            temp_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
            temp_app.add_handler(CommandHandler("start", start_command))
            temp_app.add_handler(CommandHandler("clear", clear_command))
            temp_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, llm_message_handler))
            await temp_app.initialize() # Crucial await
            ptb_application = temp_app
            ptb_bot_instance = temp_app.bot
            logger.info("PTB application initialized successfully (lazily).")
        except Exception as e_ptb_lazy_init:
            logger.error(f"Error during lazy PTB initialization: {e_ptb_lazy_init}", exc_info=True)
            ptb_application = None; ptb_bot_instance = None # Ensure reset on failure
    else:
        logger.error("Cannot perform lazy PTB initialization: TELEGRAM_BOT_TOKEN is missing.")


# --- Azure Function Entry Point ---
async def main(req: func.HttpRequest) -> func.HttpResponse:
    invocation_id = req.headers.get('X-Azure-Functions-InvocationId', 'N/A')
    logger.info(f"Function InvocationId: {invocation_id} - HTTP trigger request received.")

    if critical_secrets_missing: # Check flag from global init
         logger.critical(f"FATAL InvocationId: {invocation_id} - Bot critically misconfigured (secrets missing from startup). Cannot process.")
         return func.HttpResponse("Error: Bot configuration error (secrets).", status_code=500)
    
    # Optional checks for other clients (they might have internal error handling within functions using them)
    if not openrouter_client: logger.warning(f"Function InvocationId: {invocation_id} - OpenRouter client missing. LLM calls will likely fail.")
    if not async_container_client: logger.warning(f"Function InvocationId: {invocation_id} - CosmosDB client missing. History operations will fail.")

    # Lazy Initialization of PTB Application if not already done
    if not ptb_application:
        async with ptb_init_lock:
             if not ptb_application: # Double-check after acquiring lock
                 logger.info(f"Function InvocationId: {invocation_id} - ptb_application is None, attempting lazy initialization.")
                 await initialize_ptb_application()
                 if not ptb_application or not ptb_bot_instance: # Check if initialization succeeded
                     logger.critical(f"FATAL InvocationId: {invocation_id} - PTB lazy initialization FAILED. Cannot process update.")
                     return func.HttpResponse("Error: Bot application failed to initialize (PTB).", status_code=500)
                 logger.info(f"Function InvocationId: {invocation_id} - PTB lazy initialization successful.")

    try:
        request_body = req.get_json()
    except ValueError:
        logger.error(f"Function InvocationId: {invocation_id} - Request body not valid JSON.")
        return func.HttpResponse("Please pass valid JSON", status_code=400)
    
    if logger.isEnabledFor(logging.DEBUG):
        try: logger.debug(f"InvocationId: {invocation_id} - Body (500 chars): {json.dumps(request_body, indent=2)[:500]}")
        except Exception: logger.debug(f"InvocationId: {invocation_id} - Body (raw, 500 chars): {str(request_body)[:500]}")

    try:
        if not ptb_bot_instance: # Should have been caught by lazy init logic, but final safety
             logger.critical(f"FATAL InvocationId: {invocation_id} - ptb_bot_instance is None before Update.de_json. Critical error in init logic.")
             return func.HttpResponse("Error: Bot instance unavailable (internal error).", status_code=500)

        update = Update.de_json(request_body, ptb_bot_instance) 
        
        log_chat_id = update.effective_chat.id if update.effective_chat else "N/A_CHAT_ID"
        log_update_type = "N/A_UPDATE_TYPE"
        if update.effective_message: log_update_type = update.effective_message.text[:20] if update.effective_message.text else "NonTextMessage"
        elif update.callback_query: log_update_type = f"CallbackQuery:{update.callback_query.data}"
        logger.info(f"Function InvocationId: {invocation_id} - Processing update for chat ID: {log_chat_id}, Type: {log_update_type}...")
        
        await ptb_application.process_update(update)
        
        logger.info(f"Function InvocationId: {invocation_id} - Update processed successfully for chat ID: {log_chat_id}.")
        return func.HttpResponse("Update processed by function.", status_code=200)
    except Exception as e_process_update:
        log_chat_id_on_error = locals().get('log_chat_id', 'UNKNOWN_CHAT_ID_ON_PROCESS_ERROR')
        logger.error(f"Function InvocationId: {invocation_id} - Error during ptb_application.process_update for chat ID {log_chat_id_on_error}: {e_process_update}", exc_info=True)
        return func.HttpResponse("Internal error processing update.", status_code=200) # Return 200 to Telegram