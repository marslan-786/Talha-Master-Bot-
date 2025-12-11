import os
import sys
import asyncio
import logging
import uuid
import shutil
import signal
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.errors import FloodWait
from motor.motor_asyncio import AsyncIOMotorClient

# ================= CONFIGURATION =================
# Updated with your provided credentials
API_ID = 94575
API_HASH = "a3406de8d171bb422bb6ddf3bbd800e2"
BOT_TOKEN = "8505785410:AAGiDN3FuECbg_K6N_qtjK7OjXh1YYPy5fk"
MONGO_URL = "mongodb://mongo:AEvrikOWlrmJCQrDTQgfGtqLlwhwLuAA@crossover.proxy.rlwy.net:29609"

# List of Owner IDs
OWNER_IDS = [8167904992, 7134046678] 

# ================= DATABASE SETUP =================
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["master_bot_db"]
users_col = db["authorized_users"]
keys_col = db["access_keys"]
projects_col = db["projects"]

# ================= GLOBAL VARIABLES =================
# Stores process objects in RAM
# Format: {project_id: asyncio.subprocess.Process}
ACTIVE_PROCESSES = {} 

# Stores user state (What the user is currently doing)
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
    """Stops the process from memory and background"""
    if project_id in ACTIVE_PROCESSES:
        proc = ACTIVE_PROCESSES[project_id]
        try:
            proc.terminate()
            # Wait a bit, if not stopped, kill it forcefully
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
            "Welcome to the **Master Bot Panel**.\n"
            "Please select an option from the menu below to proceed.",
            reply_markup=get_main_menu(user_id)
        )
    else:
        # If token is provided: /start TOKEN_HERE
        if len(message.command) > 1:
            token = message.command[1]
            key_doc = await keys_col.find_one({"key": token, "status": "active"})
            
            if key_doc:
                await keys_col.update_one({"_id": key_doc["_id"]}, {"$set": {"status": "used", "used_by": user_id}})
                await users_col.insert_one({"user_id": user_id, "joined_at": message.date})
                await message.reply_text(
                    "✅ **Access Granted!**\n\n"
                    "Your token has been verified successfully.", 
                    reply_markup=get_main_menu(user_id)
                )
            else:
                await message.reply_text("❌ **Invalid or Used Token.**\n\nPlease contact the admin to get a valid token.")
        else:
            await message.reply_text(
                "🔒 **Access Denied**\n\n"
                "This is a private bot.\n"
                "You need a valid **Access Key** to use it.\n\n"
                "**Usage:**\n"
                "`/start <your_access_key>`"
            )

# ================= OWNER PANEL =================

@app.on_callback_query(filters.regex("owner_panel"))
async def owner_panel_cb(client, callback):
    if callback.from_user.id not in OWNER_IDS:
        return await callback.answer("Only for Owners!", show_alert=True)
    
    btns = [
        [InlineKeyboardButton("🔑 Generate New Key", callback_data="gen_key")],
        [InlineKeyboardButton("📜 List Active Keys", callback_data="list_keys")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    await callback.message.edit_text(
        "👑 **Owner Panel**\n\n"
        "Manage access keys and users from here.", 
        reply_markup=InlineKeyboardMarkup(btns)
    )

@app.on_callback_query(filters.regex("gen_key"))
async def generate_key(client, callback):
    new_key = str(uuid.uuid4())[:8] # Generate short key
    await keys_col.insert_one({"key": new_key, "status": "active", "created_by": callback.from_user.id})
    
    text = (
        f"✅ **New Access Key Created:**\n\n"
        f"`{new_key}`\n\n"
        f"**Share this command with the user:**\n"
        f"`/start {new_key}`"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="owner_panel")]]))

# ================= DEPLOYMENT FLOW =================

@app.on_callback_query(filters.regex("deploy_new"))
async def deploy_start(client, callback):
    user_id = callback.from_user.id
    USER_STATE[user_id] = {"step": "ask_name"}
    await callback.message.edit_text(
        "📂 **New Project Deployment**\n\n"
        "Please send a **Name** for your project.\n"
        "(Use English alphabets only, no spaces).\n\n"
        "**Example:** `my_music_bot`", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="main_menu")]])
    )

@app.on_message(filters.text & filters.private)
async def handle_text_input(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE:
        state = USER_STATE[user_id]
        
        # Step 1: Project Name
        if state["step"] == "ask_name":
            proj_name = message.text.strip().replace(" ", "_")
            
            # Check if name already exists for this user
            exist = await projects_col.find_one({"user_id": user_id, "name": proj_name})
            if exist:
                return await message.reply("❌ **Error:** A project with this name already exists.\nPlease try a different name.")
            
            USER_STATE[user_id] = {"step": "wait_files", "name": proj_name, "files": {}}
            await message.reply(
                f"✅ **Project Name Set:** `{proj_name}`\n\n"
                "**Now, please send the following 2 files:**\n\n"
                "1️⃣ `requirements.txt`\n"
                "2️⃣ `main.py` (or your main script)\n\n"
                "_(Please send them one by one)_"
            )

@app.on_message(filters.document & filters.private)
async def handle_file_upload(client, message):
    user_id = message.from_user.id
    if user_id in USER_STATE and USER_STATE[user_id]["step"] == "wait_files":
        file_name = message.document.file_name
        proj_data = USER_STATE[user_id]
        
        # Create Folder
        base_path = f"./deployments/{user_id}/{proj_data['name']}"
        os.makedirs(base_path, exist_ok=True)
        
        # Download File
        await message.download(file_name=os.path.join(base_path, file_name))
        
        if file_name == "requirements.txt":
            proj_data["files"]["req"] = True
            await message.reply("📄 **Received:** `requirements.txt`")
        elif file_name.endswith(".py"):
            proj_data["files"]["main"] = file_name
            await message.reply(f"🐍 **Received:** `{file_name}`")
        
        # Check if both files are received
        if "req" in proj_data["files"] and "main" in proj_data["files"]:
            del USER_STATE[user_id] # Clear state
            await start_deployment(client, message.chat.id, user_id, proj_data["name"], proj_data["files"]["main"])

async def start_deployment(client, chat_id, user_id, proj_name, main_file):
    msg = await client.send_message(
        chat_id, 
        f"⚙️ **Deploying: {proj_name}**\n\n"
        "Installing libraries, please wait..."
    )
    
    base_path = f"./deployments/{user_id}/{proj_name}"
    
    # 1. Install Requirements
    install_cmd = f"pip install -r {base_path}/requirements.txt"
    proc = await asyncio.create_subprocess_shell(
        install_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        # If installation fails
        with open(f"{base_path}/install_error.txt", "w") as f:
            f.write(stderr.decode())
        await msg.delete()
        await client.send_document(
            chat_id, 
            f"{base_path}/install_error.txt", 
            caption=f"❌ **Installation Failed** for `{proj_name}`.\n\nPlease check the attached error log."
        )
        return

    # 2. Run Python Script
    await msg.edit_text("🚀 **Starting Bot Script...**")
    
    # Open Log File
    log_file = open(f"{base_path}/runtime_error.log", "w")
    
    # Run Bot
    run_proc = await asyncio.create_subprocess_exec(
        "python3", main_file,
        cwd=base_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=log_file # Errors go to file
    )
    
    # Save to Database and RAM
    project_id = f"{user_id}_{proj_name}"
    ACTIVE_PROCESSES[project_id] = run_proc
    
    await projects_col.update_one(
        {"user_id": user_id, "name": proj_name},
        {"$set": {"status": "Running", "main_file": main_file, "path": base_path}},
        upsert=True
    )
    
    await msg.edit_text(
        f"✅ **Success!**\n\n"
        f"Project: `{proj_name}` is now **Running** in the background."
    )
    
    # 3. Monitor for Early Crash (5 seconds check)
    await asyncio.sleep(5)
    if run_proc.returncode is not None:
        # Bot crashed immediately
        log_file.close()
        await client.send_document(
            chat_id, 
            f"{base_path}/runtime_error.log", 
            caption=f"⚠️ **Crashed!**\n\n`{proj_name}` started but stopped immediately.\nPlease check the error log."
        )
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
    await callback.message.edit_text(
        "📂 **Your Projects**\n\n"
        "Click on a project below to manage it:", 
        reply_markup=InlineKeyboardMarkup(btns)
    )

@app.on_callback_query(filters.regex(r"^p_menu_"))
async def project_menu(client, callback):
    proj_name = callback.data.split("_")[2]
    user_id = callback.from_user.id
    
    btns = [
        [InlineKeyboardButton("🛑 Stop", callback_data=f"act_stop_{proj_name}"), InlineKeyboardButton("▶️ Start", callback_data=f"act_start_{proj_name}")],
        [InlineKeyboardButton("♻️ Restart", callback_data=f"act_restart_{proj_name}")],
        [InlineKeyboardButton("📤 Update File", callback_data=f"act_update_{proj_name}")],
        [InlineKeyboardButton("🗑️ Delete", callback_data=f"act_delete_{proj_name}")],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_projects")]
    ]
    await callback.message.edit_text(
        f"⚙️ **Managing:** `{proj_name}`\n\n"
        "Choose an action below:", 
        reply_markup=InlineKeyboardMarkup(btns)
    )

@app.on_callback_query(filters.regex(r"^act_"))
async def project_actions(client, callback):
    action, proj_name = callback.data.split("_")[1], callback.data.split("_")[2]
    user_id = callback.from_user.id
    proj_id = f"{user_id}_{proj_name}"
    
    doc = await projects_col.find_one({"user_id": user_id, "name": proj_name})
    if not doc:
        return await callback.answer("Project not found!", show_alert=True)
        
    if action == "stop":
        await stop_project_process(proj_id)
        await projects_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "Stopped"}})
        await callback.answer("Project Stopped.")
        await list_projects(client, callback) # Refresh List
        
    elif action == "start":
        await callback.message.edit_text("⏳ **Starting...**")
        await start_deployment(client, callback.message.chat.id, user_id, proj_name, doc["main_file"])
        
    elif action == "restart":
        await stop_project_process(proj_id)
        await callback.message.edit_text("⏳ **Restarting...**")
        await start_deployment(client, callback.message.chat.id, user_id, proj_name, doc["main_file"])
        
    elif action == "update":
        # Ask user which file
        btns = [
            [InlineKeyboardButton("🐍 Update Python File", callback_data=f"upd_py_{proj_name}")],
            [InlineKeyboardButton("📄 Update Requirements", callback_data=f"upd_req_{proj_name}")]
        ]
        await callback.message.edit_text(
            "📤 **Update File**\n\n"
            "Which file do you want to update?", 
            reply_markup=InlineKeyboardMarkup(btns)
        )

    elif action == "delete":
        await stop_project_process(proj_id)
        await projects_col.delete_one({"_id": doc["_id"]})
        shutil.rmtree(doc["path"], ignore_errors=True) # Delete folder
        await callback.answer("Project Deleted!")
        await list_projects(client, callback)

# Update Logic Handling
@app.on_callback_query(filters.regex(r"^upd_"))
async def ask_update_file(client, callback):
    type_, proj_name = callback.data.split("_")[1], callback.data.split("_")[2]
    user_id = callback.from_user.id
    
    USER_STATE[user_id] = {"step": "update_file", "name": proj_name, "type": type_}
    file_type_name = "Python File (.py)" if type_ == "py" else "Requirements File (.txt)"
    
    await callback.message.edit_text(
        f"📤 **Upload New File**\n\n"
        f"Please send the new **{file_type_name}** for `{proj_name}`."
    )

@app.on_message(filters.document & filters.private)
async def handle_update_upload(client, message):
    user_id = message.from_user.id
    
    if user_id in USER_STATE and USER_STATE[user_id]["step"] == "update_file":
        data = USER_STATE[user_id]
        proj_name = data["name"]
        
        base_path = f"./deployments/{user_id}/{proj_name}"
        file_name = message.document.file_name
        
        # Save file to path
        save_path = os.path.join(base_path, file_name)
        await message.download(save_path)
        
        await message.reply(
            "✅ **File Updated Successfully!**\n\n"
            "The bot is restarting now to apply changes..."
        )
        
        # Update main filename in DB if it was a python file
        if data["type"] == "py":
            await projects_col.update_one({"user_id": user_id, "name": proj_name}, {"$set": {"main_file": file_name}})
        
        # Restart Logic
        proj_id = f"{user_id}_{proj_name}"
        await stop_project_process(proj_id) # Stop old
        
        doc = await projects_col.find_one({"user_id": user_id, "name": proj_name})
        await start_deployment(client, message.chat.id, user_id, proj_name, doc["main_file"]) # Start new
        
        del USER_STATE[user_id]

# ================= GENERAL NAVIGATION =================
@app.on_callback_query(filters.regex("main_menu"))
async def back_main(client, callback):
    await callback.message.edit_text(
        "🏠 **Main Menu**\n\n"
        "Select an option below:", 
        reply_markup=get_main_menu(callback.from_user.id)
    )

if __name__ == "__main__":
    print("Master Bot Started...")
    app.run()
