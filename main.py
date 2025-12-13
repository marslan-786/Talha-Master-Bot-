import os
import sys
import asyncio
import logging
import uuid
import shutil
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    ReplyKeyboardRemove
)
from motor.motor_asyncio import AsyncIOMotorClient

# ================= CONFIGURATION =================
API_ID = 94575
API_HASH = "a3406de8d171bb422bb6ddf3bbd800e2"
BOT_TOKEN = "8505785410:AAGiDN3FuECbg_K6N_qtjK7OjXh1YYPy5fk"
MONGO_URL = "mongodb://mongo:AEvrikOWlrmJCQrDTQgfGtqLlwhwLuAA@crossover.proxy.rlwy.net:29609"

OWNER_IDS = [8167904992, 7134046678] 

# ========= DATABASE SETUP =========
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["master_bot_db"]
users_col = db["authorized_users"]
keys_col = db["access_keys"]
projects_col = db["projects"]

# ================= GLOBAL VARIABLES =================
ACTIVE_PROCESSES = {}  # { "user_proj": { "proc": proc_obj, "chat_id": 123 } }
USER_STATE = {} 
LOGGING_FLAGS = {} # { "user_proj": True/False } -> To toggle live logs

logging.basicConfig(level=logging.INFO)
app = Client("MasterBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ================= HELPER FUNCTIONS =================

async def is_authorized(user_id):
    if user_id in OWNER_IDS:
        return True
    user = await users_col.find_one({"user_id": user_id})
    return True if user else False

def get_main_menu(user_id):
    btns = [
        [InlineKeyboardButton("🚀 Deploy New Project", callback_data="deploy_new")],
        [InlineKeyboardButton("📂 Manage Projects", callback_data="manage_projects")]
    ]
    if user_id in OWNER_IDS:
        btns.append([InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")])
    return InlineKeyboardMarkup(btns)

async def stop_project_process(project_id):
    if project_id in ACTIVE_PROCESSES:
        data = ACTIVE_PROCESSES[project_id]
        proc = data["proc"]
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        except Exception as e:
            logging.error(f"Error killing process: {e}")
        del ACTIVE_PROCESSES[project_id]

# ================= LIVE LOG MONITORING =================

async def monitor_process_output(proc, project_id, log_path, client):
    """
    Reads stdout/stderr line by line.
    Saves to file AND sends to Telegram if Live Logs are enabled.
    """
    # Open file in append mode
    with open(log_path, "ab") as log_file:
        while True:
            # Read line
            line = await proc.stdout.readline()
            if not line:
                break
            
            # 1. Write to Log File
            log_file.write(line)
            log_file.flush()
            
            # 2. Check if Live Logging is ON
            if LOGGING_FLAGS.get(project_id, False):
                try:
                    # Get Chat ID from running process data
                    if project_id in ACTIVE_PROCESSES:
                        chat_id = ACTIVE_PROCESSES[project_id]["chat_id"]
                        decoded_line = line.decode('utf-8', errors='ignore').strip()
                        if decoded_line:
                            await client.send_message(chat_id, f"🖥 **{project_id.split('_')[1]}:** `{decoded_line}`")
                except Exception as e:
                    print(f"Log Send Error: {e}")
                    # Don't break loop, just skip sending message

# ================= AUTO-RESTORE SYSTEM =================

async def restore_all_projects():
    print("🔄 SYSTEM: Checking Database for saved bots...")
    
    async for project in projects_col.find({"status": "Running"}):
        user_id = project["user_id"]
        proj_name = project["name"]
        base_path = f"./deployments/{user_id}/{proj_name}"
        
        print(f"♻️ Restoring Project: {proj_name}...")
        
        if not os.path.exists(base_path):
            os.makedirs(base_path, exist_ok=True)
            
        saved_files = project.get("files", [])
        
        if not saved_files:
            continue

        for file_obj in saved_files:
            file_name = file_obj["name"]
            file_content = file_obj["content"]
            with open(os.path.join(base_path, file_name), "wb") as f:
                f.write(file_content)

        # Start Process Silently
        await start_process_logic(None, None, user_id, proj_name, silent=True)

# ================= START & AUTH FLOW =================

@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE: del USER_STATE[user_id]

    if await is_authorized(user_id):
        await message.reply_text(
            f"👋 **Welcome back, {message.from_user.first_name}!**\n\n"
            "**New Features:**\n✅ Unified Start/Stop\n✅ Live Logs Toggle\n✅ Download Logs",
            reply_markup=get_main_menu(user_id)
        )
    else:
        if len(message.command) > 1:
            token = message.command[1]
            key_doc = await keys_col.find_one({"key": token, "status": "active"})
            if key_doc:
                await keys_col.update_one({"_id": key_doc["_id"]}, {"$set": {"status": "used", "used_by": user_id}})
                await users_col.insert_one({"user_id": user_id, "joined_at": message.date})
                await message.reply_text("✅ **Access Granted!**", reply_markup=get_main_menu(user_id))
            else:
                await message.reply_text("❌ **Invalid Token.**")
        else:
            await message.reply_text("🔒 **Access Denied**\nUse `/start <key>` to login.")

# ================= OWNER PANEL =================

@app.on_callback_query(filters.regex("owner_panel"))
async def owner_panel_cb(client, callback):
    if callback.from_user.id not in OWNER_IDS:
        return await callback.answer("Admins only!", show_alert=True)
    btns = [
        [InlineKeyboardButton("🔑 Generate Key", callback_data="gen_key")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    await callback.message.edit_text("👑 **Owner Panel**", reply_markup=InlineKeyboardMarkup(btns))

@app.on_callback_query(filters.regex("gen_key"))
async def generate_key(client, callback):
    new_key = str(uuid.uuid4())[:8]
    await keys_col.insert_one({"key": new_key, "status": "active", "created_by": callback.from_user.id})
    await callback.message.edit_text(f"✅ Key: `{new_key}`\nCommand: `/start {new_key}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="owner_panel")]]))

# ================= DEPLOYMENT FLOW =================

@app.on_callback_query(filters.regex("deploy_new"))
async def deploy_start(client, callback):
    user_id = callback.from_user.id
    USER_STATE[user_id] = {"step": "ask_name"}
    await callback.message.edit_text(
        "📂 **New Project**\nSend a **Name** (e.g., `MusicBot`)", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="main_menu")]])
    )

@app.on_message(filters.text & filters.private)
async def handle_text_input(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE:
        state = USER_STATE[user_id]
        if state["step"] == "ask_name":
            proj_name = message.text.strip().replace(" ", "_")
            exist = await projects_col.find_one({"user_id": user_id, "name": proj_name})
            if exist: return await message.reply("❌ Name exists. Try another.")
            
            USER_STATE[user_id] = {"step": "wait_files", "name": proj_name}
            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton("✅ Done / Start Deploy")]], 
                resize_keyboard=True
            )
            await message.reply(
                f"✅ Project: `{proj_name}`\n**Now send files.**\n(Main file MUST be `main.py`).\nPress Button below when done. 👇",
                reply_markup=keyboard
            )

        elif message.text == "✅ Done / Start Deploy":
            if state["step"] == "wait_files":
                await finish_deployment(client, message)

@app.on_message(filters.document & filters.private)
async def handle_file_upload(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE and USER_STATE[user_id]["step"] in ["wait_files", "update_files"]:
        data = USER_STATE[user_id]
        proj_name = data["name"]
        file_name = message.document.file_name
        
        base_path = f"./deployments/{user_id}/{proj_name}"
        os.makedirs(base_path, exist_ok=True)
        save_path = os.path.join(base_path, file_name)
        
        await message.download(save_path)
        
        with open(save_path, "rb") as f:
            file_content = f.read()
            
        await projects_col.update_one(
            {"user_id": user_id, "name": proj_name},
            {"$pull": {"files": {"name": file_name}}}
        )
        await projects_col.update_one(
            {"user_id": user_id, "name": proj_name},
            {"$push": {"files": {"name": file_name, "content": file_content}}},
            upsert=True
        )

        if data["step"] == "update_files":
            await message.reply(f"📥 **Updated & Saved:** `{file_name}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish & Restart", callback_data=f"act_restart_{proj_name}")]]))
        else:
            await message.reply(f"📥 **Received:** `{file_name}`")

async def finish_deployment(client, message):
    user_id = message.from_user.id
    proj_name = USER_STATE[user_id]["name"]
    base_path = f"./deployments/{user_id}/{proj_name}"
    
    if not os.path.exists(os.path.join(base_path, "main.py")):
        return await message.reply("❌ `main.py` missing!")
    
    await message.reply("⚙️ **Starting Deployment...**", reply_markup=ReplyKeyboardRemove())
    del USER_STATE[user_id]
    await start_process_logic(client, message.chat.id, user_id, proj_name)

# ================= PROCESS LOGIC (UPDATED) =================

async def start_process_logic(client, chat_id, user_id, proj_name, silent=False):
    base_path = f"./deployments/{user_id}/{proj_name}"
    
    if not silent and client:
        msg = await client.send_message(chat_id, f"⏳ **Initializing {proj_name}...**")
    
    if not os.path.exists(os.path.join(base_path, "main.py")):
        if not silent and client: await msg.edit_text("❌ Error: Files lost/not found.")
        return

    # Install Requirements
    if os.path.exists(os.path.join(base_path, "requirements.txt")):
        if not silent and client: await msg.edit_text("📥 **Installing Libraries...**")
        install_cmd = f"pip install -r {base_path}/requirements.txt"
        proc = await asyncio.create_subprocess_shell(install_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.communicate()

    # Run Bot (with -u for unbuffered stdout)
    if not silent and client: await msg.edit_text("🚀 **Launching Bot...**")
    
    # We merge stderr into stdout for simpler logging
    run_proc = await asyncio.create_subprocess_exec(
        "python3", "-u", "main.py", 
        cwd=base_path, 
        stdout=asyncio.subprocess.PIPE, 
        stderr=asyncio.subprocess.STDOUT
    )
    
    project_id = f"{user_id}_{proj_name}"
    
    # Store process AND chat_id for live logging
    ACTIVE_PROCESSES[project_id] = {
        "proc": run_proc,
        "chat_id": chat_id
    }
    
    # Start Monitor Task
    log_file_path = f"{base_path}/logs.txt"
    # Clear old logs
    open(log_file_path, 'w').close()
    
    asyncio.create_task(monitor_process_output(run_proc, project_id, log_file_path, app))
    
    await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"status": "Running", "path": base_path}})
    
    if not silent and client:
        await msg.edit_text(f"✅ **{proj_name} is Online!**", reply_markup=get_main_menu(user_id))
    
    # Check if immediate crash
    await asyncio.sleep(5)
    if run_proc.returncode is not None:
        del ACTIVE_PROCESSES[project_id]
        await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"status": "Crashed"}})
        if not silent and client:
             await client.send_document(chat_id, log_file_path, caption=f"⚠️ **{proj_name} Crashed!**")

# ================= MANAGEMENT FLOW (UPDATED) =================

@app.on_callback_query(filters.regex("manage_projects"))
async def list_projects(client, callback):
    user_id = callback.from_user.id
    projects = projects_col.find({"user_id": user_id})
    btns = []
    async for p in projects:
        status = "🟢" if p.get("status") == "Running" else "🔴"
        btns.append([InlineKeyboardButton(f"{status} {p['name']}", callback_data=f"p_menu_{p['name']}")])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await callback.message.edit_text("📂 **Your Projects**", reply_markup=InlineKeyboardMarkup(btns))

@app.on_callback_query(filters.regex(r"^p_menu_"))
async def project_menu(client, callback):
    try: proj_name = callback.data.split("_", 2)[2]
    except: return
    
    user_id = callback.from_user.id
    project_id = f"{user_id}_{proj_name}"
    
    # Check Status from DB
    doc = await projects_col.find_one({"user_id": user_id, "name": proj_name})
    if not doc: return await callback.answer("Not Found")
    
    is_running = doc.get("status") == "Running"
    
    # 1. Toggle Button Text
    status_btn_text = "🛑 Stop" if is_running else "▶️ Start"
    status_callback = f"act_toggle_{proj_name}" # Unified Callback
    
    # 2. Log Toggle Text
    is_logging = LOGGING_FLAGS.get(project_id, False)
    log_btn_text = "🔴 Disable Logs" if is_logging else "🟢 Enable Logs"
    
    btns = [
        [InlineKeyboardButton(status_btn_text, callback_data=status_callback)],
        [
            InlineKeyboardButton(log_btn_text, callback_data=f"act_logtoggle_{proj_name}"),
            InlineKeyboardButton("📥 Download Logs", callback_data=f"act_dl_logs_{proj_name}")
        ],
        [InlineKeyboardButton("♻️ Restart", callback_data=f"act_restart_{proj_name}")],
        [InlineKeyboardButton("📤 Update Files", callback_data=f"act_update_{proj_name}")],
        [InlineKeyboardButton("🗑️ Delete", callback_data=f"act_delete_{proj_name}")],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_projects")]
    ]
    
    status_display = "Running 🟢" if is_running else "Stopped 🔴"
    await callback.message.edit_text(f"⚙️ **Manage: {proj_name}**\nStatus: {status_display}", reply_markup=InlineKeyboardMarkup(btns))

@app.on_callback_query(filters.regex(r"^act_"))
async def project_actions(client, callback):
    try:
        parts = callback.data.split("_", 2)
        action, proj_name = parts[1], parts[2]
    except: return

    user_id = callback.from_user.id
    project_id = f"{user_id}_{proj_name}"
    doc = await projects_col.find_one({"user_id": user_id, "name": proj_name})
    if not doc: return await callback.answer("Project Not Found!", show_alert=True)

    # --- UNIFIED TOGGLE (START/STOP) ---
    if action == "toggle":
        if doc.get("status") == "Running":
            # STOP IT
            await stop_project_process(project_id)
            await projects_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "Stopped"}})
            await callback.answer("🛑 Project Stopped")
        else:
            # START IT
            await callback.answer("▶️ Starting...")
            await start_process_logic(client, callback.message.chat.id, user_id, proj_name)
        
        # Refresh Menu
        await project_menu(client, callback)

    # --- LIVE LOG TOGGLE ---
    elif action == "logtoggle":
        current = LOGGING_FLAGS.get(project_id, False)
        LOGGING_FLAGS[project_id] = not current
        new_status = "Enabled" if not current else "Disabled"
        await callback.answer(f"Logs {new_status}!")
        await project_menu(client, callback)

    # --- DOWNLOAD LOGS ---
    elif action == "dl_logs":
        log_path = f"./deployments/{user_id}/{proj_name}/logs.txt"
        if os.path.exists(log_path):
            await client.send_document(callback.message.chat.id, log_path, caption=f"📄 Logs: {proj_name}")
        else:
            await callback.answer("❌ No logs found.", show_alert=True)

    elif action == "restart":
        await stop_project_process(project_id)
        await callback.message.edit_text("♻️ Restarting...")
        await start_process_logic(client, callback.message.chat.id, user_id, proj_name)

    elif action == "delete":
        await stop_project_process(project_id)
        await projects_col.delete_one({"_id": doc["_id"]})
        shutil.rmtree(doc["path"], ignore_errors=True)
        await callback.answer("Deleted.")
        await list_projects(client, callback)

    elif action == "update":
        USER_STATE[user_id] = {"step": "update_files", "name": proj_name}
        await callback.message.edit_text(
            f"📤 **Update Mode: {proj_name}**\nSend new files. Files are auto-saved to DB.\nClick Back to cancel.",
             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="manage_projects")]])
        )

# ================= NAVIGATION & STARTUP =================
@app.on_callback_query(filters.regex("main_menu"))
async def back_main(client, callback):
    if callback.from_user.id in USER_STATE: del USER_STATE[callback.from_user.id]
    await callback.message.edit_text("🏠 **Main Menu**", reply_markup=get_main_menu(callback.from_user.id))

async def main():
    print("Master Bot Starting...")
    await app.start()
    
    # --- AUTO RESTORE TRIGGER ---
    await restore_all_projects()
    
    print("Master Bot is IDLE...")
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
