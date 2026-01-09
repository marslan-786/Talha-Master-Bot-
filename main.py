import os
import sys
import asyncio
import logging
import uuid
import shutil
import psutil
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    ReplyKeyboardRemove
)
from pyrogram.errors import MessageNotModified
from motor.motor_asyncio import AsyncIOMotorClient

# ================= CONFIGURATION =================
API_ID = 94575
API_HASH = "a3406de8d171bb422bb6ddf3bbd800e2"
BOT_TOKEN = "8505785410:AAGiDN3FuECbg_K6N_qtjK7OjXh1YYPy5fk"
MONGO_URL = "mongodb+srv://arslansalfi:786786aa@cluster0.yeycg3n.mongodb.net/?appName=Cluster0"

PORT = int(os.environ.get("PORT", 8080))

# 🔥 ROLES SETUP
MAIN_OWNER_ID = [8167904992, 7149369830]
OWNER_IDS = [8167904992, 7149369830] 

# ========= DATABASE SETUP =========
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["master_bot_db"]
users_col = db["authorized_users"]
keys_col = db["access_keys"]
projects_col = db["projects"]

# ================= GLOBAL VARIABLES =================
ACTIVE_PROCESSES = {} 
USER_STATE = {} 
LOGGING_FLAGS = {} 

logging.basicConfig(level=logging.INFO)

bot_app = Client("MasterBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ================= HELPER FUNCTIONS =================

async def is_authorized(user_id):
    if user_id in OWNER_IDS: return True
    user = await users_col.find_one({"user_id": user_id})
    if user and user.get("is_blocked", False): return False # Blocked User Check
    return True if user else False

async def update_user_info(user):
    await users_col.update_one(
        {"user_id": user.id},
        {"$set": {
            "first_name": user.first_name,
            "username": user.username or "None",
            "last_active": asyncio.get_event_loop().time()
        }},
        upsert=True
    )

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
            try: await asyncio.wait_for(proc.wait(), timeout=5.0)
            except: proc.kill()
        except: pass
        del ACTIVE_PROCESSES[project_id]

async def safe_edit(message, text, reply_markup=None):
    try: await message.edit_text(text, reply_markup=reply_markup)
    except MessageNotModified: pass 
    except: pass

async def ensure_files_exist(user_id, proj_name):
    base_path = f"./deployments/{user_id}/{proj_name}"
    if not os.path.exists(base_path):
        os.makedirs(base_path, exist_ok=True)
        doc = await projects_col.find_one({"user_id": user_id, "name": proj_name})
        if doc and "files" in doc:
            for file_obj in doc["files"]:
                with open(os.path.join(base_path, file_obj["name"]), "wb") as f:
                    f.write(file_obj["content"])
            return True
    return os.path.exists(base_path)

# ================= DUMMY SERVER =================
async def health_check(request): return web.Response(text="Running")
async def start_dummy_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ================= RESOURCE MONITOR =================
async def resource_monitor():
    while True:
        await asyncio.sleep(10)
        for project_id in list(ACTIVE_PROCESSES.keys()):
            try:
                if project_id not in ACTIVE_PROCESSES: continue
                proc_data = ACTIVE_PROCESSES[project_id]
                try:
                    process = psutil.Process(proc_data["proc"].pid)
                    if (process.memory_info().rss / (1024 * 1024)) > 1024:
                        await bot_app.send_message(proc_data["chat_id"], "⚠️ **RAM Limit (1GB) Exceeded! Bot Stopped.**")
                        await stop_project_process(project_id)
                        u_id = int(project_id.split("_")[0])
                        p_name = project_id.split("_", 1)[1]
                        await projects_col.update_one({"user_id": u_id, "name": p_name}, {"$set": {"status": "Crashed"}})
                except: pass
            except: pass

async def monitor_process_output(proc, project_id, log_path, client):
    with open(log_path, "ab") as log_file:
        while True:
            line = await proc.stdout.readline()
            if not line: break
            log_file.write(line)
            log_file.flush()
            if LOGGING_FLAGS.get(project_id, False):
                try:
                    if project_id in ACTIVE_PROCESSES:
                        decoded = line.decode('utf-8', errors='ignore').strip()
                        if decoded: await client.send_message(ACTIVE_PROCESSES[project_id]["chat_id"], f"🖥 **Log:** `{decoded}`")
                except: pass

# ================= RESTORE & START =================

async def restore_all_projects():
    print("🔄 Restoring Projects...")
    async for project in projects_col.find({"status": "Running"}):
        await ensure_files_exist(project["user_id"], project["name"])
        await start_process_logic(None, None, project["user_id"], project["name"], silent=True)

async def start_process_logic(client, chat_id, user_id, proj_name, silent=False):
    base_path = f"./deployments/{user_id}/{proj_name}"
    if not silent and client: msg = await client.send_message(chat_id, f"⏳ **Initializing {proj_name}...**")
    
    if not os.path.exists(os.path.join(base_path, "main.py")):
        if not silent and client: await msg.edit_text("❌ Files missing.")
        return

    if os.path.exists(os.path.join(base_path, "requirements.txt")):
        if not silent and client: await msg.edit_text("📥 **Installing Libs...**")
        await (await asyncio.create_subprocess_shell(f"pip install -r {base_path}/requirements.txt", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)).communicate()

    if not silent and client: await msg.edit_text("🚀 **Launching...**")
    
    run_proc = await asyncio.create_subprocess_exec("python3", "-u", "main.py", cwd=base_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    project_id = f"{user_id}_{proj_name}"
    ACTIVE_PROCESSES[project_id] = {"proc": run_proc, "chat_id": chat_id if chat_id else user_id}
    
    open(f"{base_path}/logs.txt", 'w').close()
    asyncio.create_task(monitor_process_output(run_proc, project_id, f"{base_path}/logs.txt", bot_app))
    
    await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"status": "Running", "path": base_path}})
    if not silent and client: await msg.edit_text(f"✅ **{proj_name} is Online!**", reply_markup=get_main_menu(user_id))
    
    await asyncio.sleep(5)
    if run_proc.returncode is not None:
        if project_id in ACTIVE_PROCESSES: del ACTIVE_PROCESSES[project_id]
        await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"status": "Crashed"}})
        if not silent and client: await client.send_document(chat_id, f"{base_path}/logs.txt", caption=f"⚠️ **{proj_name} Crashed!**")

# ================= COMMANDS =================

@bot_app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE: del USER_STATE[user_id]
    await update_user_info(message.from_user)

    if await is_authorized(user_id):
        await message.reply_text(f"👋 **Welcome {message.from_user.first_name}!**", reply_markup=get_main_menu(user_id))
    else:
        if len(message.command) > 1:
            token = message.command[1]
            key_doc = await keys_col.find_one({"key": token, "status": "active"})
            if key_doc:
                await keys_col.update_one({"_id": key_doc["_id"]}, {"$set": {"status": "used", "used_by": user_id}})
                await users_col.insert_one({"user_id": user_id, "first_name": message.from_user.first_name, "joined_at": message.date, "is_blocked": False})
                await message.reply_text("✅ **Access Granted!**", reply_markup=get_main_menu(user_id))
            else: await message.reply_text("❌ **Invalid Token.**")
        else: await message.reply_text("🔒 **Access Denied**")

# ================= OWNER PANEL =================

@bot_app.on_callback_query(filters.regex("owner_panel"))
async def owner_panel_cb(client, callback):
    user_id = callback.from_user.id
    if user_id not in OWNER_IDS: return await callback.answer("Admins only!", show_alert=True)
    
    btns = [[InlineKeyboardButton("🔑 Generate Key", callback_data="gen_key")]]
    if user_id == OWNER_IDS:
        btns.insert(0, [InlineKeyboardButton("👥 Authorized Users (Access)", callback_data="list_access_users")])
        btns.insert(0, [InlineKeyboardButton("📂 All Projects (Full Control)", callback_data="list_all_projects_adm")])
    
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await safe_edit(callback.message, "👑 **Owner Panel**", reply_markup=InlineKeyboardMarkup(btns))

@bot_app.on_callback_query(filters.regex("gen_key"))
async def generate_key(client, callback):
    new_key = str(uuid.uuid4())[:8]
    await keys_col.insert_one({"key": new_key, "status": "active", "created_by": callback.from_user.id})
    await safe_edit(callback.message, f"✅ Key: `{new_key}`\n`/start {new_key}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="owner_panel")]]))

# ================= MANAGE ACCESS USERS (NEW) =================

@bot_app.on_callback_query(filters.regex("list_access_users"))
async def list_access_users(client, callback):
    if callback.from_user.id != OWNER_IDS: return
    
    users = await users_col.find({"user_id": {"$nin": OWNER_IDS}}).to_list(length=100)
    if not users: return await callback.answer("No authorized users found.", show_alert=True)
    
    btns = []
    for u in users:
        status = "🚫" if u.get("is_blocked") else "✅"
        btns.append([InlineKeyboardButton(f"{status} {u.get('first_name')} ({u['user_id']})", callback_data=f"acc_view_{u['user_id']}")])
    
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="owner_panel")])
    await safe_edit(callback.message, "👥 **Authorized Users**\nSelect to manage:", reply_markup=InlineKeyboardMarkup(btns))

@bot_app.on_callback_query(filters.regex(r"^acc_view_"))
async def view_access_user(client, callback):
    if callback.from_user.id != OWNER_IDS: return
    target_id = int(callback.data.split("_")[2])
    user = await users_col.find_one({"user_id": target_id})
    
    is_blocked = user.get("is_blocked", False)
    status_text = "Blocked 🔴" if is_blocked else "Active 🟢"
    block_btn_text = "🟢 Unblock" if is_blocked else "🔴 Block Access"
    block_action = "unblock" if is_blocked else "block"
    
    text = f"👤 **User:** {user.get('first_name')}\n🆔 **ID:** `{target_id}`\n📊 **Status:** {status_text}"
    
    btns = [
        [InlineKeyboardButton(block_btn_text, callback_data=f"acc_act_{block_action}_{target_id}")],
        [InlineKeyboardButton("🗑️ Delete User (Revoke)", callback_data=f"acc_act_delete_{target_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="list_access_users")]
    ]
    await safe_edit(callback.message, text, reply_markup=InlineKeyboardMarkup(btns))

@bot_app.on_callback_query(filters.regex(r"^acc_act_"))
async def access_user_actions(client, callback):
    parts = callback.data.split("_")
    action = parts[2]
    target_id = int(parts[3])
    
    if action == "block":
        await users_col.update_one({"user_id": target_id}, {"$set": {"is_blocked": True}})
        await callback.answer("User Blocked")
    elif action == "unblock":
        await users_col.update_one({"user_id": target_id}, {"$set": {"is_blocked": False}})
        await callback.answer("User Unblocked")
    elif action == "delete":
        await users_col.delete_one({"user_id": target_id})
        await callback.answer("User Deleted")
        await list_access_users(client, callback)
        return
        
    await view_access_user(client, callback)

# ================= USER PROJECT MANAGEMENT =================

@bot_app.on_callback_query(filters.regex("deploy_new"))
async def deploy_start(client, callback):
    user_id = callback.from_user.id
    USER_STATE[user_id] = {"step": "ask_name"}
    await safe_edit(callback.message, "📂 **New Project**\nSend Name (No Spaces):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="main_menu")]]))

@bot_app.on_message(filters.text & filters.private)
async def handle_text_input(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE:
        state = USER_STATE[user_id]
        if state["step"] == "ask_name":
            proj_name = message.text.strip().replace(" ", "_")
            if await projects_col.find_one({"user_id": user_id, "name": proj_name}):
                return await message.reply("❌ Name exists.")
            USER_STATE[user_id] = {"step": "wait_files", "name": proj_name}
            await message.reply(f"✅ Project: `{proj_name}`\n**Send files now.**", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("✅ Done / Start Deploy")]], resize_keyboard=True))
        
        elif message.text == "✅ Done / Start Deploy":
            if state["step"] == "wait_files": await finish_deployment(client, message)
            elif state["step"] == "update_files": await finish_update(client, message)

@bot_app.on_message(filters.document & filters.private)
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
        with open(save_path, "rb") as f: content = f.read()
        await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$push": {"files": {"name": file_name, "content": content}}}, upsert=True)
        
        if data["step"] == "update_files":
            await message.reply(f"📥 Updated: `{file_name}`\nSend more or click 'Done'.", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("✅ Done / Start Deploy")]], resize_keyboard=True))
        else:
            await message.reply(f"📥 Received: `{file_name}`")

async def finish_deployment(client, message):
    user_id = message.from_user.id
    proj_name = USER_STATE[user_id]["name"]
    await message.reply("⚙️ **Deploying...**", reply_markup=ReplyKeyboardRemove())
    del USER_STATE[user_id]
    await start_process_logic(client, message.chat.id, user_id, proj_name)

async def finish_update(client, message):
    user_id = message.from_user.id
    proj_name = USER_STATE[user_id]["name"]
    await message.reply("⚙️ **Files Updated. Restarting...**", reply_markup=ReplyKeyboardRemove())
    del USER_STATE[user_id]
    
    # Restart the bot to apply changes
    project_id = f"{user_id}_{proj_name}"
    await stop_project_process(project_id)
    await start_process_logic(client, message.chat.id, user_id, proj_name)

@bot_app.on_callback_query(filters.regex("manage_projects"))
async def list_projects(client, callback):
    user_id = callback.from_user.id
    projects = projects_col.find({"user_id": user_id})
    btns = []
    async for p in projects:
        status = "🟢" if p.get("status") == "Running" else "🔴"
        btns.append([InlineKeyboardButton(f"{status} {p['name']}", callback_data=f"p_menu_{p['name']}")])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await safe_edit(callback.message, "📂 **Your Projects**", reply_markup=InlineKeyboardMarkup(btns))

@bot_app.on_callback_query(filters.regex(r"^p_menu_"))
async def user_project_menu(client, callback):
    # 🔥 FIXED: Robust Splitting for Names with Underscores
    p_name = callback.data.replace("p_menu_", "") 
    user_id = callback.from_user.id
    project_id = f"{user_id}_{p_name}"
    doc = await projects_col.find_one({"user_id": user_id, "name": p_name})
    if not doc: return await callback.answer("Project not found", show_alert=True)
    
    is_running = doc.get("status") == "Running"
    status_text = "🛑 Stop" if is_running else "▶️ Start"
    log_text = "🔴 Logs Off" if not LOGGING_FLAGS.get(project_id) else "🟢 Logs On"
    
    btns = [
        [InlineKeyboardButton(status_text, callback_data=f"act_toggle_{p_name}")],
        [InlineKeyboardButton("📤 Update Files", callback_data=f"act_upd_{p_name}")], # 🔥 NEW BUTTON
        [InlineKeyboardButton(log_text, callback_data=f"act_log_{p_name}"), InlineKeyboardButton("📥 Logs", callback_data=f"act_dl_{p_name}")],
        [InlineKeyboardButton("🗑️ Delete", callback_data=f"act_del_{p_name}")],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_projects")]
    ]
    await safe_edit(callback.message, f"⚙️ Manage: `{p_name}`", reply_markup=InlineKeyboardMarkup(btns))

@bot_app.on_callback_query(filters.regex(r"^act_"))
async def user_actions(client, callback):
    # 🔥 FIXED: Split logic to handle names with underscores
    parts = callback.data.split("_", 2) # Only split 2 times max
    action = parts[1]
    p_name = parts[2]
    user_id = callback.from_user.id
    project_id = f"{user_id}_{p_name}"
    
    if action == "toggle":
        doc = await projects_col.find_one({"user_id": user_id, "name": p_name})
        if doc.get("status") == "Running":
            await stop_project_process(project_id)
            await projects_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "Stopped"}})
        else:
            await ensure_files_exist(user_id, p_name)
            await start_process_logic(client, callback.message.chat.id, user_id, p_name)
        await user_project_menu(client, callback)
        
    elif action == "upd":
        USER_STATE[user_id] = {"step": "update_files", "name": p_name}
        await safe_edit(callback.message, f"📤 **Updating: `{p_name}`**\nSend new files now.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"p_menu_{p_name}")]]))
        
    elif action == "del":
        await stop_project_process(project_id)
        await projects_col.delete_one({"user_id": user_id, "name": p_name})
        path = f"./deployments/{user_id}/{p_name}"
        if os.path.exists(path): shutil.rmtree(path)
        await callback.answer("Deleted!")
        await list_projects(client, callback)
        
    elif action == "dl":
        log_p = f"./deployments/{user_id}/{p_name}/logs.txt"
        if os.path.exists(log_p): await client.send_document(callback.message.chat.id, log_p)
        else: await callback.answer("No logs")
        
    elif action == "log":
        LOGGING_FLAGS[project_id] = not LOGGING_FLAGS.get(project_id, False)
        await user_project_menu(client, callback)

@bot_app.on_callback_query(filters.regex("main_menu"))
async def back_main(client, callback):
    if callback.from_user.id in USER_STATE: del USER_STATE[callback.from_user.id]
    await safe_edit(callback.message, "🏠 **Menu**", reply_markup=get_main_menu(callback.from_user.id))

# ================= MAIN EXECUTION =================

async def main():
    print("🚀 Starting Master Bot...")
    await bot_app.start()
    asyncio.create_task(start_dummy_server())
    await restore_all_projects()
    asyncio.create_task(resource_monitor())
    print("✅ Bot Online")
    await idle()
    await bot_app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
