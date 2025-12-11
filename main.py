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

# ================= DATABASE SETUP =================
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["master_bot_db"]
users_col = db["authorized_users"]
keys_col = db["access_keys"]
projects_col = db["projects"]

# ================= GLOBAL VARIABLES =================
ACTIVE_PROCESSES = {} 
USER_STATE = {} 

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

# ================= AUTO-RESTORE SYSTEM (NEW) =================
# یہ فنکشن بوٹ اسٹارٹ ہوتے ہی چلے گا اور ڈیٹا بیس سے فائلیں واپس لائے گا

async def restore_all_projects():
    print("🔄 Checking for projects to restore...")
    async for project in projects_col.find({"status": "Running"}):
        user_id = project["user_id"]
        proj_name = project["name"]
        base_path = f"./deployments/{user_id}/{proj_name}"
        
        # اگر فولڈر نہیں ہے (مطلب ریلوے نے اڑا دیا)، تو دوبارہ بنائیں
        if not os.path.exists(base_path):
            print(f"♻️ Restoring Files for: {proj_name}")
            os.makedirs(base_path, exist_ok=True)
            
            # ڈیٹا بیس سے فائلیں نکال کر ڈسک پر لکھیں
            files_data = project.get("stored_files", {})
            for fname, fcontent in files_data.items():
                with open(os.path.join(base_path, fname), "wb") as f:
                    f.write(fcontent)
            
            # دوبارہ چلائیں
            await start_process_logic(None, None, user_id, proj_name, silent=True)
        else:
            # اگر فولڈر ہے لیکن پروسیس نہیں چل رہا
            project_id = f"{user_id}_{proj_name}"
            if project_id not in ACTIVE_PROCESSES:
                 await start_process_logic(None, None, user_id, proj_name, silent=True)

# ================= START & AUTH FLOW =================

@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE: del USER_STATE[user_id]

    if await is_authorized(user_id):
        await message.reply_text(
            f"👋 **Welcome back, {message.from_user.first_name}!**\n\n"
            "**System Status:** Persistent Storage Enabled ✅\n"
            "Projects will auto-restore after updates.",
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
        "📂 **New Project**\nSend a **Name** (No spaces, e.g., `MusicBot`)", 
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
            keyboard = ReplyKeyboardMarkup([[KeyboardButton("✅ Done / Start Deploy")]], resize_keyboard=True)
            await message.reply(
                f"✅ Project: `{proj_name}`\n**Now send files.**\n(Main file MUST be `main.py`).\nPress Button when done. 👇",
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
        
        # 1. Save to Disk
        await message.download(save_path)
        
        # 2. Read Bytes and Save to DB (PERSISTENCE MAGIC)
        with open(save_path, "rb") as f:
            file_content = f.read()
            
        await projects_col.update_one(
            {"user_id": user_id, "name": proj_name},
            {"$set": {f"stored_files.{file_name.replace('.', '_DO_')}" : file_content}}, # Dot replace for mongo key safety
            upsert=True
        )
        # Note: In Mongo keys, dots are restricted, so we replace '.' with '_DO_' temporarily, 
        # but simpler logic for now: Just storing binary content mapped to filename is complex in keys.
        # BETTER APPROACH:
        await projects_col.update_one(
            {"user_id": user_id, "name": proj_name},
            {"$set": {f"stored_files.{file_name}": file_content}}, 
            upsert=True
        )

        if data["step"] == "update_files":
            await message.reply(f"📥 **Updated & Saved to DB:** `{file_name}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish & Restart", callback_data=f"act_restart_{proj_name}")]]))
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

async def start_process_logic(client, chat_id, user_id, proj_name, silent=False):
    base_path = f"./deployments/{user_id}/{proj_name}"
    
    # If client/chat_id is None (Auto Restore Mode), we just print logs
    if not silent and client:
        msg = await client.send_message(chat_id, f"⏳ **Initializing {proj_name}...**")
    
    if os.path.exists(os.path.join(base_path, "requirements.txt")):
        if not silent and client: await msg.edit_text("📥 **Installing Libraries...**")
        install_cmd = f"pip install -r {base_path}/requirements.txt"
        proc = await asyncio.create_subprocess_shell(install_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.communicate()

    if not silent and client: await msg.edit_text("🚀 **Launching Bot...**")
    
    log_file = open(f"{base_path}/runtime_error.log", "w")
    run_proc = await asyncio.create_subprocess_exec("python3", "main.py", cwd=base_path, stdout=asyncio.subprocess.PIPE, stderr=log_file)
    
    project_id = f"{user_id}_{proj_name}"
    ACTIVE_PROCESSES[project_id] = run_proc
    
    await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"status": "Running", "path": base_path}})
    
    if not silent and client:
        await msg.edit_text(f"✅ **{proj_name} is Online!**", reply_markup=get_main_menu(user_id))
    
    # Crash Monitor
    await asyncio.sleep(5)
    if run_proc.returncode is not None:
        log_file.close()
        del ACTIVE_PROCESSES[project_id]
        await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"status": "Crashed"}})
        if not silent and client:
             await client.send_document(chat_id, f"{base_path}/runtime_error.log", caption=f"⚠️ **{proj_name} Crashed!**")

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
    try: proj_name = callback.data.split("_", 2)[2]
    except: return
    btns = [
        [InlineKeyboardButton("🛑 Stop", callback_data=f"act_stop_{proj_name}"), InlineKeyboardButton("▶️ Start", callback_data=f"act_start_{proj_name}")],
        [InlineKeyboardButton("♻️ Restart", callback_data=f"act_restart_{proj_name}")],
        [InlineKeyboardButton("📤 Update Files", callback_data=f"act_update_{proj_name}")],
        [InlineKeyboardButton("🗑️ Delete", callback_data=f"act_delete_{proj_name}")],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_projects")]
    ]
    await callback.message.edit_text(f"⚙️ **Manage: {proj_name}**", reply_markup=InlineKeyboardMarkup(btns))

@app.on_callback_query(filters.regex(r"^act_"))
async def project_actions(client, callback):
    try:
        parts = callback.data.split("_", 2)
        action, proj_name = parts[1], parts[2]
    except: return

    user_id = callback.from_user.id
    doc = await projects_col.find_one({"user_id": user_id, "name": proj_name})
    if not doc: return await callback.answer("Project Not Found!", show_alert=True)

    if action == "stop":
        await stop_project_process(f"{user_id}_{proj_name}")
        await projects_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "Stopped"}})
        await callback.answer("Stopped.")
        await list_projects(client, callback)

    elif action == "start":
        await callback.message.edit_text("⏳ Starting...")
        await start_process_logic(client, callback.message.chat.id, user_id, proj_name)

    elif action == "restart":
        await stop_project_process(f"{user_id}_{proj_name}")
        await callback.message.edit_text("♻️ Restarting...")
        await start_process_logic(client, callback.message.chat.id, user_id, proj_name)

    elif action == "delete":
        await stop_project_process(f"{user_id}_{proj_name}")
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
    # جیسے ہی بوٹ سٹارٹ ہوگا، یہ پچھلے پروجیکٹس کو ریسٹور کرے گا
    await restore_all_projects()
    print("Master Bot is IDLE...")
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
