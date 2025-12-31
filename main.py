import os
import sys
import asyncio
import logging
import uuid
import shutil
import psutil
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
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from uvicorn import Config, Server

# ================= CONFIGURATION =================
API_ID = 94575
API_HASH = "a3406de8d171bb422bb6ddf3bbd800e2"
BOT_TOKEN = "8505785410:AAGiDN3FuECbg_K6N_qtjK7OjXh1YYPy5fk"
MONGO_URL = "mongodb://mongo:AEvrikOWlrmJCQrDTQgfGtqLlwhwLuAA@crossover.proxy.rlwy.net:29609"

# Railway Port Logic
PORT = int(os.environ.get("PORT", 8080))

OWNER_IDS = [8167904992, 7134046678] 

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

# 🔥 Telegram Bot Client
bot_app = Client("MasterBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# 🔥 FastAPI Web App
app = FastAPI()

# ================= HELPER FUNCTIONS =================

async def is_authorized(user_id):
    if user_id in OWNER_IDS:
        return True
    user = await users_col.find_one({"user_id": user_id})
    return True if user else False

async def update_user_info(user):
    """Saves/Updates User Name and Username in DB"""
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
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        except Exception as e:
            logging.error(f"Error killing process: {e}")
        del ACTIVE_PROCESSES[project_id]

async def safe_edit(message, text, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except MessageNotModified:
        pass 
    except Exception as e:
        logging.error(f"Edit Error: {e}")

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

# ================= RESOURCE PROTECTION (RAM LIMIT) =================

async def resource_monitor():
    while True:
        await asyncio.sleep(10)
        for project_id in list(ACTIVE_PROCESSES.keys()):
            try:
                if project_id not in ACTIVE_PROCESSES: continue
                
                proc_data = ACTIVE_PROCESSES[project_id]
                pid = proc_data["proc"].pid
                chat_id = proc_data["chat_id"]
                
                try:
                    process = psutil.Process(pid)
                    mem_info = process.memory_info()
                    mem_usage_mb = mem_info.rss / (1024 * 1024)
                    
                    if mem_usage_mb > 1024:
                        clean_name = project_id.split("_", 1)[1] if "_" in project_id else project_id
                        await bot_app.send_message(
                            chat_id, 
                            f"⚠️ **CRITICAL WARNING:** Your bot `{clean_name}` was using **{int(mem_usage_mb)}MB RAM** (Limit: 1GB).\n\n🛑 **It has been stopped.**"
                        )
                        await stop_project_process(project_id)
                        user_id = int(project_id.split("_")[0])
                        p_name = project_id.split("_", 1)[1]
                        await projects_col.update_one({"user_id": user_id, "name": p_name}, {"$set": {"status": "Crashed (RAM Limit)"}})
                        
                except psutil.NoSuchProcess:
                    pass
            except Exception as e:
                logging.error(f"Monitor Error: {e}")

# ================= LIVE LOG MONITORING =================

async def monitor_process_output(proc, project_id, log_path, client):
    with open(log_path, "ab") as log_file:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            
            log_file.write(line)
            log_file.flush()
            
            if LOGGING_FLAGS.get(project_id, False):
                try:
                    if project_id in ACTIVE_PROCESSES:
                        chat_id = ACTIVE_PROCESSES[project_id]["chat_id"]
                        clean_name = project_id.split("_", 1)[1]
                        decoded_line = line.decode('utf-8', errors='ignore').strip()
                        if decoded_line:
                            await client.send_message(chat_id, f"🖥 **{clean_name}:** `{decoded_line}`")
                except Exception:
                    pass

# ================= AUTO-RESTORE SYSTEM =================

async def restore_all_projects():
    print("🔄 SYSTEM: Checking Database for saved bots...")
    async for project in projects_col.find({"status": "Running"}):
        user_id = project["user_id"]
        proj_name = project["name"]
        await ensure_files_exist(user_id, proj_name)
        await start_process_logic(None, None, user_id, proj_name, silent=True)

# ================= WEB DASHBOARD (HTML GENERATION) =================

def get_html_base(content):
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Master Bot Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background-color: #f8f9fa; }}
            .card {{ margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); border-radius: 10px; }}
            .header {{ background: linear-gradient(90deg, #0d6efd 0%, #0043a8 100%); color: white; padding: 25px; margin-bottom: 30px; }}
            .user-name {{ font-size: 1.2rem; font-weight: bold; color: #333; }}
            .user-meta {{ color: #666; font-size: 0.9rem; }}
        </style>
    </head>
    <body>
        <div class="header text-center">
            <h1>🤖 Master Bot Admin Panel</h1>
        </div>
        <div class="container">
            {content}
        </div>
    </body>
    </html>
    """

@app.get("/", response_class=HTMLResponse)
async def home():
    users = await users_col.find().to_list(length=None) 
    user_list_html = ""
    for u in users:
        user_id = u['user_id']
        # 🔥 Feature: Show Name instead of just ID
        first_name = u.get("first_name", f"User {user_id}") 
        project_count = await projects_col.count_documents({"user_id": user_id})
        
        user_list_html += f"""
        <div class="col-md-4">
            <div class="card">
                <div class="card-body">
                    <h5 class="card-title user-name">👤 {first_name}</h5>
                    <p class="user-meta">ID: {user_id}</p>
                    <hr>
                    <p>Projects: <strong>{project_count}</strong></p>
                    <a href="/user/{user_id}" class="btn btn-primary w-100">View Details</a>
                </div>
            </div>
        </div>
        """
    return get_html_base(f'<div class="row">{user_list_html}</div>')

@app.get("/user/{user_id}", response_class=HTMLResponse)
async def view_user(user_id: int):
    # 🔥 Feature: Get Full User Details for Header
    user_doc = await users_col.find_one({"user_id": user_id})
    
    first_name = user_doc.get("first_name", "Unknown") if user_doc else "Unknown"
    username = user_doc.get("username", "N/A") if user_doc else "N/A"
    
    header_info = f"""
    <div class="alert alert-info">
        <h3>👤 {first_name}</h3>
        <p><strong>Username:</strong> @{username} <br> <strong>User ID:</strong> {user_id}</p>
    </div>
    """

    projects = await projects_col.find({"user_id": user_id}).to_list(length=None)
    proj_html = f'{header_info}<h4>📂 Active Projects</h4><a href="/" class="btn btn-secondary mb-3">⬅️ Back to Users</a><div class="row">'
    
    for p in projects:
        status = "🟢 Running" if p.get("status") == "Running" else "🔴 Stopped"
        if "Crashed" in p.get("status", ""): status = "⚠️ Crashed"
        
        p_name = p['name']
        proj_html += f"""
        <div class="col-md-6">
            <div class="card">
                <div class="card-body">
                    <h5 class="card-title">{p_name}</h5>
                    <p>Status: <strong>{status}</strong></p>
                    <div class="d-grid gap-2">
                        <a href="/action/{user_id}/{p_name}/stop" class="btn btn-warning btn-sm">🛑 Stop</a>
                        <a href="/action/{user_id}/{p_name}/start" class="btn btn-success btn-sm">▶️ Start</a>
                        <a href="/action/{user_id}/{p_name}/download" class="btn btn-info btn-sm">📥 Download Files</a>
                        <a href="/action/{user_id}/{p_name}/delete" class="btn btn-danger btn-sm" onclick="return confirm('Are you sure?')">🗑️ Delete</a>
                    </div>
                </div>
            </div>
        </div>
        """
    return get_html_base(f'{proj_html}</div>')

@app.get("/action/{user_id}/{p_name}/{action}")
async def project_action(user_id: int, p_name: str, action: str):
    project_id = f"{user_id}_{p_name}"
    doc = await projects_col.find_one({"user_id": user_id, "name": p_name})
    
    if not doc: return HTMLResponse("Project not found")
    base_path = f"./deployments/{user_id}/{p_name}"

    if action == "stop":
        await stop_project_process(project_id)
        await projects_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "Stopped"}})
    
    elif action == "start":
        await ensure_files_exist(user_id, p_name)
        await start_process_logic(None, None, user_id, p_name, silent=True)
    
    elif action == "delete":
        await stop_project_process(project_id)
        await projects_col.delete_one({"_id": doc["_id"]})
        shutil.rmtree(doc["path"], ignore_errors=True)
    
    elif action == "download":
        if not await ensure_files_exist(user_id, p_name):
            return HTMLResponse("❌ Error: Files could not be restored from Database.")
        zip_name = f"/tmp/{p_name}_files"
        shutil.make_archive(zip_name, 'zip', base_path)
        return FileResponse(f"{zip_name}.zip", filename=f"{p_name}.zip")

    return RedirectResponse(url=f"/user/{user_id}")

# ================= TELEGRAM BOT LOGIC =================

@bot_app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE: del USER_STATE[user_id]

    # 🔥 Feature: Update Name/Username on every interaction
    await update_user_info(message.from_user)

    if await is_authorized(user_id):
        await message.reply_text(
            f"👋 **Welcome back, {message.from_user.first_name}!**\n\n✅ **System Online**\n🛡️ **RAM Limit:** 1GB per bot enforced.",
            reply_markup=get_main_menu(user_id)
        )
    else:
        if len(message.command) > 1:
            token = message.command[1]
            key_doc = await keys_col.find_one({"key": token, "status": "active"})
            if key_doc:
                await keys_col.update_one({"_id": key_doc["_id"]}, {"$set": {"status": "used", "used_by": user_id}})
                # Save user info with name
                await users_col.insert_one({
                    "user_id": user_id, 
                    "first_name": message.from_user.first_name,
                    "username": message.from_user.username,
                    "joined_at": message.date
                })
                await message.reply_text("✅ **Access Granted!**", reply_markup=get_main_menu(user_id))
            else:
                await message.reply_text("❌ **Invalid Token.**")
        else:
            await message.reply_text("🔒 **Access Denied**\nUse `/start <key>` to login.")

@bot_app.on_callback_query(filters.regex("owner_panel"))
async def owner_panel_cb(client, callback):
    if callback.from_user.id not in OWNER_IDS:
        return await callback.answer("Admins only!", show_alert=True)
    btns = [
        [InlineKeyboardButton("🔑 Generate Key", callback_data="gen_key")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    await safe_edit(callback.message, "👑 **Owner Panel**", reply_markup=InlineKeyboardMarkup(btns))

@bot_app.on_callback_query(filters.regex("gen_key"))
async def generate_key(client, callback):
    new_key = str(uuid.uuid4())[:8]
    await keys_col.insert_one({"key": new_key, "status": "active", "created_by": callback.from_user.id})
    await safe_edit(callback.message, f"✅ Key: `{new_key}`\nCommand: `/start {new_key}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="owner_panel")]]))

@bot_app.on_callback_query(filters.regex("deploy_new"))
async def deploy_start(client, callback):
    user_id = callback.from_user.id
    USER_STATE[user_id] = {"step": "ask_name"}
    await safe_edit(callback.message, "📂 **New Project**\nSend a **Name** (e.g., `Group_Otp_Bot`)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="main_menu")]]))

@bot_app.on_message(filters.text & filters.private)
async def handle_text_input(client, message):
    user_id = message.from_user.id
    # Keep info updated
    await update_user_info(message.from_user)
    
    if user_id in USER_STATE:
        state = USER_STATE[user_id]
        if state["step"] == "ask_name":
            proj_name = message.text.strip().replace(" ", "_")
            exist = await projects_col.find_one({"user_id": user_id, "name": proj_name})
            if exist: return await message.reply("❌ Name exists. Try another.")
            USER_STATE[user_id] = {"step": "wait_files", "name": proj_name}
            keyboard = ReplyKeyboardMarkup([[KeyboardButton("✅ Done / Start Deploy")]], resize_keyboard=True)
            await message.reply(f"✅ Project: `{proj_name}`\n**Now send files.**", reply_markup=keyboard)
        elif message.text == "✅ Done / Start Deploy":
            if state["step"] == "wait_files":
                await finish_deployment(client, message)

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
        with open(save_path, "rb") as f:
            file_content = f.read()
        await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$pull": {"files": {"name": file_name}}})
        await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$push": {"files": {"name": file_name, "content": file_content}}}, upsert=True)
        if data["step"] == "update_files":
            await message.reply(f"📥 **Updated:** `{file_name}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish & Restart", callback_data=f"act_restart_{proj_name}")]]))
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
    if not silent and client: msg = await client.send_message(chat_id, f"⏳ **Initializing {proj_name}...**")
    
    if not os.path.exists(os.path.join(base_path, "main.py")):
        if not silent and client: await msg.edit_text("❌ Error: Files lost.")
        return

    if os.path.exists(os.path.join(base_path, "requirements.txt")):
        if not silent and client: await msg.edit_text("📥 **Installing Libraries...**")
        install_cmd = f"pip install -r {base_path}/requirements.txt"
        proc = await asyncio.create_subprocess_shell(install_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.communicate()

    if not silent and client: await msg.edit_text("🚀 **Launching Bot...**")
    
    run_proc = await asyncio.create_subprocess_exec(
        "python3", "-u", "main.py", 
        cwd=base_path, 
        stdout=asyncio.subprocess.PIPE, 
        stderr=asyncio.subprocess.STDOUT
    )
    
    project_id = f"{user_id}_{proj_name}"
    p_chat_id = chat_id if chat_id else user_id 
    ACTIVE_PROCESSES[project_id] = {"proc": run_proc, "chat_id": p_chat_id}
    
    log_file_path = f"{base_path}/logs.txt"
    open(log_file_path, 'w').close()
    
    asyncio.create_task(monitor_process_output(run_proc, project_id, log_file_path, bot_app))
    await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"status": "Running", "path": base_path}})
    
    if not silent and client:
        await msg.edit_text(f"✅ **{proj_name} is Online!**", reply_markup=get_main_menu(user_id))
    
    await asyncio.sleep(5)
    if run_proc.returncode is not None:
        if project_id in ACTIVE_PROCESSES: del ACTIVE_PROCESSES[project_id]
        await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"status": "Crashed"}})
        if not silent and client:
             await client.send_document(p_chat_id, log_file_path, caption=f"⚠️ **{proj_name} Crashed!**")

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
async def project_menu(client, callback):
    try: proj_name = callback.data.replace("p_menu_", "")
    except: return
    user_id = callback.from_user.id
    project_id = f"{user_id}_{proj_name}"
    doc = await projects_col.find_one({"user_id": user_id, "name": proj_name})
    if not doc: return await callback.answer("Project Not Found", show_alert=True)
    is_running = doc.get("status") == "Running"
    status_btn_text = "🛑 Stop" if is_running else "▶️ Start"
    log_btn_text = "🔴 Disable Logs" if LOGGING_FLAGS.get(project_id, False) else "🟢 Enable Logs"
    btns = [
        [InlineKeyboardButton(status_btn_text, callback_data=f"act_toggle_{proj_name}")],
        [InlineKeyboardButton(log_btn_text, callback_data=f"act_logtoggle_{proj_name}"), InlineKeyboardButton("📥 Download Logs", callback_data=f"act_dlogs_{proj_name}")],
        [InlineKeyboardButton("♻️ Restart", callback_data=f"act_restart_{proj_name}")],
        [InlineKeyboardButton("📤 Update Files", callback_data=f"act_update_{proj_name}")],
        [InlineKeyboardButton("🗑️ Delete", callback_data=f"act_delete_{proj_name}")],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_projects")]
    ]
    status_display = "Running 🟢" if is_running else "Stopped 🔴"
    await safe_edit(callback.message, f"⚙️ **Manage: `{proj_name}`**\nStatus: {status_display}", reply_markup=InlineKeyboardMarkup(btns))

@bot_app.on_callback_query(filters.regex(r"^act_"))
async def project_actions(client, callback):
    try:
        parts = callback.data.split("_", 2)
        if len(parts) < 3: return
        action = parts[1]
        proj_name = parts[2]
    except: return
    user_id = callback.from_user.id
    project_id = f"{user_id}_{proj_name}"
    doc = await projects_col.find_one({"user_id": user_id, "name": proj_name})
    if not doc: return await callback.answer("Project Not Found!", show_alert=True)
    if action == "toggle":
        if doc.get("status") == "Running":
            await stop_project_process(project_id)
            await projects_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "Stopped"}})
            await callback.answer("🛑 Stopped")
        else:
            await ensure_files_exist(user_id, proj_name)
            await callback.answer("▶️ Starting...")
            await start_process_logic(client, callback.message.chat.id, user_id, proj_name)
        await project_menu(client, callback)
    elif action == "logtoggle":
        current = LOGGING_FLAGS.get(project_id, False)
        LOGGING_FLAGS[project_id] = not current
        await callback.answer(f"Logs {'Disabled' if current else 'Enabled'}")
        await project_menu(client, callback)
    elif action == "dlogs":
        log_path = f"./deployments/{user_id}/{proj_name}/logs.txt"
        if os.path.exists(log_path): await client.send_document(callback.message.chat.id, log_path, caption=f"📄 Logs: {proj_name}")
        else: await callback.answer("❌ No logs found.", show_alert=True)
    elif action == "restart":
        await stop_project_process(project_id)
        await ensure_files_exist(user_id, proj_name)
        await safe_edit(callback.message, "♻️ Restarting...")
        await start_process_logic(client, callback.message.chat.id, user_id, proj_name)
    elif action == "delete":
        await stop_project_process(project_id)
        await projects_col.delete_one({"_id": doc["_id"]})
        shutil.rmtree(doc["path"], ignore_errors=True)
        await callback.answer("Deleted.")
        await list_projects(client, callback)
    elif action == "update":
        USER_STATE[user_id] = {"step": "update_files", "name": proj_name}
        await safe_edit(callback.message, f"📤 **Update Mode: `{proj_name}`**\nSend files.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="manage_projects")]]))

@bot_app.on_callback_query(filters.regex("main_menu"))
async def back_main(client, callback):
    if callback.from_user.id in USER_STATE: del USER_STATE[callback.from_user.id]
    await safe_edit(callback.message, "🏠 **Main Menu**", reply_markup=get_main_menu(callback.from_user.id))

# ================= MAIN EXECUTION =================

async def main():
    print("🚀 SYSTEM STARTUP: Initializing Master Bot...")
    
    # 1. Start Telegram Client
    await bot_app.start()
    print("✅ Telegram Bot Connected.")
    
    # 2. Restore Old Processes
    await restore_all_projects()
    
    # 3. Start Resource Monitor
    asyncio.create_task(resource_monitor())
    print("🛡️ Resource Monitor Active (Limit: 1GB/bot)")

    # 4. Start Web Server in Background
    config = Config(app=app, host="0.0.0.0", port=PORT, log_level="info")
    server = Server(config)
    
    print(f"🌍 Web Dashboard Running on Port: {PORT}")
    asyncio.create_task(server.serve())

    # 5. Keep Main Process Alive
    print("🟢 Master Bot is IDLE and Ready.")
    await idle()
    
    await bot_app.stop()

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass