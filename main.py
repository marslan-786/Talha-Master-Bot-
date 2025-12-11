import os
import sys
import asyncio
import logging
import uuid
import shutil
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient

# ================= CONFIGURATION =================
# آپ کی دی گئی تفصیلات
API_ID = 94575
API_HASH = "a3406de8d171bb422bb6ddf3bbd800e2"
BOT_TOKEN = "8505785410:AAGiDN3FuECbg_K6N_qtjK7OjXh1YYPy5fk"
MONGO_URL = "mongodb://mongo:AEvrikOWlrmJCQrDTQgfGtqLlwhwLuAA@crossover.proxy.rlwy.net:29609"

# Owner IDs
OWNER_IDS = [8167904992, 7134046678] 

# ================= DATABASE SETUP =================
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["master_bot_db"]
users_col = db["authorized_users"]
keys_col = db["access_keys"]
projects_col = db["projects"]

# ================= GLOBAL VARIABLES =================
ACTIVE_PROCESSES = {} # Stores running processes
USER_STATE = {} # Stores what the user is doing

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
    """Safely kills a running process"""
    if project_id in ACTIVE_PROCESSES:
        proc = ACTIVE_PROCESSES[project_id]
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        except Exception as e:
            logging.error(f"Error killing process: {e}")
        del ACTIVE_PROCESSES[project_id]

# ================= START & AUTH FLOW =================

@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    user_id = message.from_user.id
    
    if await is_authorized(user_id):
        await message.reply_text(
            f"👋 **Welcome back, {message.from_user.first_name}!**\n\n"
            "Current System: **Unlimited Files Support**\n"
            "Select an option below:",
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
        "📂 **New Project**\n\n"
        "Please send a **Name** for your project.\n"
        "(No spaces, e.g., `MusicBot`)", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="main_menu")]])
    )

@app.on_message(filters.text & filters.private)
async def handle_text_input(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE:
        state = USER_STATE[user_id]
        
        # Step 1: Set Project Name
        if state["step"] == "ask_name":
            proj_name = message.text.strip().replace(" ", "_")
            
            exist = await projects_col.find_one({"user_id": user_id, "name": proj_name})
            if exist:
                return await message.reply("❌ Name already exists. Try another.")
            
            # Change state to wait for MULTIPLE files
            USER_STATE[user_id] = {"step": "wait_files", "name": proj_name, "file_count": 0}
            
            await message.reply(
                f"✅ Project Name: `{proj_name}`\n\n"
                "**Now send your files.**\n"
                "You can send as many files as you want (Images, .py, .txt, etc).\n\n"
                "⚠️ **IMPORTANT:** One file MUST be named `main.py`.\n\n"
                "When you are finished sending files, click the **Done** button below.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done / Start Deploy", callback_data="deploy_finish")]])
            )

@app.on_message(filters.document & filters.private)
async def handle_file_upload(client, message):
    user_id = message.from_user.id
    
    # Check if user is in 'uploading' state (either Deployment or Update)
    if user_id in USER_STATE and USER_STATE[user_id]["step"] in ["wait_files", "update_files"]:
        data = USER_STATE[user_id]
        proj_name = data["name"]
        file_name = message.document.file_name
        
        # Determine path
        base_path = f"./deployments/{user_id}/{proj_name}"
        os.makedirs(base_path, exist_ok=True)
        
        # Download and Save
        save_path = os.path.join(base_path, file_name)
        await message.download(save_path)
        
        # Increment counter just for feedback
        if "file_count" in data:
            data["file_count"] += 1
        
        # If this is Update Mode, show Restart Button immediately
        if data["step"] == "update_files":
            await message.reply(
                f"📥 **Updated:** `{file_name}`\n"
                "Send more files or click Restart to apply changes.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish & Restart Bot", callback_data=f"act_restart_{proj_name}")]])
            )
        else:
            # Deployment Mode
            await message.reply(
                f"📥 **Received:** `{file_name}`\n"
                "Keep sending files...",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done / Start Deploy", callback_data="deploy_finish")]])
            )

@app.on_callback_query(filters.regex("deploy_finish"))
async def finish_deployment(client, callback):
    user_id = callback.from_user.id
    if user_id not in USER_STATE or USER_STATE[user_id]["step"] != "wait_files":
        return await callback.answer("Session expired.", show_alert=True)
    
    proj_name = USER_STATE[user_id]["name"]
    base_path = f"./deployments/{user_id}/{proj_name}"
    
    # Validation: Check if main.py exists
    if not os.path.exists(os.path.join(base_path, "main.py")):
        return await callback.answer("❌ Error: main.py file is missing! Please send main.py.", show_alert=True)
    
    # Clear State
    del USER_STATE[user_id]
    
    # Start the actual deployment
    await start_process_logic(client, callback.message.chat.id, user_id, proj_name)

async def start_process_logic(client, chat_id, user_id, proj_name):
    base_path = f"./deployments/{user_id}/{proj_name}"
    msg = await client.send_message(chat_id, f"⚙️ **Processing {proj_name}...**")
    
    # 1. Install Requirements (Only if requirements.txt exists)
    if os.path.exists(os.path.join(base_path, "requirements.txt")):
        await msg.edit_text("📥 **Installing Requirements...**")
        install_cmd = f"pip install -r {base_path}/requirements.txt"
        proc = await asyncio.create_subprocess_shell(
            install_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        if proc.returncode != 0:
            await msg.edit_text("⚠️ **Warning:** Requirements installation had some errors, but trying to start bot anyway.")

    # 2. Run Python Script
    await msg.edit_text("🚀 **Starting Bot...**")
    
    log_file = open(f"{base_path}/runtime_error.log", "w")
    
    # ALWAYS run main.py
    run_proc = await asyncio.create_subprocess_exec(
        "python3", "main.py",
        cwd=base_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=log_file
    )
    
    # Save State
    project_id = f"{user_id}_{proj_name}"
    ACTIVE_PROCESSES[project_id] = run_proc
    
    await projects_col.update_one(
        {"user_id": user_id, "name": proj_name},
        {"$set": {"status": "Running", "main_file": "main.py", "path": base_path}},
        upsert=True
    )
    
    await msg.edit_text(f"✅ **{proj_name} is Online!**")
    
    # 3. Crash Monitor
    await asyncio.sleep(5)
    if run_proc.returncode is not None:
        log_file.close()
        await client.send_document(chat_id, f"{base_path}/runtime_error.log", caption=f"⚠️ **{proj_name} Crashed!**")
        del ACTIVE_PROCESSES[project_id]
        await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"status": "Crashed"}})

# ================= MANAGEMENT FLOW =================

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
    proj_name = callback.data.split("_")[2]
    
    btns = [
        [InlineKeyboardButton("🛑 Stop", callback_data=f"act_stop_{proj_name}"), InlineKeyboardButton("▶️ Start", callback_data=f"act_start_{proj_name}")],
        [InlineKeyboardButton("♻️ Restart", callback_data=f"act_restart_{proj_name}")],
        [InlineKeyboardButton("📤 Add/Update Files", callback_data=f"act_update_{proj_name}")],
        [InlineKeyboardButton("🗑️ Delete", callback_data=f"act_delete_{proj_name}")],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_projects")]
    ]
    await callback.message.edit_text(f"⚙️ **Manage: {proj_name}**", reply_markup=InlineKeyboardMarkup(btns))

@app.on_callback_query(filters.regex(r"^act_"))
async def project_actions(client, callback):
    action, proj_name = callback.data.split("_")[1], callback.data.split("_")[2]
    user_id = callback.from_user.id
    proj_id = f"{user_id}_{proj_name}"
    
    doc = await projects_col.find_one({"user_id": user_id, "name": proj_name})
    if not doc: return await callback.answer("Not found.")

    if action == "stop":
        await stop_project_process(proj_id)
        await projects_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "Stopped"}})
        await callback.answer("Stopped.")
        await list_projects(client, callback)

    elif action == "start":
        await callback.message.edit_text("⏳ Starting...")
        await start_process_logic(client, callback.message.chat.id, user_id, proj_name)

    elif action == "restart":
        await stop_project_process(proj_id)
        await callback.message.edit_text("♻️ Restarting...")
        await start_process_logic(client, callback.message.chat.id, user_id, proj_name)

    elif action == "delete":
        await stop_project_process(proj_id)
        await projects_col.delete_one({"_id": doc["_id"]})
        shutil.rmtree(doc["path"], ignore_errors=True)
        await callback.answer("Deleted.")
        await list_projects(client, callback)

    elif action == "update":
        # Enable update mode
        USER_STATE[user_id] = {"step": "update_files", "name": proj_name}
        await callback.message.edit_text(
            f"📤 **Update Mode: {proj_name}**\n\n"
            "Send **ANY** file (Python, Images, Txt) here.\n"
            "If the file exists, it will be replaced.\n"
            "If it's new, it will be added.\n\n"
            "Click **Restart** when you are done sending files.",
             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="manage_projects")]])
        )

# ================= NAVIGATION =================
@app.on_callback_query(filters.regex("main_menu"))
async def back_main(client, callback):
    await callback.message.edit_text("🏠 **Main Menu**", reply_markup=get_main_menu(callback.from_user.id))

if __name__ == "__main__":
    print("Master Bot Started...")
    app.run()
